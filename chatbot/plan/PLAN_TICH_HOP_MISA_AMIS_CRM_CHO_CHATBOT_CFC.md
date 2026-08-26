# Kế hoạch triển khai AMIS CRM cho Chatbot CFC

**Ngày cập nhật:** 26/08/2026  
**Trạng thái:** Phase 0-2 đã triển khai code cục bộ, chưa gọi AMIS live, chưa deploy n8n  
**Phạm vi tổng thể:** Danh mục sản phẩm công khai không giá, tìm điểm bán và tra điểm tích lũy của chính khách  
**Phạm vi đang triển khai:** Phase 0-2 dùng Products + Customers + SaleOrders để tạo snapshot công khai  
**Runbook thực thi:** `chatbot/plan/AMIS_CRM_PHASE_0_2_RUNBOOK.md`

---

## 1. Quyết định đã chốt

### 1.1. Chỉ triển khai hai capability

| Capability | Đối tượng sử dụng | Kết quả được trả |
| --- | --- | --- |
| `sales_location_search` | Mọi khách Facebook | Tên điểm bán, địa chỉ công khai, điện thoại công khai và khoảng cách nếu đủ tọa độ |
| `loyalty_points_lookup` | Chỉ khách đã xác minh đúng tài khoản của mình | Duy nhất `total_score` hiện tại của chính khách |

### 1.2. Gộp Đại lý và Nhà phân phối

Chatbot không tách hai chức năng “tìm đại lý” và “tìm nhà phân phối”. Mọi cách hỏi sau đều đi vào `sales_location_search`:

- Đại lý gần nhất.
- Nhà phân phối gần nhất.
- Điểm bán Cò Bay.
- Chỗ mua phân CFC/Oplus.
- Cửa hàng có bán gần khu vực của tôi.

Nguồn điểm bán hợp lệ gồm:

- Customer có ít nhất một SaleOrder `is_invoiced=true`, `invoiced_amount>0` và trạng thái ghi doanh số được duyệt.
- Hàng hóa đơn hàng xác định được điểm bán thuộc nhánh ZeO hay CFC.
- Đồng thời `inactive = false`.
- Đồng thời bản ghi được doanh nghiệp duyệt cho phép công khai trên chatbot.

`account_type`/`is_distributor` có thể dùng làm tín hiệu nghiệp vụ bổ sung, nhưng không thay thế điều kiện hóa đơn và phê duyệt công khai.

### 1.3. Loại bỏ khỏi sản phẩm

- Không xây capability hỏi Công nợ.
- Không xây capability xem dữ liệu của khách hàng khác.
- Không chuyển hai nhóm này sang handoff trong kế hoạch hiện tại.
- Không đưa các field tương ứng vào tool/schema mà AI có thể gọi.

Vẫn phải giữ cơ chế **allowlist mặc định** ở backend. Đây không phải một tính năng trả lời khách, mà là chốt kỹ thuật để dữ liệu ngoài hai capability không thể lọt vào prompt hoặc response.

### 1.4. Quy tắc tra điểm tích lũy

`total_score` chỉ được trả khi hệ thống xác minh khách đang sở hữu đúng tài khoản CRM.

Phương án production được duyệt trong kế hoạch:

1. Khách yêu cầu xem điểm.
2. Bot hỏi số điện thoại đã đăng ký.
3. Hệ thống tìm đúng một Customer theo số đã chuẩn hóa.
4. Gửi OTP đến chính số điện thoại đó.
5. Khách nhập đúng OTP trong thời hạn cho phép.
6. Backend chỉ lấy `total_score` của Customer đã xác minh.
7. Phiên xác minh hết hiệu lực sau 10 phút hoặc khi đổi sender/session.

Chỉ nhập đúng số điện thoại hoặc mã khách hàng chưa đủ an toàn, vì người khác có thể biết các thông tin này. Nếu chưa có kênh gửi OTP, chức năng tra điểm chỉ được pilot nội bộ, chưa bật công khai.

---

## 2. Dữ liệu AMIS đã xác minh

### 2.1. Tình trạng dữ liệu

- CRM hiện lọc được khoảng **231 bản ghi** đại lý có thông tin địa chỉ theo điều kiện đang sử dụng trên giao diện.
- Có **2 Nhà phân phối** riêng.
- Số lượng bản ghi đủ cả `shipping_long` và `shipping_lat` chưa được thống kê.
- Cần đối chiếu API vì bộ lọc đã lưu trên giao diện không tự động trở thành bộ lọc của Open API.

### 2.2. Field dùng cho điểm bán

| Vai trò | Field AMIS | Cách dùng |
| --- | --- | --- |
| Mã nội bộ | `account_number` | Đối chiếu và deduplicate, không cần trả khách |
| Tên điểm bán | `account_name` | Có thể trả khách sau khi được duyệt |
| Phân loại | `account_type` | Chọn KH001/KH002 |
| NPP | `is_distributor` | Gộp 2 NPP vào cùng danh bạ |
| Ngừng theo dõi | `inactive` | Loại bản ghi `true` |
| Điện thoại | `office_tel` | Chỉ trả nếu được duyệt là số công khai |
| Địa chỉ giao hàng | `shipping_address` | Nguồn địa chỉ ưu tiên nếu là vị trí bán thực tế |
| Tỉnh giao hàng | `shipping_province` | Lọc khu vực |
| Huyện giao hàng | `shipping_district` | Lọc khu vực |
| Xã giao hàng | `shipping_ward` | Lọc khu vực |
| Kinh độ giao hàng | `shipping_long` | Tính khoảng cách sau khi parse và validate |
| Vĩ độ giao hàng | `shipping_lat` | Tính khoảng cách sau khi parse và validate |
| Địa chỉ hóa đơn | `billing_address` | Chỉ fallback nếu nghiệp vụ xác nhận đây là điểm bán |
| Tỉnh/huyện/xã hóa đơn | `billing_province`, `billing_district`, `billing_ward` | Fallback có kiểm soát |
| Thời điểm sửa | `modified_date` | Theo dõi freshness |

`shipping_long` và `shipping_lat` là trường chuỗi, nên phải chuyển sang số và kiểm tra giới hạn tọa độ trước khi dùng.

### 2.3. Field dùng cho tích điểm

| Vai trò | Field AMIS | Quy tắc |
| --- | --- | --- |
| Điện thoại đăng ký | `office_tel` | Dùng tìm tài khoản và gửi OTP |
| Mã khách hàng | `account_number` | Khóa đối chiếu nội bộ |
| Điểm hiện tại | `total_score` | Field duy nhất được trả sau xác minh |
| Trạng thái | `inactive` | Không trả điểm cho tài khoản đã ngừng theo dõi nếu chưa có quy định khác |
| Ngày sửa | `modified_date` | Ghi nhận thời điểm dữ liệu |

### 2.4. Field không được nạp vào công cụ AI

Các field sau không thuộc schema của hai capability và không được đưa vào Redis public index, QueryPlan result hoặc prompt:

```text
debt
debt_limit
debt_limit_type
bank_account
identification
passport_number
portal_username
owner_name
related_users
tax_code
order_sales
average_order_value
list_product
list_product_name
custom_field14
custom_field15
custom_field16
custom_field17
```

`is_public` trong AMIS nghĩa là dùng chung nội bộ, không phải đồng ý công khai trên Facebook. Không dùng field này làm consent cho chatbot.

### 2.5. Field nên bổ sung trong CRM

Khuyến nghị thêm các field riêng:

| Field đề xuất | Kiểu | Mục đích |
| --- | --- | --- |
| `Cho phép hiển thị trên chatbot` | Tích chọn | Chốt bản ghi được đưa vào danh bạ công khai |
| `Điện thoại công khai` | Một dòng | Tách số bán hàng khỏi số cá nhân |
| `Địa chỉ điểm bán công khai` | Nhiều dòng | Không phụ thuộc địa chỉ hóa đơn |
| `Phạm vi giao hàng` | Nhiều dòng/danh sách | Chỉ trả “giao tận nơi” khi có dữ liệu |
| `Giờ làm việc` | Một dòng | Thông tin phục vụ khách |

Nếu chưa thể thêm field, dùng một allowlist riêng do nghiệp vụ phê duyệt. Không mặc định công khai toàn bộ 231 bản ghi.

---

## 3. Trạng thái kết nối hiện tại

### 3.1. AppID

- AppID đã đặt: `JavisCFCChatbot`.
- AppID chính là `client_id` khi lấy token.
- `client_secret` do AMIS cấp sau khi kết nối.
- Secret chỉ được nhập vào n8n Credential hoặc secret store, không ghi trong workflow TypeScript, Git, log hoặc ảnh chụp.

### 3.2. Webhook

URL đã nhập trên AMIS:

```text
https://n8n.dinhduongcantho.io.vn/webhook/amis-crm-events
```

Kết quả kiểm tra ngày 26/08/2026:

- Không có path `amis-crm-events` trong source hiện tại.
- Không có workflow AMIS trong các workflow mà `n8nac` đang quản lý trên n8n live.
- Nhập URL vào AMIS không tự tạo Webhook node trên n8n.

Quyết định triển khai:

- Phase đầu **không phụ thuộc webhook**.
- Dùng Manual Trigger và Schedule Trigger để kéo Customers API.
- Để trống/xóa webhook trên AMIS nếu giao diện cho phép, cho đến khi có tài liệu rõ về payload và xác thực webhook.
- Chỉ bật webhook ở phase sau nếu cần đồng bộ gần realtime và đã tạo workflow Active đúng path.

---

## 4. Kiến trúc mục tiêu

```text
AMIS Products + Customers + SaleOrders API
        |
        v
FastAPI AMIS read-only adapter + public projection
        |
        +--> Product catalog không giá
        +--> Điểm bán: hóa đơn hợp lệ + brand + public approval
        |
        v
Redis Public Product + Sales Location Snapshot + Geo Index
        |
        v
n8n Manual/Schedule chỉ kích hoạt sync và nhận metrics
        |
        +--> sales_location_search (public)
        +--> loyalty_points_lookup (verified session only)
        |
        v
Chat Pipeline + grounded response
```

### 4.1. Nguyên tắc

- `chat_pipeline.py` không gọi AMIS trực tiếp.
- Adapter AMIS chịu trách nhiệm token, pagination, timeout, retry và schema.
- AI không được tự chọn field CRM hoặc viết query tùy ý.
- AI chỉ gọi capability với input đã định nghĩa.
- Backend quyết định quyền, lọc field và truy vấn.
- AI chỉ diễn đạt kết quả đã được backend trả về.
- Mọi kết quả có `source=amis_crm` và `synced_at`/`fetched_at` trong debug trace.

### 4.2. Dữ liệu điểm bán trong Redis

Mỗi bản ghi chỉ chứa:

```text
location_id
account_number
display_name
location_type
public_phone
public_address
province
district
ward
longitude
latitude
modified_date
synced_at
```

Không lưu điểm tích lũy trong index công khai này.

### 4.3. Quy tắc tìm điểm bán

Điều kiện eligible:

```text
inactive == false
AND chatbot_public == true
AND (
  account_type contains KH001
  OR account_type contains KH002
  OR is_distributor == true
)
```

Thứ tự tìm:

1. Nếu khách gửi Live Location và điểm bán có tọa độ: xếp theo khoảng cách.
2. Nếu chỉ có địa chỉ chữ: ưu tiên khớp xã, sau đó huyện, sau đó tỉnh.
3. Nếu địa danh mơ hồ: hỏi lại tỉnh/huyện trước khi truy vấn.
4. Trả tối đa 3 điểm phù hợp.
5. Không đủ tọa độ thì dùng cụm “phù hợp trong khu vực”, không khẳng định “gần nhất”.
6. Không trả “giao tận nơi” nếu chưa có `Phạm vi giao hàng` đã xác minh.

### 4.4. Quy tắc tra điểm

Input capability:

```text
sender_id
registered_phone
otp_code
```

Backend phải:

- Chuẩn hóa số điện thoại Việt Nam.
- Không cho biết số điện thoại có tồn tại hay không trước khi OTP thành công.
- Rate limit yêu cầu OTP và số lần nhập sai.
- OTP dùng một lần và hết hạn ngắn.
- Ràng buộc OTP với `sender_id` và phiên hiện tại.
- Sau xác minh, chỉ lấy `total_score`.
- Không trả tên, doanh số, công nợ, lịch sử mua hoặc dữ liệu khác kèm theo.

---

## 5. Kế hoạch triển khai theo phase

## Phase 0 - Hoàn tất dữ liệu và chính sách

**Mục tiêu:** đủ điều kiện làm PoC, chưa nối chatbot.

### Công việc

- [ ] Thống kê trong 231 đại lý có bao nhiêu bản ghi đủ `shipping_lat` và `shipping_long`.
- [ ] Mở mẫu tối thiểu 10 bản ghi, xác nhận tọa độ là điểm bán thực tế.
- [ ] Chọn địa chỉ ưu tiên: giao hàng, hóa đơn hay field công khai mới.
- [ ] Xác định số điện thoại nào được công khai.
- [ ] Tạo field `Cho phép hiển thị trên chatbot` hoặc phê duyệt allowlist ngoài CRM.
- [ ] Kiểm tra 2 NPP có đủ địa chỉ/tọa độ.
- [ ] Xác nhận AppID kết nối thành công và lưu secret an toàn.
- [ ] Chọn nhà cung cấp OTP: SMS, Zalo hoặc kênh khác.
- [ ] Không bật webhook production trong phase này.

### Gate

- Có ít nhất một đại lý và một NPP mẫu đã được duyệt công khai.
- Biết tỷ lệ tọa độ đầy đủ.
- Có quyết định về OTP.
- Có owner nghiệp vụ duyệt danh bạ công khai.

## Phase 1 - PoC Open API chỉ đọc

**Mục tiêu:** chứng minh dữ liệu API khớp giao diện CRM.

### Công việc

- Lấy token từ `POST /api/v2/Account` bằng AppID và secret.
- Gọi `GET /api/v2/Customers` với page nhỏ trước.
- Gửi header `Clientid: JavisCFCChatbot` và Bearer token.
- Kiểm tra shape thực tế của `account_type`, `is_distributor`, tọa độ và `total_score`.
- Duyệt toàn bộ pagination; page bắt đầu từ 0, page size tối đa 100.
- Lọc KH001/KH002/NPP ở phía tích hợp, không dựa vào giao diện đã lưu.
- Đối chiếu ít nhất 10 đại lý, 2 NPP và 3 tài khoản có điểm.
- Che token, số điện thoại và dữ liệu ngoài allowlist trong logs.

### Test

- Credential hợp lệ, sai và hết hạn.
- Pagination trên 100 bản ghi.
- `account_type` là chuỗi, danh sách hoặc null.
- Tọa độ null, sai định dạng hoặc ngoài giới hạn.
- Bản ghi `inactive=true`.
- API timeout, 401, 403, 429 và 5xx.

### Gate

- API và giao diện khớp 100% trên bộ mẫu.
- Không ghi ngược dữ liệu lên AMIS.
- Không có secret hoặc field nhạy cảm trong log.

## Phase 2 - Đồng bộ danh bạ điểm bán

**Mục tiêu:** tạo nguồn tra cứu nhanh, chỉ chứa dữ liệu công khai.

### Công việc

- Tạo workflow n8n riêng cho AMIS Customer Sync.
- Manual Trigger trước, Schedule Trigger sau khi nghiệm thu.
- Fetch tất cả pages rồi normalize theo schema nội bộ.
- Áp dụng điều kiện eligible và field allowlist trước khi ghi Redis.
- Ghi snapshot atomically, không để dữ liệu nửa cũ nửa mới.
- Tạo Redis GEO index cho bản ghi đủ tọa độ.
- Tạo index theo tỉnh/huyện/xã cho bản ghi chưa đủ tọa độ.
- Vô hiệu hóa/xóa khỏi index khi Customer `inactive=true` hoặc không còn được duyệt public.
- Ghi metadata: số nguồn, số eligible, số có tọa độ, số lỗi và thời điểm sync.

### Gate

- Số lượng Redis đối chiếu đúng với tập CRM eligible.
- Không có field ngoài schema công khai trong Redis.
- Sync lại không tạo trùng.
- API lỗi không xóa snapshot tốt gần nhất.

## Phase 3 - Capability tìm điểm bán và memory hội thoại

**Mục tiêu:** khách hỏi tự nhiên và bot tìm đúng điểm bán.

### Công việc

- Thêm intent/capability `sales_location_search`.
- Gộp từ khóa đại lý, NPP, cửa hàng, điểm bán và chỗ mua.
- Trích xuất slot tỉnh, huyện, xã và tọa độ.
- Lưu task/slot trong session để xử lý nhiều lượt.
- Hỗ trợ Live Location.
- Gọi service tìm kiếm có cấu trúc, không dùng RAG FAQ để đoán địa chỉ.
- Trả tối đa 3 kết quả và giữ `location_id` cho câu hỏi “cái số 2”.
- Thêm source/freshness vào debug trace.

### Kịch bản nghiệm thu

1. “Tôi ở xã Định Môn, Thới Lai, chỗ nào bán gần nhất?”
2. “Có nhà phân phối Cò Bay gần Ô Môn không?”
3. “Đại lý cấp 1 gần tôi.” rồi gửi Live Location.
4. “Cái số 2 có số điện thoại không?”
5. Khách đổi từ Ô Môn sang Vĩnh Thạnh giữa hội thoại.
6. Địa danh trùng tên ở hai tỉnh.
7. Không có tọa độ nhưng có địa chỉ cùng huyện.
8. Không có điểm bán phù hợp.
9. Dữ liệu sync quá hạn hoặc AMIS lỗi.
10. Prompt yêu cầu in toàn bộ 231 khách hàng.

### Gate

- Không trả địa chỉ công ty thay cho điểm bán.
- Không hỏi lại địa điểm đã có trong context.
- Không bịa khoảng cách, số điện thoại hoặc giao hàng.
- Không lộ danh sách đầy đủ hoặc field ngoài allowlist.

## Phase 4 - Capability tra điểm của chính khách

**Mục tiêu:** trả đúng `total_score` sau xác minh.

### Công việc

- Tích hợp kênh OTP đã được duyệt.
- Thêm state machine: `awaiting_phone` -> `awaiting_otp` -> `verified`.
- Tách capability điểm khỏi FAQ/RAG và khỏi danh bạ public.
- Sau OTP, đọc `total_score` live từ AMIS hoặc secured cache rất ngắn.
- Gắn verification với sender/session, hết hạn sau 10 phút.
- Thêm rate limit và audit log không chứa OTP plaintext.
- Chỉ trả điểm, không trả profile CRM kèm theo.

### Kịch bản nghiệm thu

1. Khách hỏi điểm nhưng chưa gửi số.
2. Số đúng, OTP đúng.
3. Số đúng, OTP sai.
4. OTP hết hạn hoặc dùng lại.
5. Sender khác nhập OTP của sender trước.
6. Thử nhiều số điện thoại để dò tài khoản.
7. Hai Customer trùng số điện thoại.
8. Customer `inactive=true`.
9. `total_score` null hoặc AMIS lỗi.
10. Sau khi xem điểm, khách hỏi thêm dữ liệu ngoài phạm vi.

### Gate

- Không trả điểm trước xác minh.
- Không tiết lộ số có tồn tại trong CRM hay không.
- Chỉ trả điểm của Customer gắn với OTP vừa xác minh.
- Không có field CRM khác trong response hoặc trace công khai.

## Phase 5 - Dashboard, replay và pilot Facebook

- Dashboard hiển thị intent, capability, slots, source, freshness và policy decision.
- Không hiển thị secret, OTP, raw Customer JSON hoặc số điện thoại đầy đủ trong trace.
- Chạy unit, contract, integration và conversation replay.
- Pilot nội bộ capability điểm bán trước.
- Pilot OTP/điểm với nhóm tài khoản test riêng.
- Chỉ bật rộng khi đạt gate và có kill switch riêng cho từng capability.

---

## 6. File dự kiến tác động khi được duyệt code

| Khu vực | Vai trò dự kiến |
| --- | --- |
| `chatbot/server/integrations/amis/` | Client, schema và service AMIS read-only |
| `chatbot/server/query_understanding.py` | Intent và slot cho điểm bán/tích điểm |
| `chatbot/server/chat_pipeline.py` | Điều phối capability, context và policy |
| `chatbot/server/eval_conversation_replays.jsonl` | Replay cho điểm bán, OTP và privacy |
| `chatbot/server/tests/` | Unit, contract, integration và regression |
| `workflows/local-n8n/` | Workflow đồng bộ Customers riêng |
| n8n Credential/secret store | AppID và client secret, không nằm trong Git |
| Redis | Snapshot/index điểm bán và metadata sync |
| Dashboard debug | Source, freshness và policy decision |

Tên file/module cụ thể được chốt sau khi đọc lại ownership boundary trước Phase 1. Không sửa workflow chatbot production trong PoC API.

---

## 7. Testing gate tổng hợp

### Unit

- Mapping Customers -> SalesLocation.
- Filter KH001/KH002/NPP và `inactive`.
- Field allowlist.
- Chuẩn hóa địa chỉ Việt Nam.
- Parse và validate tọa độ.
- Tính khoảng cách.
- Chuẩn hóa số điện thoại.
- OTP state, expiry và rate limit.

### Contract

- Schema AMIS thực tế và field null.
- Phân trang.
- Token expiry.
- Duplicate customer/phone.
- Custom field thiếu hoặc đổi kiểu.

### Security

- Raw CRM record không đi vào LLM.
- Không có capability công nợ.
- Không có generic “query customer” tool.
- Không truy vấn tùy ý theo prompt người dùng.
- Prompt injection không vượt allowlist.
- Sender A không xem được điểm của sender B.
- Secret, OTP và PII được che trong logs.

### Conversation

- Paraphrase có dấu, không dấu, viết tắt và sai chính tả.
- Multi-turn và “cái số 2”.
- Live Location.
- Chuyển chủ đề rồi quay lại.
- API lỗi, cache stale và dữ liệu thiếu.

---

## 8. Definition of Done

### Tìm điểm bán

- Một capability xử lý chung đại lý và NPP.
- Chỉ dùng bản ghi eligible đã duyệt công khai.
- Tìm đúng theo vị trí và không bịa “gần nhất”.
- Trả tối đa 3 kết quả với nguồn/freshness trong trace.
- Không tiết lộ toàn bộ danh bạ hoặc field nội bộ.

### Tích điểm

- Chỉ trả `total_score` sau OTP thành công.
- Không cho biết tài khoản có tồn tại trước xác minh.
- Không trả điểm của người khác.
- Không kèm các dữ liệu ngoài phạm vi.
- Có rate limit, audit và kill switch.

### Vận hành

- Đồng bộ AMIS lỗi không phá snapshot tốt gần nhất.
- Có metrics số bản ghi nguồn/eligible/có tọa độ/lỗi.
- Bộ replay chạy qua pipeline thật hoặc staging tương đương production.
- Pilot Facebook đối chiếu trực tiếp với AMIS.
- Không deploy nếu chưa đạt từng gate.

---

## 9. Thứ tự phê duyệt

1. **Duyệt Phase 0:** chất lượng dữ liệu, public allowlist và OTP provider.
2. **Duyệt Phase 1:** PoC API chỉ đọc, chưa chạm chatbot.
3. **Duyệt Phase 2:** workflow đồng bộ Redis, chưa trả khách.
4. **Duyệt Phase 3:** bật thử tìm điểm bán cho sender test.
5. **Duyệt Phase 4:** bật thử OTP và điểm cho tài khoản test.
6. **Duyệt Phase 5:** pilot Facebook rồi mới cân nhắc production.

Mỗi phase phải có kết quả test và diff riêng. Không gộp deploy toàn bộ trong một lần.

---

## 10. Việc cần làm ngay trước khi code

1. Lọc CRM với KH001/KH002 và `inactive = false`.
2. Thêm điều kiện `shipping_long` và `shipping_lat` không trống, ghi lại số lượng.
3. Kiểm tra 10 đại lý và 2 NPP mẫu trên bản đồ.
4. Chốt cách đánh dấu bản ghi được phép công khai.
5. Chốt số điện thoại công khai và địa chỉ ưu tiên.
6. Chọn kênh OTP; nếu chưa có, hoãn capability tích điểm công khai.
7. Xác nhận `client_secret` đã được lưu an toàn.
8. Để webhook ngoài phạm vi Phase 1.
9. Sau khi hoàn thành, duyệt PoC API read-only.

---

## 11. Nguồn chính thức

- MISA - Thiết lập API: <https://helpcrm.misa.vn/kb/api/>
- MISA - Open API v2: <https://crmconnect.misa.vn/docs-v2/index.html>
- MISA - Quản lý danh sách Khách hàng: <https://helpcrm.misa.vn/kb/quan-ly-danh-sach-khach-hang/>
- MISA - Khai báo vị trí theo kinh độ/vĩ độ: <https://helpcrm.misa.vn/kb/khai-bao-cap-nhat-theo-doi-vi-tri-khach-hang-theo-kinh-do-vi-do/>
- MISA - Quyền chức năng trên AMIS CRM: <https://helpcrm.misa.vn/kb/quyen-chuc-nang-tren-amis-crm/>
