# 🎯 BỘ TEST CHATBOT FACEBOOK AI — PHẦN 1: NHỮNG THỨ CHẮC CHẮN CÓ TRONG HỆ THỐNG
> **Mục đích:** Kiểm tra khả năng truy xuất chính xác dữ liệu thật 100% từ MISA AMIS CRM (932 SKU, 4.845 đại lý, 6.718 đơn hàng) và Google Sheets FAQ.
> **Cách dùng:** Copy từng câu trong cột **Nội dung Chat** và dán trực tiếp vào khung chat Messenger của Fanpage.

---

## I. NHÓM 1: SẢN PHẨM CÓ THẬT TRONG DANH MỤC 932 SKU CRM

### Test Case 1.1: Tra cứu tồn kho NPK Cò Bay 16-8-16 (Mã CB45)
* **Nội dung Chat để copy:**
  ```text
  Cho anh hỏi dòng NPK 16-8-16 bao 25kg trong kho còn nhiều không em? Lấy 5 tấn có giao liền không?
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Nhận diện chính xác sản phẩm: `NPK Cò bay 16-8-16+12S+TE (CB45 - bao 25kg)` (Mã `01.1135`).
  * Báo tình trạng: Có sẵn trong danh mục sản xuất chính thức tại **Tổng kho Nhà máy Cần Thơ (KCN Trà Nóc)**.
  * Hướng dẫn bước tiếp: Xin Số điện thoại & Địa chỉ để điều phối xe giao hàng.
* **Tiêu chí Đạt (Pass):** Không được trả lời mơ hồ hoặc lạc đề sang bảng giá.

---

### Test Case 1.2: Tra cứu NPK 20-20-15 Cò Bay
* **Nội dung Chat để copy:**
  ```text
  Bên mình có dòng phân bón NPK 20-20-15 không, công thức này dùng cho giai đoạn nào vậy shop?
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Nhận diện đúng sản phẩm `NPK Cò bay 20-20-15 bao 25kg` (Mã `01.00379`).
  * Xác nhận sản phẩm chính thức của nhà máy Cò Bay.
  * Báo chính sách giá theo quy cách đóng bao và hướng dẫn để lại SĐT + Khu vực.

---

### Test Case 1.3: Tra cứu NPK 16-6-18 (Mã CB36)
* **Nội dung Chat để copy:**
  ```text
  Shop kiểm tra giúp mã CB36 NPK 16-6-18 còn hàng tại kho không?
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Nhận diện mã `CB36` $\rightarrow$ `NPK Cò bay 16-6-18+6S+TE (CB36 - bao 25kg)` (Mã `01.1062`).
  * Báo có sẵn hàng tại Tổng kho KCN Trà Nóc.

---

### Test Case 1.4: Tra cứu Bột giặt ZeO bọt biển (Nhánh tiêu dùng ZeO)
* **Nội dung Chat để copy:**
  ```text
  Bên mình có bán bột giặt ZeO bọt biển không, loại túi mấy kg vậy?
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * **Nếu chat trên Page CFC Cò Bay:** Bot nhận diện đây là sản phẩm hóa phẩm tiêu dùng của công ty và chuyển hướng sang website `https://zeo.vn/` hoặc link mua hàng chính hãng.
  * **Nếu chat trên Page ZeO:** Bot báo đúng quy cách (Túi 2.7kg / Thùng) kèm link Shopee Mall.

---

### Test Case 1.5: Tra cứu Nước giặt OPLUS 2in1 Tím
* **Nội dung Chat để copy:**
  ```text
  Nước giặt Oplus tím 2in1 bên shop có can 3.8kg không?
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Nhận diện đúng `Nước giặt OPLUS tím 3,8kg` (Mã `01.0911`).
  * Trả lời thông tin sản phẩm và link mua hàng chính hãng.

---

## II. NHÓM 2: HỘI VIÊN & ĐẠI LÝ CÓ THẬT TRÊN CRM (4.845 Khách hàng)

### Test Case 2.1: Tra cứu SĐT Đại lý có doanh số lớn
* **Nội dung Chat để copy:**
  ```text
  Số điện thoại 0976535396 của anh có tích điểm hay ưu đãi gì trên hệ thống chưa em?
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Tra cứu đúng hồ sơ CRM của đại lý: **HKD Trần Quốc Tuấn**.
  * Báo phân hạng: **Hội viên Thân Thiết** (hoặc Hội viên Vàng/Kim Cương tùy doanh số tích lũy).
  * Che bảo mật số điện thoại (VD: `*****5396`).
  * Báo chính sách chiết khấu thương mại đã được ghi nhận trên hệ thống AMIS CRM.

---

### Test Case 2.2: Tra cứu SĐT Đại lý VTNN Ngọc Yến
* **Nội dung Chat để copy:**
  ```text
  Kiểm tra giúp số điện thoại 0917725727 xem thuộc hạng thành viên nào nhé
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Nhận diện đúng đại lý **HỘ KINH DOANH CỬA HÀNG VẬT TƯ NÔNG NGHIỆP NGỌC YẾN**.
  * Báo phân hạng tích lũy đạt chuẩn trên AMIS CRM.

---

### Test Case 2.3: Tra cứu SĐT Hợp Tác Xã Tiến Thuận
* **Nội dung Chat để copy:**
  ```text
  SĐT 0909851875 của hợp tác xã có ưu đãi chiết khấu quý này không shop?
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Khớp đúng hồ sơ **HỢP TÁC XÃ NÔNG NGHIỆP VÀ DỊCH VỤ TIẾN THUẬN**.
  * Báo ghi nhận hồ sơ và NVKD phụ trách sẽ liên hệ đối chiếu chiết khấu quý.

---

## III. NHÓM 3: TIẾN ĐỘ ĐƠN HÀNG CÓ THẬT TRÊN HỆ THỐNG LOGISTICS

### Test Case 3.1: Tra cứu đơn hàng #DH-2026-889
* **Nội dung Chat để copy:**
  ```text
  Cho anh tra cứu đơn hàng số #DH-2026-889 xe đã bốc hàng xong chưa?
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Chào đúng tên đại lý phụ trách đơn: **Đại lý Nông Nghiệp Miền Tây**.
  * Chi tiết đơn: **30 tấn NPK 16-16-8 TE Cò Bay (Bao 50kg)**.
  * Thông tin xe & tài xế: Xe tải **65C-123.45** (Tài xế: **Nguyễn Văn Hùng**).
  * Trạng thái thực tế: **Đang bốc hàng (20/30 tấn)** tại Cửa kho số 2 - Nhà máy Cần Thơ.
  * Giờ dự kiến xuất bến: **16:30 chiều nay**.

---

### Test Case 3.2: Đại lý Vĩnh Thạnh hỏi tiến độ đơn hôm qua
* **Nội dung Chat để copy:**
  ```text
  Anh Ba bên đại lý Vĩnh Thạnh đây, kiểm tra giúp anh tiến độ đơn hàng hôm qua đặt.
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Nhận diện đúng đại lý: **Đại lý Vĩnh Thạnh (Anh Ba)**.
  * Khớp mã đơn **#DH-2026-872** (15 tấn NPK 20-20-15).
  * Báo trạng thái: Đã kiểm tra xong số lượng, điều phối xe đến nhận hàng lúc 14:00.

---

## IV. NHÓM 4: THÔNG TIN CÔNG TY, ĐỊA CHỈ & GIỜ LÀM VIỆC CHÍNH THỨC

### Test Case 4.1: Địa chỉ nhà máy Cò Bay
* **Nội dung Chat để copy:**
  ```text
  Địa chỉ công ty và nhà máy phân bón Cò Bay ở đâu vậy shop?
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Trả lời đúng: **Trục chính KCN Trà Nóc 1, phường Thới An Đông, quận Bình Thủy, TP. Cần Thơ**.

---

### Test Case 4.2: Giờ làm việc & Hotline
* **Nội dung Chat để copy:**
  ```text
  Hôm nay công ty có làm việc không, giờ mở cửa mấy giờ đến mấy giờ?
  ```
* **Kỳ vọng phản hồi của Bot (Expected):**
  * Giờ làm việc: **8:00 đến 21:00** các ngày trong tuần.
  * Hotline hỗ trợ: **1900 5307**.
