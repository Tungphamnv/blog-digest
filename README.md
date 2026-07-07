# Blog Digest — Tóm tắt bài blog tự động về Telegram

Tự động đọc RSS các blog bạn theo dõi, tải toàn bài, tóm tắt bằng AI (OpenRouter model free), rồi gộp thành 1 bản tin gửi qua Telegram. Chạy hoàn toàn miễn phí trên GitHub Actions.

## Cách hoạt động

```
GitHub Actions (cron mỗi 2h)
  → đọc feeds.txt → parse RSS → lọc bài mới (state.json)
  → tải toàn bài (trafilatura) → tóm tắt (OpenRouter free)
  → gộp thành 1 bản tin → gửi Telegram
  → commit state.json ngược vào repo
```

## Cài đặt (khoảng 15 phút)

### 1. Tạo repo
Tạo 1 repo mới trên GitHub (public để dùng Actions miễn phí thoải mái), rồi upload toàn bộ các file trong thư mục này lên.

### 2. Lấy khóa OpenRouter
- Đăng ký tại https://openrouter.ai
- Vào phần **Keys**, tạo 1 API key mới, copy lại.

### 3. Tạo Telegram Bot
- Mở Telegram, nhắn cho **@BotFather**, gõ `/newbot`, làm theo hướng dẫn.
- Copy **bot token** BotFather trả về.
- Lấy **chat ID** của bạn: nhắn 1 tin bất kỳ cho bot, rồi mở trình duyệt truy cập
  `https://api.telegram.org/bot<TOKEN>/getUpdates` — tìm `"chat":{"id":...}`.
  (Thay `<TOKEN>` bằng token thật.)

### 4. Khai báo Secrets trên GitHub
Vào repo → **Settings → Secrets and variables → Actions → New repository secret**, thêm 3 secret:

| Tên | Giá trị |
|-----|---------|
| `OPENROUTER_API_KEY` | khóa OpenRouter ở bước 2 |
| `TELEGRAM_BOT_TOKEN` | token bot ở bước 3 |
| `TELEGRAM_CHAT_ID` | chat ID ở bước 3 |

### 5. Thêm blog cần theo dõi
Sửa file `feeds.txt`, mỗi dòng 1 link RSS. Nếu blog không lộ link RSS, thử
`tenmien.com/feed`, `/rss`, `/atom.xml`, hoặc dùng https://rss.app để tạo feed.

### 6. Chạy thử
Vào tab **Actions → Blog Digest → Run workflow** để chạy tay lần đầu.
- **Lần chạy đầu** chỉ ghi nhận các bài hiện có là "đã thấy" và gửi 1 tin xác nhận —
  KHÔNG tóm tắt hàng loạt bài cũ. Từ lần sau, chỉ bài MỚI mới được tóm tắt.

## Tùy chỉnh

Mở `summarize.py`, phần đầu file:
- `OPENROUTER_MODELS`: danh sách model free (thử lần lượt). Cập nhật tại
  https://openrouter.ai/models?max_price=0 nếu model bị ngừng.
- `MAX_ITEMS_PER_RUN`: số bài tối đa mỗi lần chạy.
- `SUMMARY_LANG`: ngôn ngữ tóm tắt.

Đổi lịch chạy: sửa dòng `cron` trong `.github/workflows/digest.yml` (giờ UTC).

## Lưu ý

- Khóa bí mật chỉ nằm trong GitHub Secrets, không có trong code → repo để public vẫn an toàn.
- Model free của OpenRouter có giới hạn request/ngày và đôi khi thay đổi — nếu tóm tắt trống, kiểm tra log trong tab Actions và thử đổi model.
- Nếu 1 bài không tải được toàn văn (bị chặn/paywall), nó sẽ bị bỏ qua nhưng vẫn được đánh dấu đã xử lý để không thử lại mãi.
