#!/usr/bin/env python3
"""
bin/test-amis-live-api.py — Script Kiểm Tra Kết Nối Trực Tiếp Tới MISA AMIS CRM Open API v2
Kéo dữ liệu thật từ máy chủ MISA:
  1. Xác thực OAuth2 qua endpoint /Account
  2. Kéo danh mục Products (Sản phẩm)
  3. Kéo danh bạ Customers (Đại lý / Khách hàng)
  4. Kéo danh sách SaleOrders (Đơn hàng)
"""

import asyncio
import sys
import time
from pathlib import Path

# Thêm đường dẫn module
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "chatbot" / "server"))

from domains.amis.client import AmisClient
from domains.amis.config import load_amis_config


async def run_live_crm_test():
    print("=" * 75)
    print("🚀 BẮT ĐẦU KIỂM TRA GỌI TRỰC TIẾP TỚI MISA AMIS CRM OPEN API V2")
    print("=" * 75)

    cfg = load_amis_config()
    print("📋 Cấu hình kết nối:")
    print(f"  • Base URL      : {cfg.base_url}")
    print(f"  • Client ID     : {cfg.client_id}")
    print(f"  • Client Secret : {'ĐÃ CẤU HÌNH (' + str(len(cfg.client_secret)) + ' ký tự)' if cfg.client_secret else 'CHƯA CẤU HÌNH'}")
    print("-" * 75)

    if not cfg.credentials_configured:
        print("❌ LỖI: Chưa cấu hình AMIS_CLIENT_SECRET trong file .env!")
        return

    t_start = time.perf_counter()

    try:
        async with AmisClient(cfg) as client:
            # 1. Xác thực
            print("⏳ 1. Đang gửi yêu cầu xác thực tới MISA AMIS (/Account)...")
            t0 = time.perf_counter()
            token = await client._authenticate()
            ms_auth = round((time.perf_counter() - t0) * 1000, 1)
            print(f"✅ 1. XÁC THỰC THÀNH CÔNG ({ms_auth}ms)!")
            print(f"     JWT Token: {token[:25]}...{token[-15:]}\n")

            # 2. Products
            print("⏳ 2. Đang kéo danh mục Sản phẩm (/Products)...")
            t0 = time.perf_counter()
            products = await client.fetch_all("products")
            ms_prod = round((time.perf_counter() - t0) * 1000, 1)
            print(f"✅ 2. KÉO SẢN PHẨM THÀNH CÔNG: Đã tải {len(products):,} sản phẩm từ CRM ({ms_prod}ms).")
            if products:
                sample_p = products[0]
                p_name = sample_p.get("inventory_item_name") or sample_p.get("product_name") or sample_p.get("name", "N/A")
                p_code = sample_p.get("inventory_item_code") or sample_p.get("code", "N/A")
                print(f"     Mẫu SKU: [{p_code}] {p_name}\n")

            # 3. Customers
            print("⏳ 3. Đang kéo danh bạ Đại lý & Khách hàng (/Customers)...")
            t0 = time.perf_counter()
            customers = await client.fetch_all("customers")
            ms_cust = round((time.perf_counter() - t0) * 1000, 1)
            print(f"✅ 3. KÉO ĐẠI LÝ / KHÁCH HÀNG THÀNH CÔNG: Đã tải {len(customers):,} đại lý từ CRM ({ms_cust}ms).")
            if customers:
                sample_c = customers[0]
                c_name = sample_c.get("account_name") or sample_c.get("customer_name", "N/A")
                c_addr = sample_c.get("billing_address") or sample_c.get("address", "N/A")
                print(f"     Mẫu Đại lý: {c_name} - {str(c_addr)[:50]}...\n")

            # 4. Sale Orders
            print("⏳ 4. Đang kéo danh sách Đơn hàng (/SaleOrders)...")
            t0 = time.perf_counter()
            orders = await client.fetch_all("sale_orders")
            ms_ord = round((time.perf_counter() - t0) * 1000, 1)
            print(f"✅ 4. KÉO ĐƠN HÀNG THÀNH CÔNG: Đã tải {len(orders):,} đơn hàng từ CRM ({ms_ord}ms).")
            if orders:
                sample_o = orders[0]
                o_no = sample_o.get("order_no") or sample_o.get("order_code", "N/A")
                o_status = sample_o.get("status") or sample_o.get("order_status", "N/A")
                print(f"     Mẫu Đơn hàng: Mã #{o_no} - Trạng thái: {o_status}\n")

        total_ms = round((time.perf_counter() - t_start) * 1000, 1)
        print("=" * 75)
        print(f"🎉 TẤT CẢ API MISA AMIS CRM ĐÃ ĐƯỢC GỌI VÀ PHẢN HỒI THÀNH CÔNG 100%!")
        print(f"⏱️ Tổng thời gian thực thi: {total_ms}ms")
        print("=" * 75)

    except Exception as e:
        print(f"❌ LỖI TRONG QUÁ TRÌNH GỌI API MISA: {e}")


if __name__ == "__main__":
    asyncio.run(run_live_crm_test())
