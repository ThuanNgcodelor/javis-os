#!/bin/bash
# ==============================================================================
# Click đúp để dừng toàn bộ dịch vụ JAVIS OS, n8n, Ollama, Redis, Cloudflared
# ==============================================================================
cd "$(dirname "$0")" || exit 1
bash "bin/stop-all.sh"
echo ""
echo "Nhấn phím bất kỳ để đóng cửa sổ..."
read -n 1 -s
