# 🤖 AGENTS.md — AI AGENT CONTEXT & OPERATIONAL GUIDELINES
<!-- AI-CONTEXT-ROOT: chatbot/ -->
<!-- LAST-UPDATED: 2026-08-28 -->

> **DÀNH CHO AI / CODEX / COPILOT TIẾP THEO KHI MỞ THƯ MỤC NÀY:**
> Bạn **BẮT BUỘC** phải đọc kỹ toàn bộ tài liệu này trước khi phân tích hoặc chỉnh sửa bất kỳ file nào trong `chatbot/`.
> Mọi thay đổi lớn về kiến trúc hoặc tính năng sau phiên làm việc của bạn phải được **cập nhật bổ sung vào file này** để bảo toàn ngữ cảnh (Context Continuity).

---

## 1. TỔNG QUAN HỆ THỐNG & NGHIỆP VỤ (SYSTEM CONTEXT)

Hệ thống `chatbot/` là **Nền tảng Quản trị & Trợ lý AI Bán hàng / CSKH Đa kênh (Enterprise Chatbot Engine)**, phục vụ đồng thời 2 thương hiệu:
1. **ZeO Vietnam**: Chăm sóc gia đình sinh học (Nước rửa chén, Nước giặt xả, Lau sàn sinh học ZeO, PANO, Oplus) -> Bán lẻ Shopee Mall + Tuyển đại lý/sỉ.
2. **CFC Cò Bay (Cần Thơ)**: Phân bón & Dinh dưỡng cây trồng nông nghiệp (NPK Cò Bay, Hữu cơ Cobanic, dinh dưỡng chuyên sâu cho Sầu riêng, Mít, Ổi, Lúa...) -> Tư vấn kỹ thuật nông học, tra cứu điểm hội viên & đơn hàng AMIS MISA CRM, kết nối đại lý địa phương.

### Kiến trúc Lõi (Hybrid Multi-Layer AI Architecture)
- **Fast-path (Deterministic & FSM)**: Dùng Regex & State Machine bóc tách SĐT, mã đơn, địa bàn với độ trễ <20ms, bảo toàn ngữ cảnh đa lượt.
- **RAG & Vector Search**: Redis Stack Vector Search (1024 dims BGE-M3 / Semantic Text Search) tra cứu FAQ, quy trình kỹ thuật từ Google Sheets & Markdown.
- **Single Brain AI Engine (`ai_engine.py`)**: Bộ não sinh câu trả lời tự nhiên, có Guardrails chống bịa đặt giá/chính sách, hỗ trợ Chit-chat tự nhiên.
- **CRM Integration (`domains/amis/`)**: Tích hợp trực tiếp MISA AMIS CRM (Đơn hàng, Tích điểm, Đại lý phân phối).
- **Control Center Dashboard (`static/`, `admin_routes.py`)**: UI quản trị Domain-Driven Design (DDD).

---

## 2. CÂY THƯ MỤC CHI TIẾT & CHỨC NĂNG TỪNG PHẦN

```
chatbot/
├── AGENTS.md                      # [FILE NÀY] Ngữ cảnh & Cẩm nang bắt buộc cho AI
├── README.md                      # Tài liệu tổng quan tính năng & hướng dẫn chạy
├── Bao_Cao_Doi_Chieu_Khach_Hang.md # Báo cáo phân tích đối soát dữ liệu khách hàng
├── PLAN_CAI_TIEN_CFC_...md       # Kế hoạch cải tiến chất lượng hội thoại
├── knowledge/                     # Kho tri thức tĩnh (Static Knowledge Base)
│   ├── cfc_faq.md                 # Bộ FAQ & kỹ thuật bón phân CFC Cò Bay
│   ├── zeo_faq.md                 # Bộ FAQ sản phẩm & chính sách ZeO
│   └── shopee_catalog.json        # Database cache sản phẩm Shopee Mall
├── plan/                          # Tài liệu kế hoạch & lộ trình phát triển
├── skills/                        # Các kỹ năng & tài liệu nghiệp vụ mở rộng
└── server/                        # Backend FastAPI Lõi
    ├── main.py                    # Khởi tạo FastAPI App, Background Workers & Lifecycle
    ├── settings.json              # File cấu hình (ĐÃ ĐƯỢC GITIGNORE - Không commit)
    ├── settings.example.json      # Bản mẫu cấu hình an toàn cho Git
    ├── chat_pipeline.py           # Pipeline xử lý hội thoại chính (FSM, Fast-path, Fallback)
    ├── ai_engine.py               # Module điều phối AI (Groq, OpenRouter, Gemini, Ollama)
    ├── cfc_semantic_planner.py    # Bộ phân tích Intent chuyên sâu cho nhãn hàng CFC
    ├── admin_routes.py            # Gateway Admin Router theo mô hình DDD
    ├── domains/                   # Các Domain nghiệp vụ độc lập (Domain-Driven Design)
    │   ├── amis/                  # Tích hợp MISA AMIS CRM (Live CRM, Orders, Loyalty)
    │   │   ├── live_crm.py        # Cache & tra cứu đơn hàng (chỉ lấy "Đã ghi"), hội viên
    │   │   └── ...
    │   ├── assistant/             # Trợ lý AI điều hành & Agent công cụ
    │   ├── customers/             # Quản lý khách hàng, hội thoại, Lead CRM & Export CSV
    │   ├── knowledge/             # Kho tri thức, Markdown & Google Sheets Live Hub
    │   ├── learning/              # Hàng đợi học (Learning Queue) & AI gợi ý FAQ
    │   ├── n8n/                   # Điều khiển Workflow n8n & file watching
    │   ├── rag_test/              # Kiểm thử Semantic Search & NLU evaluation
    │   ├── reports/               # Báo cáo điều hành kinh doanh & AI Insights
    │   ├── system/                # Trạng thái hệ thống, Settings, Health & Analytics
    │   └── common/                # Cấu hình chung, kết nối Redis, DB helpers
    ├── rag_search.py              # Tìm kiếm ngữ nghĩa RAG (Vector + BM25 + Intent Match)
    ├── embedder.py                # Sinh vector embedding (BGE-M3 / Fallback)
    ├── shopee_matcher.py          # Thuật toán so khớp thông minh sản phẩm Shopee Mall
    ├── telegram_notifier.py       # Module bắn thông báo Telegram (Lead nóng, Fallback)
    ├── ai_reporter.py             # Sinh báo cáo kinh doanh tự động qua AI
    ├── conversation_orchestrator.py # Quản lý trạng thái ngữ cảnh hội thoại đa lượt
    ├── conversation_store.py      # Lưu trữ & truy xuất session chat từ Redis
    ├── dialogue_router.py         # Định tuyến luồng hội thoại
    ├── document_ingestor.py       # Bóc tách, chunking & nạp tài liệu Markdown vào Redis
    ├── grounding_policy.py        # Chính sách kiểm soát chống ảo giác (Anti-Hallucination)
    ├── knowledge_sync.py          # Đồng bộ dữ liệu tri thức vào Redis Cache
    ├── message_idempotency.py     # Chống trùng lặp tin nhắn khi Fanpage gửi webhook lặp
    ├── nlu_shadow.py              # Chạy ngầm đánh giá NLU song song để cải tiến
    ├── query_understanding.py     # Chuẩn hóa tiếng Việt, tách từ lóng & thực thể
    ├── run_test_md_scenarios.py   # Tool chạy tự động 20 kịch bản kiểm thử trong test_script.md
    └── static/                    # Frontend Giao diện Quản trị Admin Dashboard (HTML/CSS/JS)
```

---

## 3. CÁCH CHUYỂN ĐỔI (SWITCH) GIỮA CLOUD API VÀ OLLAMA LOCAL

Hệ thống hỗ trợ chuyển đổi tức thì thông qua file `chatbot/server/settings.json` tại khối `"ai_providers"`:

```json
"ai_providers": {
    "execution_mode": "cloud",
    "preferred_provider": "groq",
    "gemini": {
        "api_key": "",
        "model": "gemini-2.0-flash"
    },
    "openrouter": {
        "api_key": "sk-or-v1-...",
        "model": "google/gemini-2.0-flash-exp:free"
    },
    "groq": {
        "api_key": "gsk_...",
        "model": "llama-3.3-70b-versatile"
    }
}
```

### Hướng Dẫn Switch Nhanh (Chỉ cần sửa 1 trường `execution_mode`):
1. **Chế độ 100% Cloud (Khuyên dùng khi test / chạy thật):**
   - Đổi `"execution_mode": "cloud"`
   - Bot sẽ **chỉ** gọi Cloud API (ưu tiên `preferred_provider` -> fallback sang các cloud còn lại). Hoàn toàn **ngắt kết nối** với Ollama Local để tránh bị kéo chậm/treo do hết RAM máy.
2. **Chế độ 100% Local (Chạy Offline không cần mạng / Bảo mật tuyệt đối):**
   - Đổi `"execution_mode": "local"`
   - Bot sẽ **chỉ** gọi Ollama Local (`qwen2.5:7b-instruct`). Không gửi bất kỳ byte dữ liệu nào ra Cloud.
3. **Chế độ Tự động (Auto Fallback):**
   - Đổi `"execution_mode": "auto"`
   - Thử Cloud trước, nếu lỗi toàn bộ Cloud sẽ tự động rớt về Local Ollama.

---

## 4. CÁC ĐỢT REFACTOR & FIX QUAN TRỌNG ĐÃ HOÀN THÀNH

1. **Bảo mật Git & API Key:**
   - Đã gỡ `chatbot/server/settings.json` khỏi Git tracking và thêm vào `.gitignore`.
2. **Tích hợp AMIS CRM (`domains/amis/live_crm.py`):**
   - Đã lọc triệt để `revenue_status == "Đã ghi"`, loại bỏ các đơn "Bản nháp" khi khách tra cứu.
   - Sửa thứ tự ưu tiên Regex nhận diện mã đơn trong `chat_pipeline.py` (ưu tiên `DHxxxx` và `0000xxxx` trước cụm từ "đơn hàng").
3. **Nâng cấp NLU & Xử lý số điện thoại:**
   - Bổ sung từ lóng `"ne", "day ne", "zalo"` vào regex bóc tách SĐT `_is_phone_only_submission`.
   - Khi khách cung cấp SĐT trong luồng tư vấn kỹ thuật bón phân, hệ thống giữ chặt ngữ cảnh `agronomy`, không bị nhảy nhầm sang tra cứu tích điểm (`loyalty_lookup`).
4. **Mở khóa Trí thông minh & Giao tiếp (Phase 1):**
   - Xóa bỏ rào cản "chặn AI khi không có facts" trong `reason_and_answer_cskh` (`ai_engine.py`).
   - Xóa bỏ câu thông báo lỗi cứng nhắc của CFC trong `chat_pipeline.py`.
   - Bổ sung Guardrail: Cho phép AI trò chuyện tự nhiên (Chit-chat) nhưng nếu khách hỏi giá/sản phẩm chưa có trong CSDL thì AI khéo léo xin SĐT chứ không tự bịa giá.
5. **Cơ chế Switch Mode Local / Cloud tức thì:**
   - Cập nhật `ai_engine.py` nhận diện `execution_mode: "cloud" | "local" | "auto"`.

---

## 5. NGUYÊN TẮC LÀM VIỆC DÀNH CHO CODEX / AI TIẾP THEO

- **Không phá vỡ Fast-path**: Không bỏ các hàm Regex nhận diện nhanh (như `_is_phone_only_submission`) vì đây là chốt chặn đảm bảo độ trễ <20ms cho khách hàng.
- **Bảo toàn Zero-Hallucination cho giá và tồn kho**: Khi bổ sung prompt, luôn nhắc nhở AI không được tự bịa giá tĩnh hoặc cam kết tồn kho nếu không có trong Data/Tool.
- **Cập nhật tài liệu**: Bất cứ khi nào bạn tạo thêm router trong `domains/`, thêm hàm trong `ai_engine.py` hoặc chỉnh sửa flow chính trong `chat_pipeline.py`, hãy ghi chú lại vào mục **4. CÁC ĐỢT REFACTOR** của file này.
