#!/usr/bin/env python3
"""
Blog Digest — tự động tóm tắt bài blog + newsletter mới và gửi về Discord.

Luồng xử lý:
  1. Đọc danh sách feed từ feeds.txt + (tùy chọn) đọc newsletter trong Gmail
  2. Parse RSS / đọc email, lấy nội dung mới
  3. Lọc mục MỚI (so với state.json — chống trùng lặp)
  4. Tải toàn bộ nội dung (trafilatura cho RSS; body email cho newsletter)
  5. Gọi OpenRouter (model free) để tóm tắt
  6. Gộp tất cả tóm tắt thành 1 bản tin, gửi qua Discord
  7. Cập nhật state.json (sẽ được workflow commit ngược vào repo)

Khóa bí mật đọc qua biến môi trường (GitHub Secrets), KHÔNG hard-code:
  OPENROUTER_API_KEY, DISCORD_WEBHOOK_URL
  (tùy chọn, để đọc Gmail) GMAIL_ADDRESS, GMAIL_APP_PASSWORD
"""

import os
import sys
import json
import time
import email
import imaplib
from email.header import decode_header
from datetime import datetime, timedelta
from pathlib import Path

import requests
import feedparser
import trafilatura

# ----------------------------- Cấu hình -----------------------------

FEEDS_FILE = Path("feeds.txt")
STATE_FILE = Path("state.json")

# Danh sách model free của OpenRouter, thử lần lượt nếu model trước lỗi/quá tải.
# LƯU Ý: danh sách model free thay đổi theo thời gian — kiểm tra tại
# https://openrouter.ai/models?max_price=0 và cập nhật lại nếu cần.
OPENROUTER_MODELS = [
    "tencent/hy3:free",
    "google/gemma-4-26b-a4b-it:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "openrouter/free",
]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Giới hạn an toàn
MAX_ITEMS_PER_RUN = 15      # tối đa số bài xử lý mỗi lần chạy (tránh flood + rate limit)
MAX_ITEMS_PER_FEED = 1      # mỗi blog/feed chỉ lấy bài mới nhất
MAX_ARTICLE_CHARS = 8000    # cắt bớt bài quá dài trước khi đưa vào AI (tiết kiệm token)
DELAY_BETWEEN_CALLS = 3     # giây nghỉ giữa 2 lần gọi API (né rate limit model free)
DISCORD_MAX_CHARS = 1900    # giới hạn ký tự / tin nhắn Discord (thực tế 2000, chừa lề)

# Ngôn ngữ tóm tắt mong muốn
SUMMARY_LANG = "tiếng Việt"

# --- Cấu hình Gmail (tùy chọn) ---
# Chỉ đọc email trong nhãn này (bạn tự tạo filter đưa newsletter vào nhãn).
GMAIL_LABEL = "Newsletters"
IMAP_HOST = "imap.gmail.com"
GMAIL_LOOKBACK_DAYS = 7      # chỉ xét email trong N ngày gần nhất (giảm tải)


# ----------------------------- State -----------------------------

def load_state() -> dict:
    """Đọc state đã lưu. Trả về {'seen': [...urls...]}."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen": []}


def save_state(state: dict) -> None:
    # Giới hạn kích thước state (chỉ giữ 2000 link gần nhất) để file không phình vô hạn
    state["seen"] = state["seen"][-2000:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------- Feeds -----------------------------

def load_feeds() -> list[str]:
    if not FEEDS_FILE.exists():
        print(f"Không tìm thấy {FEEDS_FILE}", file=sys.stderr)
        return []
    feeds = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            feeds.append(line)
    return feeds


def entry_id(entry) -> str:
    """Định danh duy nhất của 1 bài: ưu tiên link (bền hơn ngày đăng)."""
    return entry.get("link") or entry.get("id") or entry.get("title", "")


def collect_new_entries(feeds: list[str], seen: set[str]) -> list[dict]:
    """Duyệt tất cả feed, trả về tối đa 1 bài mới nhất mỗi feed nếu chưa có trong seen."""
    new_items = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"Lỗi parse feed {url}: {e}", file=sys.stderr)
            continue

        source = parsed.feed.get("title", url)
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            eid = entry_id(entry)
            if not eid or eid in seen:
                continue
            new_items.append({
                "id": eid,
                "title": entry.get("title", "(không có tiêu đề)"),
                "link": entry.get("link", eid),
                "source": source,
                "content": None,  # RSS: tải nội dung sau; email: đã có sẵn
            })
    return new_items


# ----------------------------- Tải nội dung bài -----------------------------

def fetch_article_text(url: str) -> str:
    """Tải toàn bộ bài viết và trích nội dung chính bằng trafilatura."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        text = trafilatura.extract(downloaded, include_comments=False,
                                   include_tables=False) or ""
        return text[:MAX_ARTICLE_CHARS]
    except Exception as e:
        print(f"Lỗi tải bài {url}: {e}", file=sys.stderr)
        return ""


# ----------------------------- Đọc newsletter từ Gmail -----------------------------

def _decode_mime(value: str) -> str:
    """Giải mã header email (tiêu đề, người gửi) về chuỗi đọc được."""
    if not value:
        return ""
    out = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _extract_email_text(msg) -> str:
    """Lấy nội dung email: ưu tiên text/plain, nếu chỉ có HTML thì trích qua trafilatura."""
    plain, html_body = None, None
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition") or ""):
                continue
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ctype == "text/plain" and plain is None:
                plain = text
            elif ctype == "text/html" and html_body is None:
                html_body = text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_body = text
            else:
                plain = text

    if plain and plain.strip():
        return plain
    if html_body:
        return trafilatura.extract(html_body) or ""
    return ""


def collect_gmail_entries(address: str, app_password: str, seen: set[str]) -> list[dict]:
    """Đọc newsletter chưa đọc trong nhãn Gmail, rồi đánh dấu đã đọc."""
    items = []
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(address, app_password)
        status, _ = imap.select(f'"{GMAIL_LABEL}"', readonly=False)
        if status != "OK":
            print(f"Không mở được nhãn Gmail '{GMAIL_LABEL}'. "
                  f"Kiểm tra tên nhãn có đúng không.", file=sys.stderr)
            imap.logout()
            return items

        since = (datetime.utcnow() - timedelta(days=GMAIL_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        status, data = imap.search(None, "UNSEEN", "SINCE", since)
        if status != "OK":
            imap.logout()
            return items

        for eid in data[0].split():
            status, msg_data = imap.fetch(eid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            msg_id = (msg.get("Message-ID") or "").strip()
            imap.store(eid, "+FLAGS", "\\Seen")
            if not msg_id or msg_id in seen:
                continue
            body = _extract_email_text(msg)[:MAX_ARTICLE_CHARS]
            items.append({
                "id": msg_id,
                "title": _decode_mime(msg.get("Subject")) or "(không tiêu đề)",
                "link": None,  # email không có link bài gốc cố định
                "source": _decode_mime(msg.get("From")),
                "content": body,  # nội dung đã có sẵn, không cần tải lại
            })
        imap.logout()
    except Exception as e:
        print(f"Lỗi đọc Gmail: {e}", file=sys.stderr)
    return items


# ----------------------------- Tóm tắt qua OpenRouter -----------------------------

def summarize(text: str, title: str, api_key: str) -> str | None:
    """Gọi OpenRouter tóm tắt. Thử lần lượt các model free, trả None nếu tất cả lỗi."""
    prompt = (
        f"Tóm tắt bài viết dưới đây bằng {SUMMARY_LANG}, khoảng 3-4 câu, "
        f"nêu ý chính và điểm đáng chú ý nhất. Chỉ trả về bản tóm tắt, "
        f"không thêm lời dẫn.\n\nTiêu đề: {title}\n\nNội dung:\n{text}"
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for model in OPENROUTER_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,
        }
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers,
                                 json=payload, timeout=90)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                if content:
                    return content
            else:
                print(f"Model {model} trả {resp.status_code}: {resp.text[:200]}",
                      file=sys.stderr)
        except Exception as e:
            print(f"Lỗi gọi model {model}: {e}", file=sys.stderr)
        time.sleep(2)  # nghỉ trước khi thử model kế tiếp

    return None


# ----------------------------- Gửi Discord -----------------------------

def send_discord(text: str, webhook_url: str) -> None:
    """Gửi tin nhắn Discord qua webhook, tự chia nhỏ nếu vượt giới hạn ký tự."""
    chunks = split_message(text, DISCORD_MAX_CHARS)
    failed = False
    for chunk in chunks:
        try:
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
            # Discord trả 204 (No Content) khi gửi thành công
            if resp.status_code not in (200, 204):
                print(f"Lỗi gửi Discord {resp.status_code}: {resp.text[:200]}",
                      file=sys.stderr)
                failed = True
        except Exception as e:
            print(f"Lỗi gửi Discord: {e}", file=sys.stderr)
            failed = True
        time.sleep(1)  # né rate limit của webhook Discord
    if failed:
        raise RuntimeError("Gửi Discord thất bại. Kiểm tra DISCORD_WEBHOOK_URL.")


def split_message(text: str, limit: int) -> list[str]:
    """Chia text thành các đoạn <= limit, cắt theo ranh giới bài (dòng trống kép)."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > limit:
            if current:
                parts.append(current.rstrip())
            current = block + "\n\n"
        else:
            current += block + "\n\n"
    if current.strip():
        parts.append(current.rstrip())
    return parts


# ----------------------------- Main -----------------------------

def main() -> int:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

    if not all([api_key, webhook_url]):
        print("Thiếu biến môi trường: OPENROUTER_API_KEY / DISCORD_WEBHOOK_URL",
              file=sys.stderr)
        return 1

    feeds = load_feeds()
    gmail_addr = os.environ.get("GMAIL_ADDRESS")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    gmail_on = bool(gmail_addr and gmail_pass)

    if not feeds and not gmail_on:
        print("Không có feed nào và cũng không bật Gmail.", file=sys.stderr)
        return 1

    state = load_state()
    seen = set(state["seen"])
    first_run = not STATE_FILE.exists()

    # Thu thập từ cả 2 nguồn
    new_items = collect_new_entries(feeds, seen) if feeds else []
    if gmail_on:
        gmail_items = collect_gmail_entries(gmail_addr, gmail_pass, seen)
        print(f"Gmail: tìm thấy {len(gmail_items)} newsletter mới.")
        new_items += gmail_items
    print(f"Tổng cộng {len(new_items)} mục mới.")

    # LẦN CHẠY ĐẦU TIÊN: chỉ ghi nhận các bài hiện có là "đã thấy", KHÔNG tóm tắt —
    # tránh tóm tắt hàng loạt bài cũ và spam bạn ngay lần đầu.
    if first_run:
        for item in new_items:
            seen.add(item["id"])
        state["seen"] = list(seen)
        save_state(state)
        send_discord(
            "✅ Blog Digest đã kích hoạt. Từ giờ bạn sẽ nhận tóm tắt các bài "
            "MỚI đăng sau thời điểm này.",
            webhook_url,
        )
        print("Lần chạy đầu: đã seed state, bỏ qua tóm tắt.")
        return 0

    if not new_items:
        print("Không có bài mới. Kết thúc.")
        return 0

    # Giới hạn số bài xử lý mỗi lần
    new_items = new_items[:MAX_ITEMS_PER_RUN]

    summaries = []
    failed_titles = []
    for item in new_items:
        print(f"Đang xử lý: {item['title']}")
        # Email đã có content sẵn; RSS thì tải toàn bài từ link
        text = item.get("content") or (fetch_article_text(item["link"]) if item["link"] else "")
        if not text:
            # Không lấy được nội dung → vẫn đánh dấu đã xử lý để không thử lại mãi
            seen.add(item["id"])
            continue

        summary = summarize(text, item["title"], api_key)
        if summary:
            summaries.append({
                "title": item["title"],
                "link": item["link"],
                "source": item["source"],
                "summary": summary,
            })
            seen.add(item["id"])
        else:
            failed_titles.append(item["title"])
            print(f"Không tóm tắt được, sẽ thử lại lần sau: {item['title']}",
                  file=sys.stderr)
        time.sleep(DELAY_BETWEEN_CALLS)

    # Gộp thành 1 bản tin (định dạng Markdown của Discord)
    if summaries:
        lines = [f"📰 **Bản tin** — {len(summaries)} mục mới\n"]
        for s in summaries:
            block = (
                f"**{s['title']}**\n"
                f"*{s['source']}*\n"
                f"{s['summary']}\n"
            )
            if s["link"]:  # email không có link thì bỏ dòng này
                block += f"🔗 <{s['link']}>\n"
            lines.append(block)
        send_discord("\n".join(lines), webhook_url)
        print(f"Đã gửi bản tin gồm {len(summaries)} mục.")
    else:
        print("Không tạo được tóm tắt nào.")
        detail = "\n".join(f"- {title}" for title in failed_titles[:8])
        send_discord(
            "⚠️ Blog Digest chạy xong nhưng không tạo được tóm tắt nào. "
            "Khả năng cao model OpenRouter đang lỗi/quá tải.\n\n"
            f"{detail}" if detail else
            "⚠️ Blog Digest chạy xong nhưng không có nội dung mới đủ điều kiện để gửi.",
            webhook_url,
        )

    state["seen"] = list(seen)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
