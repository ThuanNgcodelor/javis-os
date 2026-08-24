#!/bin/bash
# ==============================================================================
# JAVIS OS — 4-TERMINALS LAUNCHER CHO MACOS
# Khởi động toàn bộ hệ thống trong 4 cửa sổ / tab Terminal riêng biệt:
#   1. 🔴 Redis Stack (Docker :6379)
#   2. 🤖 Ollama Server (:11434)
#   3. 🔁 n8n Workflow Gateway (:5678)
#   4. 🧠 JAVIS OS Core (:7777) & ☁️ Cloudflared Tunnel
# ==============================================================================
set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REDIS_DIR="$APP_DIR/infra/redis"
CF_CFG="$APP_DIR/infra/cloudflared/config.yml"
SERVER_DIR="$APP_DIR/server"

# Hàm mở cửa sổ Terminal mới an toàn, không bị lỗi escape dấu ngoặc
open_tab() {
  local title="$1"
  local cmd="$2"
  osascript -e 'on run argv' \
            -e 'tell application "Terminal"' \
            -e 'activate' \
            -e 'do script (item 2 of argv)' \
            -e 'end tell' \
            -e 'end run' "$title" "$cmd"
}

echo ""
echo "🚀 Đang mở 4 cửa sổ Terminal riêng biệt cho JAVIS OS..."
echo ""

# ── [1/4] Tab 1: Redis Stack ──────────────────────────────────────────────────
echo "   [1/4] Mở Terminal Redis Stack (:6379)..."
open_tab "🔴 Redis :6379" "cd \"$REDIS_DIR\" && docker compose up"
sleep 1

# ── [2/4] Tab 2: Ollama ───────────────────────────────────────────────────────
echo "   [2/4] Mở Terminal Ollama Server (:11434)..."
open_tab "🤖 Ollama :11434" "export OLLAMA_HOST=0.0.0.0:11434; export OLLAMA_CONTEXT_LENGTH=4096; export OLLAMA_NUM_PARALLEL=1; ollama serve"
sleep 1

# ── [3/4] Tab 3: n8n Gateway ──────────────────────────────────────────────────
echo "   [3/4] Mở Terminal n8n Workflow Gateway (:5678)..."
open_tab "🔁 n8n :5678" "export WEBHOOK_URL=https://n8n.dinhduongcantho.io.vn/; export N8N_EDITOR_BASE_URL=https://n8n.dinhduongcantho.io.vn; export N8N_HOST=n8n.dinhduongcantho.io.vn; export N8N_PROTOCOL=https; npx n8n start"
sleep 1

# ── [4/4] Tab 4: JAVIS OS & Cloudflared Tunnel ────────────────────────────────
echo "   [4/4] Mở Terminal JAVIS OS Core (:7777) & Cloudflare Tunnel..."
open_tab "🧠 Javis OS :7777" "(cloudflared tunnel --config \"$CF_CFG\" run n8n-dinhduongcantho &); cd \"$SERVER_DIR\" && source ../.venv/bin/activate && python -m uvicorn main:app --host 0.0.0.0 --port 7777 --reload"

echo ""
echo "✅ Đã mở thành công 4 Terminal!"
echo "🌐 Đang mở Dashboard JAVIS OS: http://localhost:7777"
echo ""

sleep 2
open "http://localhost:7777" 2>/dev/null || true
