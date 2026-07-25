#!/usr/bin/env python3
"""
Blog Digest — tự động tóm tắt bài blog + newsletter mới (và bài cũ chưa đọc
khi nguồn đó im ắng) rồi gửi về Discord. Chạy hoàn toàn trong GitHub Actions,
không phụ thuộc dịch vụ ngoài nào khác.

Luồng xử lý:
  1. Đọc danh sách feed từ feeds.txt.
  2. (Tùy chọn) Đọc newsletter Gmail qua IMAP (App Password), CHỈ lấy mail từ
     đúng danh sách sender đã duyệt trong GMAIL_SENDERS (nhãn "Newsletters").
  3. Với MỖI nguồn (feed hoặc newsletter), xét độc lập:
       - Có bài/mail MỚI (chưa có trong state.json) → lấy bài mới nhất làm
         "tin mới hôm nay"; nếu nguồn đó có > 1 bài mới cùng lúc, phần dư
         được xếp vào hàng đợi backlog của chính nguồn đó (không bỏ sót).
       - KHÔNG có gì mới → nếu nguồn đó đang có backlog (bài cũ chưa từng
         gửi), lấy bài GẦN NHẤT trong backlog ra làm "bài cũ" hôm nay, gắn
         nhãn ngày đăng gốc rõ ràng trong bản tin.
  4. Tải nội dung CHỈ cho các mục thực sự được chọn hôm nay (trafilatura cho
     RSS qua link; body email qua IMAP tra theo Message-ID) — không tải
     trước hàng loạt, tránh tốn tài nguyên cho bài không dùng tới.
  5. Gọi OpenRouter (model free) để tóm tắt.
  6. Gộp tất cả tóm tắt thành 1 bản tin, gửi qua Discord.
  7. Cập nhật state.json (seen + backlog per-source) — workflow commit
     ngược vào repo.

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
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from pathlib import Path

import requests
import feedparser
import trafilatura

# ----------------------------- Cấu hình -----------------------------

FEEDS_FILE = Path("feeds.txt")
STATE_FILE = Path("state.json")

# --- Cấu hình Gmail (tùy chọn) ---
GMAIL_LABEL = "Newsletters"
IMAP_HOST = "imap.gmail.com"
GMAIL_LOOKBACK_DAYS = 14  # cửa sổ tìm mail MỚI mỗi lần chạy (bài cũ hơn dựa vào backlog)

# CHỈ 6 sender này được xử lý qua Gmail — các sender khác trong nhãn Newsletters
# (kể cả nếu có) bị bỏ qua, vì đã trùng RSS ở feeds.txt hoặc là mail rác đăng ký.
GMAIL_SENDERS = {
    "erik@learnui.design": "Design Hacks (Erik Kennedy)",
    "tamas@heydesigner.com": "HeyDesigner Weekly",
    "hello@sarahdoody.com": "Sarah Doody",
    "uxmovement@substack.com": "UX Movement",
    "aigoodies@mail.beehiiv.com": "AI Goodies",
    "lenny+community-wisdom@substack.com": "Lenny's — Community Wisdom",
}

# Danh sách model free của OpenRouter, thử lần lượt nếu model trước lỗi/quá tải.
OPENROUTER_MODELS = [
    "tencent/hy3:free",
    "google/gemma-4-26b-a4b-it:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "openrouter/free",
]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Giới hạn an toàn
# 13 feed RSS + 6 nguồn Gmail = 19 nguồn tối đa/ngày (mỗi nguồn 1 tin mới) → để dư.
MAX_ITEMS_PER_RUN = 20
MAX_NEW_PER_SOURCE = 1      # mỗi nguồn chỉ đưa 1 tin MỚI/ngày vào bản tin (phần dư -> backlog)
MAX_BACKLOG_PER_SOURCE = 40  # giới hạn hàng đợi backlog mỗi nguồn (tránh phình vô hạn)
MAX_ARTICLE_CHARS = 8000    # cắt bớt bài quá dài trước khi đưa vào AI (tiết kiệm token)
DELAY_BETWEEN_CALLS = 3     # giây nghỉ giữa 2 lần gọi API (né rate limit model free)
DISCORD_MAX_CHARS = 1900    # giới hạn ký tự / tin nhắn Discord (thực tế 2000, chừa lề)

SUMMARY_LANG = "tiếng Việt"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


# ----------------------------- State -----------------------------

def load_state() -> dict:
    """Đọc state đã lưu.

    Schema:
      {
        "seen": [...id đã xử lý (link RSS hoặc Message-ID email)...],
        "backlog": {
            "<source_key>": [
                {"id":.., "title":.., "link":.., "source":.., "published":..,
                 "published_sort":..},
                ...
            ]
        }
      }
    Tương thích ngược: state cũ chỉ có "seen" vẫn đọc được bình thường,
    "backlog" sẽ được khởi tạo rỗng.
    """
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            data.setdefault("seen", [])
            data.setdefault("backlog", {})
            return data
        except Exception:
            pass
    return {"seen": [], "backlog": {}}


def save_state(state: dict) -> None:
    # Giới hạn kích thước "seen" (chỉ giữ 3000 id gần nhất) để file không phình vô hạn
    state["seen"] = state["seen"][-3000:]
    for key, items in list(state.get("backlog", {}).items()):
        state["backlog"][key] = items[-MAX_BACKLOG_PER_SOURCE:]
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


def entry_published(entry) -> tuple[str, str]:
    """Trả về (ngày hiển thị DD/MM/YYYY, khóa sắp xếp YYYY-MM-DD).
    Nếu feed không có ngày, trả về ("", "") — coi như cũ nhất khi so sánh."""
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return "", ""
    try:
        display = time.strftime("%d/%m/%Y", struct)
        sort_key = time.strftime("%Y-%m-%d", struct)
        return display, sort_key
    except Exception:
        return "", ""


def build_rss_candidates(feeds: list[str]) -> dict[str, dict]:
    """Trả về {source_key(=feed url): {"name":.., "candidates":[items mới->cũ chưa lọc seen]}}"""
    pool: dict[str, dict] = {}
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"Lỗi parse feed {url}: {e}", file=sys.stderr)
            continue

        name = parsed.feed.get("title", url)
        candidates = []
        for entry in parsed.entries:
            eid = entry_id(entry)
            if not eid:
                continue
            display_date, sort_key = entry_published(entry)
            candidates.append({
                "id": eid,
                "title": entry.get("title", "(không có tiêu đề)"),
                "link": entry.get("link", eid),
                "source": name,
                "source_key": url,
                "content": None,  # tải nội dung sau
                "published": display_date,
                "published_sort": sort_key,
            })
        pool[url] = {"name": name, "candidates": candidates}
    return pool


# ----------------------------- Newsletter Gmail (qua IMAP) -----------------------------

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


def _parse_email_date(msg) -> tuple[str, str]:
    """Trả về (ngày hiển thị DD/MM/YYYY, khóa sắp xếp YYYY-MM-DD) từ header Date."""
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt is None:
            return "", ""
        return dt.strftime("%d/%m/%Y"), dt.strftime("%Y-%m-%d")
    except Exception:
        return "", ""


def imap_connect(address: str, app_password: str) -> "imaplib.IMAP4_SSL | None":
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(address, app_password)
        status, _ = imap.select(f'"{GMAIL_LABEL}"', readonly=True)
        if status != "OK":
            print(f"Không mở được nhãn Gmail '{GMAIL_LABEL}'. "
                  f"Kiểm tra tên nhãn / nhãn có bật 'Show in IMAP' chưa.",
                  file=sys.stderr)
            imap.logout()
            return None
        return imap
    except Exception as e:
        print(f"Lỗi đăng nhập Gmail IMAP: {e}", file=sys.stderr)
        return None


def _make_gmail_id(source_key: str, sort_key: str, title: str) -> str:
    """ID ổn định cho 1 mail: sender + ngày + tiêu đề. KHÔNG dùng Message-ID
    header — 1 số nguồn (Substack/Beehiiv) có thể thiếu/đổi header này, và
    quan trọng hơn: id này chỉ dùng để chống trùng (seen/backlog), việc tải
    lại nội dung sau này tra theo sender+ngày+tiêu đề (fetch_gmail_body),
    không phụ thuộc Message-ID."""
    return f"{source_key}::{sort_key}::{title.strip().lower()}"


def build_gmail_candidates(imap) -> dict[str, dict]:
    """Với mỗi sender đã duyệt, tìm mail trong N ngày gần nhất (chỉ lấy
    header, KHÔNG tải nội dung — nội dung chỉ tải sau cho mục thực sự được
    chọn). Trả về pool giống định dạng build_rss_candidates."""
    pool: dict[str, dict] = {}
    since = (datetime.utcnow() - timedelta(days=GMAIL_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    for sender, source_name in GMAIL_SENDERS.items():
        source_key = f"gmail:{sender}"
        candidates = []
        try:
            status, data = imap.search(None, "FROM", sender, "SINCE", since)
            if status != "OK":
                print(f"IMAP search lỗi cho {sender}: status={status} data={data}",
                      file=sys.stderr)
            else:
                eids = data[0].split() if data and data[0] else []
                for eid in eids:
                    status, msg_data = imap.fetch(
                        eid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])"
                    )
                    if status != "OK" or not msg_data or not msg_data[0]:
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])
                    display_date, sort_key = _parse_email_date(msg)
                    title = _decode_mime(msg.get("Subject")) or "(không tiêu đề)"
                    candidates.append({
                        "id": _make_gmail_id(source_key, sort_key, title),
                        "title": title,
                        "link": None,
                        "source": source_name,
                        "source_key": source_key,
                        "content": None,  # tải sau, chỉ cho mục được chọn
                        "published": display_date,
                        "published_sort": sort_key,
                    })
        except Exception as e:
            print(f"Lỗi tìm mail từ {sender}: {e}", file=sys.stderr)
        pool[source_key] = {"name": source_name, "candidates": candidates}
    return pool


def fetch_gmail_body(imap, item: dict) -> str:
    """Tải nội dung 1 email cụ thể — tra theo sender + ngày đăng (published_sort)
    +/- 1 ngày (bù lệch múi giờ), khớp thêm tiêu đề nếu 1 ngày có nhiều mail.
    Dùng được cho cả candidate mới lấy qua IMAP lẫn mục backlog cũ/seed thủ công,
    vì không phụ thuộc Message-ID."""
    if imap is None:
        return ""
    source_key = item.get("source_key", "")
    sender = source_key.split("gmail:", 1)[-1] if source_key.startswith("gmail:") else ""
    sort_key = item.get("published_sort") or ""
    if not sender or not sort_key:
        return ""
    try:
        base = datetime.strptime(sort_key, "%Y-%m-%d")
    except Exception:
        return ""
    since = (base - timedelta(days=1)).strftime("%d-%b-%Y")
    before = (base + timedelta(days=2)).strftime("%d-%b-%Y")
    try:
        status, data = imap.search(None, "FROM", sender, "SINCE", since, "BEFORE", before)
        if status != "OK" or not data or not data[0]:
            return ""
        eids = data[0].split()
        if not eids:
            return ""
        chosen = eids[0]
        title = (item.get("title") or "").strip().lower()
        if len(eids) > 1 and title:
            for eid in eids:
                status, msg_data = imap.fetch(eid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                if _decode_mime(msg.get("Subject")).strip().lower() == title:
                    chosen = eid
                    break
        status, msg_data = imap.fetch(chosen, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            return ""
        msg = email.message_from_bytes(msg_data[0][1])
        return _extract_email_text(msg)[:MAX_ARTICLE_CHARS]
    except Exception as e:
        print(f"Lỗi tải nội dung mail ({sender}, {sort_key}): {e}", file=sys.stderr)
        return ""


# ----------------------------- Chọn tin mới / bài cũ theo từng nguồn -----------------------------

def select_items_for_today(pool: dict[str, dict], seen: set[str],
                            backlog: dict[str, list[dict]]) -> tuple[list[dict], list[dict]]:
    """Với mỗi nguồn: có bài mới thì lấy 1 bài mới nhất (dư thì dồn backlog);
    không có bài mới thì lấy bài gần nhất trong backlog ra làm 'bài cũ'."""
    new_items: list[dict] = []
    catchup_items: list[dict] = []

    for source_key, info in pool.items():
        candidates = info["candidates"]
        unseen = [c for c in candidates if c["id"] not in seen]
        unseen.sort(key=lambda c: c.get("published_sort") or "", reverse=True)

        if unseen:
            pick = dict(unseen[0])
            pick["is_backlog"] = False
            new_items.append(pick)

            existing_ids = {b["id"] for b in backlog.get(source_key, [])}
            for extra in unseen[MAX_NEW_PER_SOURCE:]:
                if extra["id"] not in existing_ids and extra["id"] != pick["id"]:
                    backlog.setdefault(source_key, []).append(extra)
        else:
            queue = backlog.get(source_key, [])
            if queue:
                queue.sort(key=lambda c: c.get("published_sort") or "", reverse=True)
                pick = dict(queue.pop(0))
                pick["is_backlog"] = True
                catchup_items.append(pick)
                backlog[source_key] = queue

    return new_items, catchup_items


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
        time.sleep(2)

    return None


# ----------------------------- Gửi Discord -----------------------------

def send_discord(text: str, webhook_url: str) -> None:
    """Gửi tin nhắn Discord qua webhook, tự chia nhỏ nếu vượt giới hạn ký tự."""
    chunks = split_message(text, DISCORD_MAX_CHARS)
    failed = False
    for chunk in chunks:
        try:
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
            if resp.status_code not in (200, 204):
                print(f"Lỗi gửi Discord {resp.status_code}: {resp.text[:200]}",
                      file=sys.stderr)
                failed = True
        except Exception as e:
            print(f"Lỗi gửi Discord: {e}", file=sys.stderr)
            failed = True
        time.sleep(1)
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
    dry_run = env_bool("DRY_RUN")
    prefix = "[DRY RUN] " if dry_run else ""

    if not all([api_key, webhook_url]):
        print("Thiếu biến môi trường: OPENROUTER_API_KEY / DISCORD_WEBHOOK_URL",
              file=sys.stderr)
        return 1

    feeds = load_feeds()
    rss_pool = build_rss_candidates(feeds) if feeds else {}

    gmail_addr = os.environ.get("GMAIL_ADDRESS")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    imap = imap_connect(gmail_addr, gmail_pass) if (gmail_addr and gmail_pass) else None
    gmail_on = imap is not None
    gmail_pool = build_gmail_candidates(imap) if gmail_on else {}
    if gmail_addr and gmail_pass and not gmail_on:
        print("GMAIL_ADDRESS/GMAIL_APP_PASSWORD có set nhưng đăng nhập IMAP lỗi — "
              "chạy tiếp chỉ với RSS.", file=sys.stderr)

    try:
        pool = {**rss_pool, **gmail_pool}

        if not pool:
            print("Không có nguồn nào (feed rỗng và Gmail không bật/lỗi).", file=sys.stderr)
            return 1

        state = load_state()
        seen = set(state["seen"])
        backlog = state.get("backlog", {})
        first_run = not any(seen)  # state chưa từng có id nào -> coi như lần đầu

        if first_run:
            # Lần chạy đầu: chỉ ghi nhận bài hiện có là "đã thấy", KHÔNG tóm tắt,
            # KHÔNG đưa vào backlog (backlog chỉ dùng cho bài phát sinh SAU khi đã bật).
            for info in pool.values():
                for item in info["candidates"]:
                    seen.add(item["id"])
            if not dry_run:
                state["seen"] = list(seen)
                state["backlog"] = {}
                save_state(state)
            send_discord(
                f"{prefix}✅ Blog Digest đã kích hoạt ({len(pool)} nguồn: "
                f"{len(rss_pool)} RSS + {len(gmail_pool)} newsletter). Từ giờ bạn sẽ "
                "nhận tóm tắt bài MỚI, và bài cũ chưa đọc khi nguồn đó im ắng.",
                webhook_url,
            )
            print("Lần chạy đầu: đã seed state, bỏ qua tóm tắt.")
            return 0

        new_items, catchup_items = select_items_for_today(pool, seen, backlog)
        print(f"Tin mới: {len(new_items)} | Bài cũ (catch-up): {len(catchup_items)}")

        all_items = (new_items + catchup_items)[:MAX_ITEMS_PER_RUN]

        if not all_items:
            print("Không có gì để gửi hôm nay (không có tin mới, backlog rỗng ở mọi nguồn).")
            send_discord(
                f"{prefix}✅ Blog Digest chạy xong. Không có tin mới và không còn "
                "bài cũ nào trong hàng đợi hôm nay.",
                webhook_url,
            )
            if not dry_run:
                state["backlog"] = backlog
                save_state(state)
            return 0

        summaries = []
        failed_items = []
        no_text_items = []
        for item in all_items:
            print(f"Đang xử lý ({'bài cũ' if item.get('is_backlog') else 'mới'}): {item['title']}")
            if item.get("source_key", "").startswith("gmail:"):
                text = fetch_gmail_body(imap, item)
            else:
                text = item.get("content") or (fetch_article_text(item["link"]) if item.get("link") else "")
            if not text:
                no_text_items.append(item)
                seen.add(item["id"])
                continue

            summary = summarize(text, item["title"], api_key)
            if summary:
                summaries.append({**item, "summary": summary})
                seen.add(item["id"])
            else:
                failed_items.append(item)
                seen.add(item["id"])
                print(f"Không tóm tắt được, gửi fallback title/link: {item['title']}",
                      file=sys.stderr)
            time.sleep(DELAY_BETWEEN_CALLS)

        def format_block(entry: dict, with_summary: bool) -> str:
            tag = f" _(bài cũ – đăng {entry['published']})_" if entry.get("is_backlog") and entry.get("published") else (
                " _(bài cũ)_" if entry.get("is_backlog") else "")
            block = f"**{entry['title']}**{tag}\n*{entry['source']}*\n"
            if with_summary:
                block += f"{entry['summary']}\n"
            if entry.get("link"):
                block += f"🔗 <{entry['link']}>\n"
            return block

        if summaries or failed_items:
            lines = [f"{prefix}📰 **Bản tin** — {len(summaries)} tóm tắt, {len(failed_items)} fallback\n"]
            for s in summaries:
                lines.append(format_block(s, with_summary=True))
            if failed_items:
                lines.append("⚠️ **Chưa tóm tắt được, gửi link trước:**\n")
                for item in failed_items:
                    lines.append(format_block(item, with_summary=False))
            lines.append(
                "```text\n"
                "Run report\n"
                f"Nguồn: {len(pool)} ({len(rss_pool)} RSS + {len(gmail_pool)} newsletter)\n"
                f"Tin mới: {len(new_items)}\n"
                f"Bài cũ (catch-up): {len(catchup_items)}\n"
                f"Đã xử lý: {len(all_items)}\n"
                f"Tóm tắt thành công: {len(summaries)}\n"
                f"AI fallback: {len(failed_items)}\n"
                f"Không lấy được nội dung: {len(no_text_items)}\n"
                f"Dry run: {dry_run}\n"
                "```"
            )
            send_discord("\n".join(lines), webhook_url)
            print(f"Đã gửi bản tin gồm {len(summaries)} tóm tắt và {len(failed_items)} fallback.")
        else:
            print("Không tạo được tóm tắt nào.")
            send_discord(
                f"{prefix}⚠️ Blog Digest chạy xong nhưng không có mục nào gửi được.\n"
                f"- Không lấy được nội dung: {len(no_text_items)}\n"
                f"- Dry run: {dry_run}",
                webhook_url,
            )

        if dry_run:
            print("DRY_RUN=true: không ghi state.json.")
        else:
            state["seen"] = list(seen)
            state["backlog"] = backlog
            save_state(state)
        return 0
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
