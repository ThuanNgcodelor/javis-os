# Javis OS

**AI agentic tự host, đổi được bộ não, có Second Brain và automation.** Javis hỗ trợ **10 nhà cung cấp**: Claude Code, ChatGPT/Codex, Antigravity CLI, OpenRouter, OpenAI, Gemini, Anthropic, Groq, Ollama và provider nội bộ mở rộng.

[Tài liệu tiếng Việt](docs/README.md) · [English](README.en.md)

## Javis là gì?

Javis OS là lớp điều hành AI chạy trên máy hoặc VPS của bạn: trò chuyện, đọc/ghi file, gọi kết nối MCP, chạy skill, việc nền và lưu tri thức trong Second Brain.

Giao diện nhóm model thành **7 nhóm** để chọn nhanh theo nhu cầu, nhưng vẫn giữ quyền đổi model bất cứ lúc nào.

Triết lý cốt lõi: **năng lực nằm ở Javis, không nằm ở model.** Bạn có thể đổi model mà vẫn giữ tool, skill, session, workflow và dữ liệu của mình. Khác biệt thực tế chỉ nằm ở năng lực riêng của từng provider, chẳng hạn lệnh máy của một số CLI.

## Cài đặt và deploy

- Docker/VPS: các Compose file nằm trong [`deploy/docker/`](deploy/docker/), gồm `docker-compose.yml`, `docker-compose.hostinger.yml`, `docker-compose.proxy.yml` và `docker-compose.multi.yml`.
- Hostinger Docker Manager chỉ còn 3 trường cần điền: `DOMAIN_NAME`, `JAVIS_ADMIN_USER`, `JAVIS_ADMIN_PASSWORD`.
- Linux/macOS native: chạy [`deploy/linux/install.sh`](deploy/linux/install.sh).
- Windows: chạy [`deploy/windows/setup.bat`](deploy/windows/setup.bat); sau đó mở bằng [`deploy/windows/JAVIS OS.bat`](deploy/windows/JAVIS OS.bat), và có thể bật tự khởi động bằng `javis-autostart.bat install`.

Xem hướng dẫn triển khai chi tiết tại [DEPLOY.md](DEPLOY.md). Các dữ liệu chạy thật luôn nằm ngoài source image: Docker dùng volume `/data` và `/brains`; không xoá volume khi cập nhật image.

## Phát triển

Trước khi push, chạy CI local theo môi trường Python 3.12 và kiểm tra các đường dẫn deploy dưới `deploy/`. Image GHCR build từ repository root, sử dụng [`deploy/docker/Dockerfile`](deploy/docker/Dockerfile).
