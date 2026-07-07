#!/usr/bin/env python3
"""
Blog Digest — tự động tóm tắt bài blog mới và gửi về Discord.

Luồng xử lý:
  1. Đọc danh sách feed từ feeds.txt
  2. Parse RSS, lấy các bài trong feed
  3. Lọc bài MỚI (so với state.json — chống trùng lặp)
  4. Tải toàn bộ nội dung bài viết (trafilatura)
  5. Gọi OpenRouter (model free) để tóm tắt
  6. Gộp tất cả tóm tắt thành 1 bản tin, gửi qua Discord
  7. Cập nhật state.json (sẽ được workflow commit ngược vào repo)

Khóa bí mật đọc qua biến môi trường (GitHub Secrets), KHÔNG hard-code:
  OPENROUTER_API_KEY, DISCORD_WEBHOOK_URL
"""

import os
import sys
import json
import time
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
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "mistralai/mistral-7b-instruct:free",
]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Giới hạn an toàn
MAX_ITEMS_PER_RUN = 15      # tối đa số bài xử lý mỗi lần chạy (tránh flood + rate limit)
MAX_ARTICLE_CHARS = 8000    # cắt bớt bài quá dài trước khi đưa vào AI (tiết kiệm token)
DELAY_BETWEEN_CALLS = 3     # giây nghỉ giữa 2 lần gọi API (né rate limit model free)
DISCORD_MAX_CHARS = 1900    # giới hạn ký tự / tin nhắn Discord (thực tế 2000, chừa lề)

# Ngôn ngữ tóm tắt mong muốn
SUMMARY_LANG = "tiếng Việt"


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
    """Duyệt tất cả feed, trả về danh sách bài mới (chưa có trong seen)."""
    new_items = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"Lỗi parse feed {url}: {e}", file=sys.stderr)
            continue

        source = parsed.feed.get("title", url)
        for entry in parsed.entries:
            eid = entry_id(entry)
            if not eid or eid in seen:
                continue
            new_items.append({
                "id": eid,
                "title": entry.get("title", "(không có tiêu đề)"),
                "link": entry.get("link", eid),
                "source": source,
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
    for chunk in chunks:
        try:
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
            # Discord trả 204 (No Content) khi gửi thành công
            if resp.status_code not in (200, 204):
                print(f"Lỗi gửi Discord {resp.status_code}: {resp.text[:200]}",
                      file=sys.stderr)
        except Exception as e:
            print(f"Lỗi gửi Discord: {e}", file=sys.stderr)
        time.sleep(1)  # né rate limit của webhook Discord


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
    if not feeds:
        print("Danh sách feed rỗng.", file=sys.stderr)
        return 1

    state = load_state()
    seen = set(state["seen"])
    first_run = not STATE_FILE.exists()

    new_items = collect_new_entries(feeds, seen)
    print(f"Tìm thấy {len(new_items)} bài mới.")

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
    for item in new_items:
        print(f"Đang xử lý: {item['title']}")
        text = fetch_article_text(item["link"])
        if not text:
            # Không tải được toàn bài → vẫn đánh dấu đã xử lý để không thử lại mãi
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
        time.sleep(DELAY_BETWEEN_CALLS)

    # Gộp thành 1 bản tin (định dạng Markdown của Discord)
    if summaries:
        lines = [f"📰 **Bản tin blog** — {len(summaries)} bài mới\n"]
        for s in summaries:
            lines.append(
                f"**{s['title']}**\n"
                f"*{s['source']}*\n"
                f"{s['summary']}\n"
                f"🔗 <{s['link']}>\n"
            )
        send_discord("\n".join(lines), webhook_url)
        print(f"Đã gửi bản tin gồm {len(summaries)} bài.")
    else:
        print("Không tạo được tóm tắt nào.")

    state["seen"] = list(seen)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
