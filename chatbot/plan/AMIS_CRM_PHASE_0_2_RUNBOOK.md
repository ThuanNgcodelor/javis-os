# Runbook triển khai AMIS CRM - Phase 0 đến Phase 2

**Ngày cập nhật:** 26/08/2026  
**Phạm vi:** Chuẩn hóa dữ liệu, Open API chỉ đọc và snapshot Redis công khai  
**Không thuộc phạm vi:** Kết nối vào câu trả lời chatbot, tra đơn hàng của khách, tồn kho, điểm tích lũy và công khai giá

## 1. Trạng thái triển khai

| Phase | Phần code | Phần live AMIS | Trạng thái |
| --- | --- | --- | --- |
| Phase 0 | Policy, field allowlist, gate dữ liệu và CLI status | Chưa tạo/xác nhận field public, chưa có danh sách được duyệt | Code xong, chờ nghiệp vụ |
| Phase 1 | Client token, retry, pagination, contract validation và audit không PII | Chưa có `AMIS_CLIENT_SECRET` | Code xong, chưa gọi live |
| Phase 2 | Public projection, Redis transaction, GEO index và workflow n8n inactive | Chưa audit live, chưa push workflow | Code xong, chờ gate Phase 0-1 |

Workflow CFC production không được push tự động. Người vận hành tự push sau khi audit live đạt.

## 2. Quy tắc dữ liệu bắt buộc

Khách có hóa đơn chỉ là **ứng viên đã mua hàng**, chưa phải điểm bán công khai.

Một Customer chỉ vào danh bạ chatbot khi thỏa toàn bộ:

```text
inactive == false
AND được duyệt công khai bằng field AMIS hoặc allowlist pilot
AND có địa chỉ công khai
AND tồn tại SaleOrder trong thời hạn cho phép
AND SaleOrder.is_invoiced == true
AND SaleOrder.invoiced_amount > 0
AND SaleOrder.revenue_status thuộc danh sách được duyệt
AND trạng thái đơn không bị hủy/từ chối
AND hàng hóa đơn hàng map được sang thương hiệu ZeO hoặc CFC
```

Chatbot không nhận raw Customer, SaleOrder hoặc Product. Dữ liệu thô được project ngay trong FastAPI và không ghi vào Redis public.

## 3. Việc phải làm trong AMIS - Phase 0 live

Tạo hoặc xác nhận ba field Customer sau và ghi lại **mã field API thực tế**:

| Tên hiển thị | Kiểu | Ý nghĩa |
| --- | --- | --- |
| Cho phép hiển thị trên chatbot | Tích chọn | Owner nghiệp vụ duyệt công khai |
| Điện thoại điểm bán công khai | Một dòng | Không dùng số cá nhân mặc định |
| Địa chỉ điểm bán công khai | Nhiều dòng | Không mặc định dùng địa chỉ hóa đơn |

Sau đó:

1. Vào **Đơn hàng**, lọc `Đã xuất hóa đơn = Có` và `Giá trị đã xuất hóa đơn > 0`.
2. Chỉ giữ trạng thái ghi doanh số được nghiệp vụ chấp nhận; mặc định hiện tại là `Đã ghi`.
3. Đối chiếu tối thiểu 10 khách với tab Hóa đơn và Hàng hóa đơn hàng.
4. Duyệt thử ít nhất một điểm bán ZeO và một điểm bán CFC.
5. Kiểm tra địa chỉ, số công khai và tọa độ là vị trí bán thực tế.

Không bật fallback từ `billing_address` hoặc `office_tel` trước khi có phê duyệt bằng văn bản/nghiệp vụ.

## 4. Cấu hình an toàn - Phase 1

Secret chỉ đặt trong environment, không nhập vào `settings.json`, workflow TypeScript hoặc Git.

```bash
cd /Users/hyden/Documents/David-nguyen/javis-os
export AMIS_CLIENT_ID=JavisCFCChatbot
read -s AMIS_CLIENT_SECRET
export AMIS_CLIENT_SECRET

# Thay bằng mã field thực tế trong AMIS Developer > API > Fields
export AMIS_PUBLIC_APPROVAL_FIELD=custom_fieldXX
export AMIS_PUBLIC_PHONE_FIELD=custom_fieldYY
export AMIS_PUBLIC_ADDRESS_FIELD=custom_fieldZZ
```

Kiểm tra cấu hình, không gọi mạng:

```bash
.venv/bin/python chatbot/server/scripts/amis_crm_sync.py status
```

Audit live chỉ đọc, chỉ in số lượng và lý do loại; không in tên, số điện thoại hoặc raw JSON:

```bash
.venv/bin/python chatbot/server/scripts/amis_crm_sync.py audit
```

Gate Phase 1 đạt khi:

- API đọc đủ pagination Products, Customers và SaleOrders.
- Số liệu mẫu khớp giao diện AMIS.
- Có ít nhất một Product và một điểm bán công khai hợp lệ.
- Không có giá, hóa đơn, công nợ hoặc mã số thuế trong projection.

## 5. Đồng bộ snapshot - Phase 2

Chạy thủ công trước:

```bash
.venv/bin/python chatbot/server/scripts/amis_crm_sync.py sync
```

Các key được ghi atomically trong một Redis transaction:

```text
amis:public:products:active
amis:public:sales-locations:active
amis:public:sales-locations:geo
amis:public:sync:last-success
```

Nếu số Product/điểm bán thấp hơn gate, sync trả lỗi và giữ snapshot tốt trước đó.

Workflow local:

```text
workflows/local-n8n/amis_crm_public_sync.workflow.ts
```

Workflow đang `active: false`, chạy mỗi 30 phút sau khi được bật. Nó chỉ gọi endpoint nội bộ và nhận metrics, không nhận raw CRM record.

Validate và push sau khi người vận hành duyệt:

```bash
npx --yes n8nac skills validate workflows/local-n8n/amis_crm_public_sync.workflow.ts
npx --yes n8nac push workflows/local-n8n/amis_crm_public_sync.workflow.ts --verify
```

Không push trước khi audit live đạt và credential/environment đã được cấu hình trên process FastAPI/n8n.

## 6. Hợp đồng không công khai giá

Public Product chỉ chứa:

```text
product_code, product_name, brand, brand_scope, product_category,
usage_unit, description, sale_description, avatar, search_keywords, source
```

Projection từ chối các field như `price`, `unit_price`, `purchased_price`, `unit_cost`, `invoiced_amount`, `tax`, `discount`, `debt` và dữ liệu định danh nhạy cảm.

Phase 0-2 chưa thay đổi các route giá Shopee/Web hiện tại trong `chat_pipeline.py`. Việc chatbot ngừng trả giá trên mọi nguồn là phase policy tiếp theo và phải có regression test riêng.
