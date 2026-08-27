# Kế hoạch Ollama Conversation Orchestrator đa luồng cho Chatbot ZeO/CFC

**Ngày cập nhật:** 27/08/2026  
**Phạm vi:** `chatbot/server` và các bài kiểm thử liên quan  
**Trạng thái:** Kế hoạch triển khai; chưa sửa runtime, chưa deploy, chưa thay đổi workflow n8n  
**Mục tiêu chính:** Mỗi khách Messenger có một hội thoại liên tục được Ollama đọc đúng ngữ cảnh, trong khi dữ liệu Redis/AMIS/RAG và các output một lượt đang đúng vẫn được giữ nguyên.

---

## 1. Quyết định kiến trúc

Không tạo một tiến trình hoặc một model Ollama riêng cho từng khách. Mỗi khách có một conversation riêng trong Redis, được xác định bằng `brand + channel + page_id + sender_id`. Khi có tin nhắn mới, backend nạp đúng lịch sử của khách đó và gửi `messages[]` vào cùng model Ollama.

Vai trò được chia rõ:

- **Redis conversation store:** lưu transcript, state, kết quả tool và TTL.
- **Ollama Conversation Orchestrator:** hiểu câu nối tiếp, tham chiếu, đổi chủ đề, slot còn thiếu và chọn tool.
- **QueryPlan/dialogue router hiện tại:** tiếp tục xử lý các intent rõ ràng và làm fallback khi Ollama lỗi.
- **Redis/AMIS/Shopee/RAG:** nguồn sự thật duy nhất cho dữ liệu nghiệp vụ.
- **Formatter/grounding policy:** kiểm soát output cuối, không cho Ollama tự tạo giá, số điện thoại, tồn kho, trạng thái đơn hoặc chính sách.

Luồng mục tiêu:

```text
Messenger/n8n
  -> ChatPipelineRequest
  -> Load profile + conversation + recent tool results
  -> Privacy/safety guards
  -> Deterministic QueryPlan
  -> Ollama Conversation Orchestrator
  -> Tool allowlist (Redis/AMIS/Shopee/RAG)
  -> Grounding validation
  -> Existing formatter hoặc grounded composer
  -> Persist conversation + tool results + trace
```

## 2. Ràng buộc không được vi phạm

1. Không thay dữ liệu Redis, AMIS projection hoặc cấu trúc đồng bộ đang chạy tốt trong đợt đầu.
2. Không cho Ollama trả fact nghiệp vụ nếu chưa có tool result hoặc `source_id` hợp lệ.
3. Không đổi output của các câu hỏi một lượt đang được deterministic route trả đúng.
4. Không để Ollama bypass privacy guard, ownership check, OTP, grounding policy hoặc capability boundary.
5. Ollama lỗi, timeout hoặc JSON sai thì pipeline hiện tại vẫn trả lời như trước.
6. Không đẩy hoặc kích hoạt workflow n8n. Chủ dự án tự quyết định deployment sau khi duyệt kết quả.
7. Không đưa CRM raw, PII không cần thiết, dữ liệu tài chính hoặc thông tin khách khác vào prompt.

## 3. Vấn đề hiện tại cần giải quyết

Hệ thống đã lưu `recent_turns`, `conversation_summary`, `active_goal`, `confirmed_slots` và một phần entity. Tuy nhiên lớp memory hiện thiên về sản phẩm/lead, chưa nhớ đầy đủ các đối tượng vừa trả từ tool.

Các điểm chính:

- Ollama planner hiện tại chủ yếu dành cho ZeO và danh sách intent còn hẹp.
- Ollama chỉ nhận summary ngắn trong NLU shadow, chưa nhận một conversation envelope đầy đủ.
- Lịch sử trước khi gọi LLM đang bị giới hạn hai lần; model có thể chỉ thấy khoảng ba cặp hội thoại gần nhất.
- Khi đã có chat history, `conversation_summary` có thể không được đưa cùng vào prompt.
- Kết quả tool như danh sách đại lý chưa được lưu thành `last_tool_results`; lượt sau chỉ còn text trả lời.
- Các cụm “mấy chỗ trên”, “số của họ”, “loại lúc nãy”, “đơn đó”, “còn hàng không” chưa có cơ chế tham chiếu dùng chung.
- Khi deterministic intent không nhận ra, semantic RAG có thể kéo câu nối tiếp sang một FAQ gần nghĩa nhưng sai tác vụ.

## 4. Conversation contract mới

Giữ các Redis key hiện tại để tương thích ngược. Bổ sung schema mới bên trong `conversation_state`; dữ liệu cũ thiếu field vẫn đọc được bằng default.

```json
{
  "schema_version": 4,
  "conversation_id": "cfc:messenger:<page_id>:<sender_id>:<episode>",
  "active_goal": {"name": "dealer_lookup", "stage": "browsing"},
  "confirmed_slots": {},
  "recent_turns": [],
  "conversation_summary": "",
  "last_tool_results": [],
  "reference_stack": [],
  "topic_stack": [],
  "takeover_state": {},
  "updated_at": ""
}
```

### 4.1. `last_tool_results`

Mỗi kết quả tool chỉ giữ các field được phép công khai và đủ để tham chiếu:

```json
{
  "result_id": "tool_result_...",
  "tool": "sales_location_search",
  "source_id": "amis:public:sales-locations:active",
  "query": {"ward": "Định Môn", "district": "Thới Lai"},
  "match_scope": "province_fallback",
  "items": [
    {
      "entity_id": "location_...",
      "display_name": "...",
      "public_phone": "...",
      "public_address": "..."
    }
  ],
  "created_at": "...",
  "expires_at": "..."
}
```

Giới hạn đề xuất: tối đa 5 kết quả tool gần nhất, mỗi kết quả tối đa 8 item. Dữ liệu biến động như tồn kho, giá và trạng thái đơn phải có TTL ngắn và luôn được tra lại trước khi xác nhận.

### 4.2. Phân lớp memory

| Lớp | Nội dung | Thời hạn đề xuất |
| --- | --- | --- |
| Recent turns | 10-20 message gần nhất | Theo session hiện tại |
| Structured state | Goal, slots, entity, pending action | 30 ngày như cấu hình hiện tại |
| Tool-result reference | ID và bản chiếu an toàn của kết quả vừa trả | Theo độ tươi từng tool |
| Customer profile | Phone/area đã được khách cung cấp, consent phù hợp | Theo chính sách dữ liệu |
| Conversation summary | Tóm tắt mục tiêu, quyết định và việc còn chờ | 30 ngày; cập nhật khi đổi chủ đề |

## 5. Ollama planner đa luồng

Tạo planner mới, không dùng nguyên prompt ZeO hiện tại. Planner không trực tiếp viết câu trả lời cho khách; chỉ xuất JSON theo schema cố định.

```json
{
  "intent": "dealer_contact_followup",
  "confidence": 0.96,
  "is_followup": true,
  "topic_changed": false,
  "reference": {
    "type": "tool_result",
    "result_id": "tool_result_...",
    "entity_ids": ["location_1", "location_2"]
  },
  "requested_fields": ["public_phone"],
  "tool": "dealer_contact_lookup",
  "arguments": {},
  "missing_slots": [],
  "reason_code": "PHONE_OF_PREVIOUS_DEALERS"
}
```

Planner phải nhận:

- System policy theo brand.
- Conversation summary.
- Recent turns đã redact PII không cần thiết.
- Active goal và confirmed slots.
- Metadata của `last_tool_results`.
- QueryPlan deterministic hiện tại.
- Tin nhắn mới của khách.

Planner không được nhận field CRM ngoài public/privileged contract đã duyệt.

## 6. Ma trận capability đa luồng

| Nhóm luồng | Ví dụ follow-up | Tool/source | Quy tắc output |
| --- | --- | --- | --- |
| Danh mục/sản phẩm | “loại số 2”, “cái đó dùng sao” | Shopee catalog, Redis product KB | Giữ đúng sản phẩm vừa hiển thị; không đổi link/giá nguồn |
| Giá | “bao đó bao nhiêu”, “giá sỉ thì sao” | Catalog giá hoặc nguồn pricing có hiệu lực | Không có nguồn thì giữ `price_unverified` |
| Tồn kho | “còn không”, “lấy 5 tấn được chứ” | ATP/inventory adapter | Luôn tra lại realtime/TTL ngắn; không dùng số cũ làm fact |
| Đại lý/điểm bán | “xin SĐT”, “chỗ số 2”, “gần nhất” | AMIS public sales locations | Chỉ dùng `public_phone`; công bố mức khớp exact/fallback |
| Giao hàng | “họ có giao tận nhà không” | Delivery scope/policy đã xác minh | Không suy ra từ việc có địa chỉ đại lý |
| Đơn hàng | “đơn đó tới đâu rồi”, “xe bốc chưa” | Privileged order lookup | Xác minh ownership; luôn lấy trạng thái mới |
| Hội viên/tích điểm | “điểm của tôi”, “hạng hiện tại sao” | Privileged loyalty lookup | OTP/ownership trước khi trả dữ liệu cá nhân |
| Tư vấn cây trồng | “giai đoạn đó bón sao”, “100 ha thì sao” | Knowledge kỹ thuật + expert intake | Không tự tạo công thức/liều lượng ngoài nguồn |
| Mua sỉ/đại lý | “chiết khấu quý này”, “tôi cấp 1” | Wholesale policy có hiệu lực hoặc handoff | Nhớ phone, area, cấp đại lý; không bịa tỷ lệ |
| Khiếu nại/đổi trả | “lô đó bị vón”, “đổi thế nào” | Complaint SOP, return policy | Giữ sản phẩm/lô/vấn đề; ưu tiên handoff đúng bộ phận |
| Công ty/liên hệ | “số nào”, “địa chỉ đó ở đâu” | FAQ có nguồn | Phân biệt hotline công ty với SĐT đại lý/khách |
| Lead/handoff | “tôi gửi số rồi”, “bao giờ gọi” | Customer profile + handoff state | Không hỏi lại slot; không hứa trạng thái chưa có bằng chứng |
| Chuyển chủ đề | “thôi hỏi đơn hàng”, “quay lại loại trước” | Conversation state | Đóng/mở goal đúng; không làm bẩn slot giữa các topic |

## 7. Thứ tự xử lý trong pipeline

Thứ tự đề xuất để không làm yếu guardrail hiện tại:

1. Normalize input và kiểm tra idempotency.
2. Load profile/session theo sender.
3. Chạy privacy guard và các chặn dữ liệu nhạy cảm.
4. Build QueryPlan deterministic.
5. Chạy orchestrator ở mode cấu hình.
6. Nếu deterministic route rõ và thuộc protected route: giữ nguyên output hiện tại.
7. Nếu câu mơ hồ/follow-up và planner đủ confidence: resolve reference và gọi tool allowlist.
8. Validate source, freshness, ownership và public fields.
9. Dùng formatter hiện tại; chỉ dùng Ollama composer ở nhánh đã được bật riêng.
10. Persist state, transcript, tool result và trace trước khi trả webhook.

Semantic RAG không được phép ghi đè một follow-up đã resolve thành tool/active goal hợp lệ.

## 8. Chế độ rollout và rollback

Thêm feature flag bằng environment:

```text
CHAT_CONVERSATION_MODE=off|shadow|assist|primary
CHAT_CONVERSATION_SAMPLE_RATE=0.0..1.0
CHAT_CONVERSATION_MIN_CONFIDENCE=0.85
```

| Mode | Ảnh hưởng output |
| --- | --- |
| `off` | Pipeline hiện tại hoàn toàn không đổi |
| `shadow` | Mọi turn được Ollama phân tích nhưng không được thay route/output |
| `assist` | Chỉ can thiệp câu follow-up/unknown có context và tool hợp lệ |
| `primary` | Ollama planner được ưu tiên rộng; chưa bật production trong kế hoạch đầu |

Rollback production chỉ cần chuyển về `off`; không cần xóa Redis hoặc rollback dữ liệu.

## 9. Lộ trình triển khai nhiều luồng

### P0 - Đóng băng baseline

- Chạy toàn bộ unittest và conversation replay hiện tại.
- Lưu golden expectations cho protected routes.
- Ghi intent, source, answer mode, fallback reason và state sau mỗi turn.
- Tách lỗi sẵn có khỏi regression do orchestrator.

Gate: test hiện tại phải xanh trước khi bắt đầu P1.

### P1 - Conversation envelope dùng chung

- Thêm schema v4 tương thích ngược.
- Sửa context compiler để gửi đồng thời summary + recent turns.
- Loại bỏ cắt lịch sử hai lần.
- Thêm `last_tool_results`, `reference_stack` và episode metadata.
- Chưa thay route/output.

Gate: output protected routes không đổi; restart vẫn phục hồi state đúng.

### P2 - Ollama multi-flow shadow

- Tạo `conversation_orchestrator.py`.
- Mở rộng planner cho toàn bộ capability trong bảng mục 6.
- Tận dụng cơ chế `nlu_shadow.py` hiện có để chạy nền, bounded và redact PII.
- Ghi agreement, latency, timeout, malformed JSON và proposed tool.
- Không tác động câu trả lời khách.

Gate: không tăng latency webhook; không rò PII; planner đạt ngưỡng trên bộ replay.

### P3 - Tool-result memory cho mọi domain

- Bọc các hàm lookup hiện tại thành tool contract chuẩn.
- Lưu kết quả sản phẩm, đại lý, inventory, order, loyalty và FAQ reference theo TTL phù hợp.
- Tool privileged phải có ownership/auth gate.
- Chưa cho Ollama tự soạn fact.

Gate: mọi tool result có `source_id`, freshness và public/privileged classification.

### P4 - Assist Wave A: dữ liệu công khai, rủi ro thấp

Bật theo sender hash/canary cho:

- Product/catalog follow-up.
- Dealer/location/contact follow-up.
- Shipping follow-up có policy nguồn.
- Company contact/address disambiguation.
- Topic switch và quay lại topic trước.

Gate: protected output không đổi; không bịa phone/link/policy; follow-up accuracy đạt ít nhất 95%.

### P5 - Assist Wave B: dữ liệu vận hành

Bật cho:

- Inventory/ATP.
- Order/shipment status.
- Loyalty/member tier.
- Pricing/discount có ngày hiệu lực.

Điều kiện bắt buộc: adapter realtime/TTL ngắn, ownership, permission và freshness rõ ràng. Nếu thiếu một điều kiện thì giữ capability boundary hiện tại.

### P6 - Assist Wave C: quy trình chuyên môn và handoff

Bật cho:

- Agronomy multi-turn.
- Wholesale/B2B intake.
- Complaint/return SOP.
- Lead collection và handoff status.

Ollama được phép tóm tắt tình huống và slot, nhưng khuyến nghị kỹ thuật, tỷ lệ chiết khấu và cam kết xử lý vẫn phải có nguồn.

### P7 - Đánh giá `primary` mode

Chỉ xem xét sau khi shadow/assist đủ dữ liệu. `primary` không phải điều kiện để hoàn thành dự án; `assist` có thể là mode production lâu dài vì giữ tốt các output deterministic hiện tại.

## 10. File dự kiến thay đổi khi triển khai

| File | Mức thay đổi | Nội dung |
| --- | --- | --- |
| `chatbot/server/conversation_orchestrator.py` | Mới | Context compiler, planner schema, mode gate, tool decision |
| `chatbot/server/ai_engine.py` | Vừa | Planner CFC/ZeO nhận messages và structured context |
| `chatbot/server/chat_pipeline.py` | Có kiểm soát | Hook sau load state, trước RAG và sau tool result |
| `chatbot/server/conversation_store.py` | Nhỏ | Persist schema v4/tool result, giữ TTL và key cũ |
| `chatbot/server/nlu_shadow.py` | Vừa | Observation multi-turn, metrics, bounded queue |
| `chatbot/server/query_understanding.py` | Nhỏ | Bổ sung contract tham chiếu; không mở rộng regex tràn lan |
| `chatbot/server/dialogue_router.py` | Nhỏ | Route các follow-up intent vào tool phù hợp |
| `chatbot/server/grounding_policy.py` | Nhỏ | Gate source/freshness/tool result cho response mới |
| `chatbot/server/settings.json` | Hạn chế | Default mode; secret vẫn chỉ lấy từ environment |
| `chatbot/server/eval_conversation_replays.jsonl` | Vừa | Gold conversation đa luồng |
| `chatbot/server/conversation_replay_eval.py` | Vừa | Chấm reference/tool/freshness/topic switch |
| `chatbot/server/tests/` | Lớn | Regression, isolation, failure và multi-flow tests |

Không sửa n8n workflow trong P0-P4. `page_id` có thể thêm sau dưới dạng optional field; nếu payload không có thì dùng key `brand + sender_id` như hiện tại.

## 11. Ma trận kiểm thử bắt buộc

### 11.1. Regression một lượt

- Mỗi intent hiện tại có ít nhất một golden case.
- So sánh intent, source, fallback reason và các fact quan trọng.
- Với output động, so sánh invariant thay vì khóa nguyên văn địa chỉ/số lượng.

### 11.2. Replay nhiều lượt

1. Sản phẩm -> chọn số 2 -> hỏi giá -> xin link.
2. Sản phẩm -> hỏi còn hàng -> đổi quy cách -> hỏi lại tồn kho.
3. Đại lý -> xin SĐT -> chọn đại lý số 2 -> hỏi giao tận nhà.
4. GPS -> hỏi chỗ gần nhất -> hỏi chỉ đường.
5. Đơn hàng -> bổ sung mã đơn -> hỏi xe đã bốc chưa -> hỏi thời gian giao.
6. Hội viên -> gửi phone -> OTP -> hỏi điểm -> hỏi hạng.
7. Cây trồng -> bổ sung giai đoạn -> diện tích -> khu vực -> xin kỹ sư.
8. Mua sỉ -> bổ sung cấp đại lý -> phone -> area -> hỏi trạng thái handoff.
9. Khiếu nại -> bổ sung sản phẩm/lô -> ảnh -> yêu cầu đổi trả.
10. Đổi chủ đề ba lần rồi quay lại topic cũ.

### 11.3. Safety và isolation

- Hai sender không đọc được state của nhau.
- Hai brand không dùng chéo source/hotline/catalog.
- Không trả phone không thuộc public projection.
- Không trả thông tin đơn/điểm nếu chưa xác minh ownership.
- Prompt injection trong chat history không được nâng quyền tool.
- Ollama timeout, Redis lỗi một phần hoặc JSON malformed đều fail-safe.

## 12. Tiêu chí nghiệm thu

| Chỉ số | Ngưỡng |
| --- | ---: |
| Existing protected-route regression | 100% pass |
| K1 source correctness | 100% |
| Unsupported/private claim | 0 |
| Cross-sender/brand contamination | 0 |
| Follow-up reference accuracy | >= 95% |
| Active-goal resume | >= 95% |
| Known-slot repeat rate | 0% |
| Tool result có source/freshness hợp lệ | 100% |
| Shadow failure ảnh hưởng webhook | 0 |

Không dùng một điểm tổng để che lỗi. Báo cáo phải tách `memory`, `routing`, `grounding`, `task_completion`, `latency` và `safety`.

## 13. Observability cần có

Mỗi turn bổ sung trace an toàn:

```json
{
  "conversation_orchestrator": {
    "mode": "shadow",
    "intent": "dealer_contact_followup",
    "confidence": 0.96,
    "reference_resolved": true,
    "tool": "dealer_contact_lookup",
    "tool_executed": false,
    "route_changed": false,
    "reason_code": "PHONE_OF_PREVIOUS_DEALERS",
    "latency_ms": 420
  }
}
```

Log không ghi phone/email nguyên văn. Dashboard/debug chỉ hiển thị dữ liệu đã redact và source metadata.

## 14. Rủi ro và biện pháp chặn

| Rủi ro | Biện pháp |
| --- | --- |
| Ollama hiểu đúng ngữ cảnh nhưng tự thêm fact | Planner chỉ trả JSON; facts chỉ từ tool result |
| LLM làm chậm mọi tin nhắn | Shadow chạy nền; assist chỉ chạy cho follow-up/unknown |
| Context quá dài | Summary + 10-20 message + tool metadata; không gửi toàn bộ vô hạn |
| FAQ đang đúng bị đổi câu | Protected-route bypass và golden regression |
| Dữ liệu realtime bị dùng lại quá lâu | TTL theo capability và forced refresh trước xác nhận |
| RAG bắt nhầm câu nối tiếp | Resolved tool/active goal có ưu tiên cao hơn semantic RAG |
| State cũ không tương thích | Schema default và migration-on-read, không bulk rewrite Redis |
| Lẫn khách/Page | Conversation key có channel/page/sender và isolation tests |

## 15. Thứ tự thực hiện đề xuất

1. P0 baseline và golden regression.
2. P1 conversation schema v4, không đổi output.
3. P2 Ollama shadow cho toàn bộ nhóm luồng.
4. P3 tool-result memory dùng chung.
5. P4 public-flow assist theo canary.
6. P5 privileged-flow assist khi adapter/ownership đạt gate.
7. P6 expert/handoff flow.
8. Đánh giá P7; không tự động bật `primary`.

Mỗi phase là một thay đổi độc lập, có test và rollback riêng. Không gộp thay đổi dữ liệu, workflow n8n và conversation orchestrator trong cùng một lần phát hành.

## 16. Definition of Done tổng thể

Dự án được xem là hoàn thành khi:

- Cùng một khách có thể hội thoại liên tục qua tất cả nhóm capability trong mục 6.
- Câu rút gọn và đại từ được resolve về đúng entity/tool result.
- Khách đổi chủ đề hoặc quay lại chủ đề cũ không làm lẫn slot.
- Output hiện tại có nguồn tiếp tục giữ nguyên ở protected routes.
- Dữ liệu động luôn được kiểm tra freshness và quyền truy cập.
- Ollama hoặc Redis gặp lỗi vẫn có fallback an toàn.
- Bộ replay đa luồng và full unittest đạt toàn bộ gate.
- Production rollout chỉ diễn ra sau khi chủ dự án duyệt shadow report và tự quyết định deployment.

