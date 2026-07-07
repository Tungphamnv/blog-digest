# Blog Digest — Tóm tắt bài blog tự động về Discord

Tự động đọc RSS các blog bạn theo dõi, tải toàn bài, tóm tắt bằng AI (OpenRouter model free), rồi gộp thành 1 bản tin gửi qua Discord. Chạy hoàn toàn miễn phí trên GitHub Actions.

## Cách hoạt động

```
GitHub Actions (cron mỗi 2h)
  → đọc feeds.txt → parse RSS → lọc bài mới (state.json)
  → tải toàn bài (trafilatura) → tóm tắt (OpenRouter free)
  → gộp thành 1 bản tin → gửi Discord (webhook)
  → commit state.json ngược vào repo
```

## Cài đặt (khoảng 15 phút)

### 1. Tạo repo
Tạo 1 repo mới trên GitHub (public để dùng Actions miễn phí thoải mái), rồi upload toàn bộ các file trong thư mục này lên.

### 2. Lấy khóa OpenRouter
- Đăng ký tại https://openrouter.ai
- Vào phần **Keys**, tạo 1 API key mới, copy lại.

### 3. Tạo Discord Webhook
- Mở Discord, vào server của bạn (chưa có thì bấm dấu **+** để tạo server miễn phí).
- Rê chuột vào 1 kênh text → bấm **bánh răng** (Edit Channel).
- Vào **Integrations → Webhooks → New Webhook**, đặt tên, rồi **Copy Webhook URL**.
- Link có dạng `https://discord.com/api/webhooks/.../...`

### 4. Khai báo Secrets trên GitHub
Vào repo → **Settings → Secrets and variables → Actions → New repository secret**, thêm 2 secret:

| Tên | Giá trị |
|-----|---------|
| `OPENROUTER_API_KEY` | khóa OpenRouter ở bước 2 |
| `DISCORD_WEBHOOK_URL` | webhook URL ở bước 3 |

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

- Khóa bí mật (khóa OpenRouter + webhook Discord) chỉ nằm trong GitHub Secrets, không có trong code → repo để public vẫn an toàn. Lưu ý: ai có webhook URL đều gửi được tin vào kênh của bạn, nên đừng để lộ nó ra ngoài.
- Model free của OpenRouter có giới hạn request/ngày và đôi khi thay đổi — nếu tóm tắt trống, kiểm tra log trong tab Actions và thử đổi model.
- Nếu 1 bài không tải được toàn văn (bị chặn/paywall), nó sẽ bị bỏ qua nhưng vẫn được đánh dấu đã xử lý để không thử lại mãi.
