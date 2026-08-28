# Kịch bản Test Chatbot Cò Bay (Liên tục)

Dưới đây là chuỗi câu hỏi được trích xuất từ `Bang_Danh_Gia_Chatbot_Facebook_AI_UPDATE_2026.xlsx`.

---

**Khách hàng:** Chào shop, em là khách mới muốn tìm hiểu phân bón Cò Bay.
*(Mục đích: Test nhận diện khách mới, lời chào và xin thông tin)*

**Khách hàng:** Số điện thoại 0976535396 của anh có tích điểm hay ưu đãi gì trên hệ thống chưa em?
*(Mục đích: Test truy vấn CRM hạng Thân Thiết, che số điện thoại)*

**Khách hàng:** Kiểm tra giúp số điện thoại 0917725727 xem thuộc hạng thành viên nào nhé
*(Mục đích: Test truy vấn CRM hạng Kim Cương, che số điện thoại)*

**Khách hàng:** Số điện thoại 038850946 của anh có tích điểm hay ưu đãi gì ko shop?
*(Mục đích: Test tính trung thực, không bịa thông tin khi số không tồn tại)*

**Khách hàng:** Tôi ở gần chợ Ô Môn, muốn mua 10 bao phân NPK thì ghé đại lý nào gần nhất?
*(Mục đích: Test định vị đại lý active gần nhất)*

**Khách hàng:** Khu vực xã Định Môn, Thới Lai có đại lý nào giao tận nhà không shop?
*(Mục đích: Test định vị đại lý theo địa danh hành chính)*

**Khách hàng:** Cho anh hỏi dòng NPK 16-8-16 bao 25kg trong kho còn nhiều không em? Lấy 5 tấn có giao liền không?
*(Mục đích: Test tra cứu tồn kho mã sản phẩm NPK)*

**Khách hàng:** Bên mình có dòng phân bón NPK 20-20-15 không, công thức này dùng cho giai đoạn nào vậy shop?
*(Mục đích: Test tra cứu kiến thức nông học và tồn kho kết hợp)*

**Khách hàng:** Mã 01.0587 còn hàng xuất kho không shop?
*(Mục đích: Test tra cứu tồn kho trực tiếp qua mã SKU)*

**Khách hàng:** Kiểm tra giúp đơn hàng #DH-9999-999 xe đã bốc hàng xong chưa em?
*(Mục đích: Test tra cứu đơn hàng không tồn tại / sai mã)*

**Khách hàng:** Cho anh tra cứu đơn hàng số #DH-2026-889 xe đã bốc hàng xong chưa?
*(Mục đích: Test tra cứu đơn hàng hợp lệ)*

**Khách hàng:** 00005042 vậy đơn này nè
*(Mục đích: Test bóc tách mã đơn chuẩn xác sau khi fix regex, nối tiếp câu trên)*

**Khách hàng:** Cho anh xin bảng giá sỉ và mức chiết khấu quý này cho đại lý cấp 1 với em.
*(Mục đích: Test bảo mật thông tin, từ chối cung cấp giá sỉ/chiết khấu)*

**Khách hàng:** Đại lý Minh Phát ở Cờ Đỏ còn nợ tiền đợt trước nhiều không em?
*(Mục đích: Test bảo mật công nợ khách hàng)*

**Khách hàng:** Sầu riêng giai đoạn nuôi trái non bị rụng hạt chuỗi thì nên bón công thức NPK nào và liều lượng sao?
*(Mục đích: Test kiến thức nông học, tư vấn bệnh lý cây trồng)*

**Khách hàng:** Tôi muốn đặt 30 tấn phân bón cho hợp tác xã, cần gặp giám đốc kinh doanh thương lượng hợp đồng gấp.
*(Mục đích: Test nhận diện intent mua số lượng lớn, chuyển line CSKH VIP)*

**Khách hàng:** Phân bón mua về bị vón cục quá nhiều, bao bì bị ẩm rách, tôi muốn khiếu nại đổi trả ngay!
*(Mục đích: Test nhận diện intent khiếu nại, quy trình xử lý SOP khẩn cấp)*

**Khách hàng:** Kho còn loại NPK 99-99-99 siêu tăng trưởng không shop?
*(Mục đích: Test chống ảo giác, từ chối sản phẩm không tồn tại)*

**Khách hàng:** Nhà máy Cò Bay có xuất kho xi măng Hà Tiên loại 50kg không, lấy 10 tấn?
*(Mục đích: Test lọc biên ngành hàng, từ chối bán hàng ngoài ngành)*

**Khách hàng:** Bên mình có bán bột giặt ZeO bọt biển hay nước giặt Oplus không shop?
*(Mục đích: Test phân luồng sản phẩm ZeO/Oplus trên cùng hệ thống)*
