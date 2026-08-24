#!/bin/bash
# ==============================================================================
# JAVIS OS + N8N + OLLAMA + CLOUDFLARED + REDIS — 1-CLICK STOP SCRIPT
# Dừng toàn bộ các tiến trình dịch vụ đang chạy.
# ==============================================================================
set -u

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

echo ""
echo "${BOLD}${CYAN}⏹  Đang dừng toàn bộ dịch vụ JAVIS OS & N8N...${NC}"
echo ""

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REDIS_DIR="$APP_DIR/infra/redis"
LOG_DIR="$APP_DIR/logs"
PID_FILE="$LOG_DIR/pids.txt"

# 1. Dừng theo file PID nếu có
if [ -f "$PID_FILE" ]; then
  while read -r pid; do
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null && echo "  ${GREEN}✓${NC} Đã dừng process PID $pid" || true
    fi
  done < "$PID_FILE"
  rm -f "$PID_FILE"
fi

# 2. Dừng theo port để giải phóng port
echo "  Đang giải phóng các cổng mạng..."
lsof -ti tcp:7777 2>/dev/null | xargs kill -9 2>/dev/null && echo "  ${GREEN}✓${NC} Đã dừng JAVIS OS (:7777)" || true
lsof -ti tcp:5678 2>/dev/null | xargs kill -9 2>/dev/null && echo "  ${GREEN}✓${NC} Đã dừng n8n (:5678)" || true
lsof -ti tcp:11434 2>/dev/null | xargs kill -9 2>/dev/null && echo "  ${GREEN}✓${NC} Đã dừng Ollama (:11434)" || true

# 3. Dừng các tiến trình nền theo tên lệnh
pkill -f "cloudflared" 2>/dev/null && echo "  ${GREEN}✓${NC} Đã dừng Cloudflared tunnel" || true
pkill -f "npx n8n" 2>/dev/null && echo "  ${GREEN}✓${NC} Đã dừng n8n daemon" || true
pkill -f "uvicorn main:app" 2>/dev/null && echo "  ${GREEN}✓${NC} Đã dừng uvicorn server" || true
pkill -f "ollama serve" 2>/dev/null && echo "  ${GREEN}✓${NC} Đã dừng Ollama serve" || true

# 4. Dừng Redis Docker
if [ -d "$REDIS_DIR" ]; then
  echo "  Đang dừng Redis Docker..."
  (cd "$REDIS_DIR" && docker compose down 2>/dev/null || true) && echo "  ${GREEN}✓${NC} Đã dừng Redis Stack" || true
fi

echo ""
echo "${GREEN}${BOLD}✅ TẤT CẢ DỊCH VỤ ĐÃ ĐƯỢC DỪNG AN TOÀN.${NC}"
echo ""
