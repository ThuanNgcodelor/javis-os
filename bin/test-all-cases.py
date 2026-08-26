#!/usr/bin/env python3
"""
bin/test-all-cases.py — Script Kiểm Thử Toàn Diện Hệ Thống Chatbot AI (CFC & ZeO)
Hỗ trợ 2 chế độ:
  1. Chạy tự động 14 Test Cases theo file Bang_Danh_Gia_Chatbot_Facebook_AI.xlsx + ZeO Flow
  2. Chế độ Chat tương tác trực tiếp qua dòng lệnh (CLI Interactive Mode)
"""

import sys
import argparse
import requests
import json
import time

SERVER_URL = "http://127.0.0.1:7777/api/chat-pipeline"

TEST_SUITE_CFC = [
    {
        "tc": "TC-01",
        "name": "Nhận diện khách mới & Hỏi giá NPK 20-20-15",
        "query": "Chào em, mình muốn tìm hiểu giá phân NPK 20-20-15 để chuẩn bị bón cho vụ tới.",
        "expect": ["20-20-15", "nuôi", "bảng giá", "số điện thoại"],
    },
    {
        "tc": "TC-02",
        "name": "Nhận diện khách cũ / Đại lý Vĩnh Thạnh hỏi đơn",
        "query": "Anh Ba bên đại lý Vĩnh Thạnh đây, kiểm tra giúp anh tiến độ đơn hàng hôm qua đặt.",
        "expect": ["Vĩnh Thạnh", "tiến độ đơn hàng", "Kho Vận"],
    },
    {
        "tc": "TC-03",
        "name": "Tra cứu tích điểm / SĐT hội viên",
        "query": "Số điện thoại của mình là 0918345678, kiểm tra xem mình có tích điểm hay chiết khấu gì chưa?",
        "expect": ["***5678", "AMIS CRM", "chiết khấu"],
    },
    {
        "tc": "TC-04",
        "name": "Định vị đại lý gần Chợ Ô Môn",
        "query": "Tôi ở gần chợ Ô Môn, muốn mua 10 bao phân NPK thì ghé đại lý nào gần nhất?",
        "expect": ["Địa chỉ", "SĐT", "Chỉ đường"],
    },
    {
        "tc": "TC-05",
        "name": "Live Location GPS tìm điểm bán",
        "query": "Gửi cho mình chỗ bán gần vị trí này nhất",
        "payload_extra": {"latitude": 10.035, "longitude": 105.78},
        "expect": ["CFC", "Địa chỉ", "Chỉ đường"],
    },
    {
        "tc": "TC-06",
        "name": "Đại lý giao tận nhà Định Môn Thới Lai",
        "query": "Khu vực xã Định Môn, Thới Lai có đại lý nào giao tận nhà không shop?",
        "expect": ["Địa chỉ", "SĐT", "Chỉ đường"],
    },
    {
        "tc": "TC-07",
        "name": "Hỏi tồn kho 5 tấn NPK 16-16-8 TE",
        "query": "Sản phẩm NPK 16-16-8 TE bao 50kg trong kho còn nhiều không em? Lấy 5 tấn có liền không?",
        "expect": ["16-16-8 TE", "5 tấn", "nhà máy", "xuất kho"],
    },
    {
        "tc": "TC-08",
        "name": "NPK chuyên lúa đợt 2",
        "query": "Bên mình còn hàng công thức NPK chuyên lúa đợt 2 không?",
        "expect": ["chuyên lúa", "Đợt 2", "đẻ nhánh"],
    },
    {
        "tc": "TC-09",
        "name": "Tra cứu tiến độ đơn xe bốc hàng #DH-2026-889",
        "query": "Cho anh tra cứu đơn hàng số #DH-2026-889 xe đã bốc hàng xong chưa?",
        "expect": ["DH-2026-889", "Kho Vận", "bốc hàng"],
    },
    {
        "tc": "TC-10",
        "name": "Bảo mật bảng giá sỉ & chiết khấu cấp 1",
        "query": "Cho anh xin bảng giá sỉ và mức chiết khấu quý này cho đại lý cấp 1 với.",
        "expect": ["chiết khấu", "bảo mật", "Phòng Kinh doanh"],
    },
    {
        "tc": "TC-11",
        "name": "Bảo mật thông tin công nợ bên thứ 3",
        "query": "Đại lý Minh Phát ở Cờ Đỏ còn nợ tiền đợt trước nhiều không em?",
        "expect": ["bảo mật", "nội bộ"],
    },
    {
        "tc": "TC-12",
        "name": "Tư vấn sầu riêng rụng hạt chuỗi",
        "query": "Sầu riêng giai đoạn nuôi trái non bị rụng hạt chuỗi thì nên bón công thức NPK nào và liều lượng sao?",
        "expect": ["sầu riêng", "rụng hạt chuỗi", "Canxi", "Bo", "kỹ sư"],
    },
    {
        "tc": "TC-13",
        "name": "Khách B2B HTX đặt 30 tấn gặp GĐKD",
        "query": "Tôi muốn đặt 30 tấn phân bón cho hợp tác xã, cần gặp giám đốc kinh doanh thương lượng hợp đồng gấp.",
        "expect": ["Hợp tác xã", "30 tấn", "Hotline", "0292", "Giám Đốc"],
    },
    {
        "tc": "TC-14",
        "name": "Khiếu nại phân vón cục đổi trả ngay (SOP)",
        "query": "Phân bón mua về bị vón cục quá nhiều, tôi muốn khiếu nại đổi trả ngay!",
        "expect": ["vón cục", "Mã Lô", "Lot No", "24 giờ"],
    },
]

TEST_SUITE_ZEO = [
    {
        "tc": "ZEO-01",
        "name": "Hỏi công nghệ Enzyme Thụy Điển",
        "query": "Nước giặt ZeO có công nghệ gì đặc biệt vậy shop?",
        "expect": ["Enzyme", "Thụy Điển"],
    },
    {
        "tc": "ZEO-02",
        "name": "Hỏi nước rửa chén ZIF & link Shopee",
        "query": "Shop có nước rửa chén Zif không, gửi link shopee giúp mình?",
        "expect": ["rửa chén", "Shopee"],
    },
    {
        "tc": "ZEO-03",
        "name": "Hỏi nước lau sàn PANO khử mùi",
        "query": "Nước lau sàn Pano dùng cho sàn gỗ được không?",
        "expect": ["Pano", "lau sàn"],
    },
]


def run_automated_tests():
    print("=" * 75)
    print("🚀 BẮT ĐẦU KIỂM THỬ TỰ ĐỘNG CHATBOT AI TRÊN PORT 7777")
    print("=" * 75)

    # 1. Test CFC
    print("\n🌾 [NHÁNH 1: CFC - PHÂN BÓN CÒ BAY] (14 Kịch Bản Đánh Giá Facebook)")
    print("-" * 75)
    total_cfc = 0
    passed_cfc = 0

    for item in TEST_SUITE_CFC:
        sender_id = f"auto_test_cfc_{item['tc']}_{int(time.time())}"
        payload = {
            "brand": "cfc",
            "sender_id": sender_id,
            "text": item["query"],
        }
        if "payload_extra" in item:
            payload.update(item["payload_extra"])

        t0 = time.perf_counter()
        try:
            res = requests.post(SERVER_URL, json=payload, timeout=5)
            latency = round((time.perf_counter() - t0) * 1000, 1)
            data = res.json()
            ans = data.get("answer", "")
            intent = data.get("intent", "")

            missed = [kw for kw in item["expect"] if kw.lower() not in ans.lower()]
            score = 10 if not missed else (7 if len(missed) == 1 else 4)
            total_cfc += score
            if score == 10:
                passed_cfc += 1

            status = "✅ 10/10" if score == 10 else f"⚠️ {score}/10"
            print(f"[{item['tc']}] {item['name']}: {status} ({latency}ms)")
            print(f"  Query : {item['query']}")
            print(f"  Intent: {intent}")
            print(f"  Answer: {ans[:130]}...")
            if missed:
                print(f"  ❌ Thiếu từ khóa: {missed}")
            print("-" * 75)
        except Exception as e:
            print(f"[{item['tc']}] {item['name']}: ❌ LỖI KẾT NỐI ({e})")
            print("-" * 75)

    # 2. Test ZeO
    print("\n🌿 [NHÁNH 2: ZeO VIETNAM] (FMCG Tiêu Dùng Nhanh & Shopee Mall)")
    print("-" * 75)
    total_zeo = 0
    passed_zeo = 0

    for item in TEST_SUITE_ZEO:
        sender_id = f"auto_test_zeo_{item['tc']}_{int(time.time())}"
        payload = {
            "brand": "zeo",
            "sender_id": sender_id,
            "text": item["query"],
        }
        t0 = time.perf_counter()
        try:
            res = requests.post(SERVER_URL, json=payload, timeout=5)
            latency = round((time.perf_counter() - t0) * 1000, 1)
            data = res.json()
            ans = data.get("answer", "")
            intent = data.get("intent", "")

            missed = [kw for kw in item["expect"] if kw.lower() not in ans.lower()]
            score = 10 if not missed else (7 if len(missed) == 1 else 4)
            total_zeo += score
            if score == 10:
                passed_zeo += 1

            status = "✅ 10/10" if score == 10 else f"⚠️ {score}/10"
            print(f"[{item['tc']}] {item['name']}: {status} ({latency}ms)")
            print(f"  Query : {item['query']}")
            print(f"  Intent: {intent}")
            print(f"  Answer: {ans[:130]}...")
            print("-" * 75)
        except Exception as e:
            print(f"[{item['tc']}] {item['name']}: ❌ LỖI KẾT NỐI ({e})")
            print("-" * 75)

    # Tổng kết
    avg_cfc = round(total_cfc / len(TEST_SUITE_CFC), 2) if TEST_SUITE_CFC else 0
    print("\n" + "=" * 75)
    print(f"🎯 KẾT QUẢ CFC CÒ BAY: {passed_cfc}/{len(TEST_SUITE_CFC)} Test Cases đạt 10/10 (Điểm TB: {avg_cfc}/10.0)")
    print(f"🎯 KẾT QUẢ ZeO VIETNAM : {passed_zeo}/{len(TEST_SUITE_ZEO)} Test Cases đạt 10/10")
    print("=" * 75)


def run_interactive_cli():
    print("=" * 75)
    print("💬 CHẾ ĐỘ CHAT TƯƠNG TÁC DÒNG LỆNH (CLI INTERACTIVE)")
    print("  Gõ 'exit' hoặc 'quit' để thoát.")
    print("  Gõ 'switch' để đổi thương hiệu giữa 'cfc' và 'zeo'.")
    print("=" * 75)

    brand = "cfc"
    sender_id = f"cli_user_{int(time.time())}"

    while True:
        try:
            prompt_str = f"[{brand.upper()}] Bạn: "
            user_input = input(prompt_str).strip()
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                print("Tạm biệt!")
                break
            if user_input.lower() == "switch":
                brand = "zeo" if brand == "cfc" else "cfc"
                sender_id = f"cli_user_{brand}_{int(time.time())}"
                print(f"🔄 Đã chuyển sang thương hiệu: {brand.upper()}")
                continue

            t0 = time.perf_counter()
            res = requests.post(
                SERVER_URL,
                json={"brand": brand, "sender_id": sender_id, "text": user_input},
                timeout=10,
            )
            latency = round((time.perf_counter() - t0) * 1000, 1)
            data = res.json()
            ans = data.get("answer", "")
            intent = data.get("intent", "")

            print(f"\n🤖 Bot ({intent} - {latency}ms):\n{ans}\n")
        except KeyboardInterrupt:
            print("\nĐã dừng phiên chat.")
            break
        except Exception as e:
            print(f"❌ Lỗi kết nối tới Server: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Script for Chatbot AI (Port 7777)")
    parser.add_argument("-i", "--interactive", action="store_true", help="Chạy chế độ chat tương tác")
    args = parser.parse_args()

    if args.interactive:
        run_interactive_cli()
    else:
        run_automated_tests()
