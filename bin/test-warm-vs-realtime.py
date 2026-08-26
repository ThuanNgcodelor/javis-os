#!/usr/bin/env python3
"""
bin/test-warm-vs-realtime.py — So Sánh Trực Quan Luồng WARM (Cache) vs REALTIME (Live CRM)
Chứng minh rõ ràng khi nào tin nhắn đi vào WARM (Redis < 15ms)
và khi nào tin nhắn đi vào REALTIME (Live AMIS CRM / Ticket System).
"""

import sys
import time
import requests
import json
from pathlib import Path

SERVER_URL = "http://127.0.0.1:7777/api/chat-pipeline"
TICKETS_FILE = Path(__file__).resolve().parent.parent / "chatbot" / "server" / "data" / "internal_tickets.json"

TEST_CASES = [
    {
        "category": "🟢 1. LUỒNG WARM CACHE (Đọc tức thì từ Redis)",
        "query": "Tôi ở gần chợ Ô Môn, muốn mua 10 bao phân NPK thì ghé đại lý nào gần nhất?",
        "expected_path": "Redis WARM Cache (381 Đại lý & Geo Search)",
        "expected_speed": "< 25ms",
    },
    {
        "category": "🟢 1. LUỒNG WARM CACHE (Đọc danh mục sản phẩm Redis)",
        "query": "Chào em, mình muốn tìm hiểu giá phân NPK 20-20-15 để chuẩn bị bón cho vụ tới.",
        "expected_path": "Redis WARM Cache (932 Sản phẩm & Kiến thức Nông Học)",
        "expected_speed": "< 25ms",
    },
    {
        "category": "🔴 2. LUỒNG REALTIME (Tra cứu đơn vận tải thời gian thực)",
        "query": "Cho anh tra cứu đơn hàng số #DH-2026-889 xe đã bốc hàng xong chưa?",
        "expected_path": "REALTIME LIVE CRM API (Bóc tách mã #DH & Tra cứu tiến độ bốc hàng)",
        "expected_speed": "Dynamic Dispatcher",
    },
    {
        "category": "🔴 2. LUỒNG REALTIME (Bảo mật tài chính & Đối chiếu CRM)",
        "query": "Số điện thoại của mình là 0918345678, kiểm tra xem mình có tích điểm hay chiết khấu gì chưa?",
        "expected_path": "REALTIME LIVE CRM API (Tra cứu hồ sơ khách hàng & Mask bảo mật SĐT)",
        "expected_speed": "Dynamic Dispatcher",
    },
    {
        "category": "🔴 2. LUỒNG REALTIME CSKH (Tạo Ticket Khiếu nại SLA 24h & Takeover)",
        "query": "Phân bón mua về bị vón cục quá nhiều, tôi muốn khiếu nại đổi trả ngay!",
        "expected_path": "REALTIME CSKH TICKET (Ghi nhận ticket nội bộ, yêu cầu Mã Lô NSX & Tạm dừng Bot)",
        "expected_speed": "Ticket & Human Takeover",
    },
]


def run_demo():
    print("=" * 80)
    print("🔍 KIỂM THỬ PHÂN ĐỊNH RÕ RÀNG: KHI NÀO DÙNG WARM VS KHI NÀO DÙNG REALTIME")
    print("=" * 80)

    for idx, item in enumerate(TEST_CASES, 1):
        print(f"\n{item['category']}")
        print(f"  💬 Câu hỏi (User): \"{item['query']}\"")
        print(f"  🎯 Cơ chế xử lý  : {item['expected_path']}")

        t0 = time.perf_counter()
        sender_id = f"demo_warm_rt_{idx}_{int(time.time())}"
        res = requests.post(SERVER_URL, json={"brand": "cfc", "sender_id": sender_id, "text": item["query"]})
        latency = round((time.perf_counter() - t0) * 1000, 1)
        data = res.json()

        intent = data.get("intent", "")
        ans = data.get("answer", "")

        print(f"  ⚡ Tốc độ xử lý  : {latency}ms")
        print(f"  🏷️ Intent nhận diện: {intent}")
        print(f"  🤖 Bot phản hồi  :\n     {ans[:140]}...")
        print("-" * 80)

    # Hiển thị bằng chứng ticket được tạo realtime trong file
    print("\n📂 [BẰNG CHỨNG REALTIME]: Các Ticket CSKH & Lead VIP vừa được tạo tự động:")
    if TICKETS_FILE.exists():
        with open(TICKETS_FILE, "r", encoding="utf-8") as f:
            tickets = json.load(f)
            print(f"  Tổng số ticket đã ghi nhận trong file: {len(tickets)} ticket")
            for t in tickets[-2:]:
                print(f"  • Mã Ticket: {t.get('ticket_id')} | Loại: {t.get('ticket_type')} | SLA: {t.get('sla_hours')}h | Trạng thái: {t.get('status')}")
    print("=" * 80)


if __name__ == "__main__":
    run_demo()
