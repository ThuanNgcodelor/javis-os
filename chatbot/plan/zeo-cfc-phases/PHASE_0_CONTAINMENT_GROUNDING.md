# Phase 0 — Containment, grounding và chặn rủi ro production

Trạng thái: `READY FOR REVIEW — CHƯA CODE`  
Ưu tiên: P0, blocking mọi phase customer-facing khác  
Ước lượng: 1–2 ngày kỹ thuật; thao tác live cần phê duyệt riêng  
Phụ thuộc: không có

## 1. Mục tiêu

Chặn ngay các đường có thể gửi dữ kiện không được kiểm chứng cho khách, đồng thời giữ các flow an toàn đang hoạt động. Phase này ưu tiên “đúng hoặc nói chưa đủ dữ liệu” hơn “trả lời hay”.

Kết quả mong muốn:

- câu hỏi nông học không còn được LLM tự dựng liều/phác đồ/chính sách;
- không có fact thì không free-generate customer answer;
- `source_id` của model không còn được xem là bằng chứng;
- lỗi FastAPI/sync không còn bị n8n che thành execution xanh giả;
- rủi ro AMIS Full Warm được cô lập theo quyết định của chủ hệ thống;
- có regression tối thiểu trước mọi thay đổi tiếp theo.

## 2. Bằng chứng hiện trạng cần xử lý

1. `chat_pipeline.py` gọi `consult_cfc_agronomy_with_ai()` ở nhiều nhánh và gửi text trực tiếp.
2. `ai_engine.py` prompt bắt model tự phân tích sinh lý cây và nêu chính sách diện tích lớn.
3. `source_id="ollama:cfc_agronomy"` được gắn khi có AI answer dù provider thật có thể khác.
4. `grounding_policy.py` coi source string không rỗng là grounded.
5. `AGRONOMY_REQUIRES_EXPERT_REVIEW` chỉ là trace/fallback reason, không chặn gửi.
6. Working-tree diff hiện bỏ guard CFC no-grounded-knowledge và bỏ kiểm tra empty facts trong `reason_and_answer_cskh()`.
7. Hai chatbot workflow route HTTP error về cùng bước prepare/send fallback.
8. Knowledge sync ghi metadata thành công trước khi vector/hot-cache hoàn tất và bỏ qua một số lỗi refresh.
9. AMIS Full Warm cùng Public Sync đang active; Full Warm xử lý raw CRM và từng chứa secret trong source.

## 3. Ngoài phạm vi

- Chưa xây full claim ledger/runtime manifest — thuộc Phase 1.
- Chưa nâng multi-intent/memory/source challenge đầy đủ — thuộc Phase 2.
- Chưa làm AMIS privileged realtime — thuộc Phase 3.
- Chưa tạo protocol nông học mới — thuộc Phase 4.
- Không đổi model để “sửa” hallucination.
- Không flush Redis, không rewrite history, không push/activate/deactivate live nếu chưa được phê duyệt.

## 4. Work packages

### P0-WP1 — Khóa customer-facing free generation

Việc làm:

1. Khôi phục invariant trong `reason_and_answer_cskh()`:
   - nếu không có `retrieved_facts` và không có `catalog_products`, trả `None`;
   - caller dùng clarification/safe fallback/handoff.
2. Không cho CFC unknown/no-grounded route gọi LLM để lấp khoảng trống.
3. LLM vẫn có thể dùng cho NLU JSON hoặc rewrite text đã grounded; không được tạo fact mới.
4. Dùng feature flag fail-closed cho customer-facing fact generation; mặc định production là `off`.

File dự kiến:

- `chatbot/server/ai_engine.py`
- `chatbot/server/chat_pipeline.py`
- `chatbot/server/settings.example.json`
- tests grounding/context

Regression cần giữ:

- greeting/chit-chat an toàn;
- dealer contact/purchase intake/clarification;
- ZeO Shopee/catalog answer có nguồn;
- privacy, order/stock/loyalty capability boundary.

### P0-WP2 — Chặn AI agronomy direct answer

Việc làm:

1. Hai call site của `consult_cfc_agronomy_with_ai()` không được trả text thẳng cho khách.
2. Với câu eligibility “Có phân cho cây X không?”:
   - tra FAQ/product-fit đã duyệt;
   - trả ngắn;
   - hỏi crop stage nếu khách muốn tư vấn sâu.
3. Với dosage/protocol thiếu fact:
   - thu thập crop, stage, tuổi cây, diện tích, tình trạng;
   - chuyển kỹ sư/handoff;
   - không nêu số hoặc chính sách.
4. Có thể giữ agronomy generator ở `internal_draft`/shadow cho nghiên cứu, nhưng output không customer-facing và không được gắn source nghiệp vụ.
5. Xóa/không sử dụng hard-coded product descriptions khi CRM không match; catalog chỉ được chứng minh tên/mã/đơn vị.

File dự kiến:

- `chatbot/server/ai_engine.py`
- `chatbot/server/chat_pipeline.py`
- `chatbot/server/dialogue_router.py` nếu cần tách product-fit với dosage
- tests CFC agronomy regression

### P0-WP3 — Grounding fail-closed

Quy tắc tạm thời trước Phase 1:

- provider/model/generator không phải source;
- source phải thuộc allowlist type: FAQ/catalog/approved-static/public-tool/privileged-tool;
- claim risk cao không có source cụ thể phải block;
- fallback reason có từ `REQUIRES_EXPERT_REVIEW` phải dẫn đến intake/handoff, không gửi draft kỹ thuật.

Việc làm:

1. `grounding_policy.py` reject `ollama:*`, `groq:*`, `openrouter:*`, `gemini:*` như fact source.
2. Bổ sung quyết định `blocked_unsupported_claim` hoặc tương đương.
3. Caller không được bỏ qua grounding decision.
4. Grounding fail phải ghi reason code và safe response, không gọi provider kế tiếp để tìm câu khác.

File dự kiến:

- `chatbot/server/grounding_policy.py`
- `chatbot/server/chat_pipeline.py`
- `chatbot/server/tests/test_grounding_policy.py`
- `chatbot/server/tests/test_ai_grounding_context.py`

### P0-WP4 — Safe source challenge tối thiểu

Phase 2 mới làm claim-level challenge đầy đủ. P0 cần fallback tối thiểu:

1. Detect nhóm câu “có thật không/nguồn đâu/chắc không/thông tin vừa nói”.
2. Nếu last response không có evidence đáng tin:
   - xin lỗi;
   - rút lại chi tiết không xác minh;
   - không bảo vệ output model;
   - chuyển nhân viên nếu cần.
3. Nếu last response có một source an toàn, chỉ nói loại nguồn/last updated đã được phép; không tự suy từng claim.
4. Ghi trace `SOURCE_CHALLENGE_SAFE_FALLBACK` để Phase 2 có thể replay.

Không thêm regex cho mọi paraphrase. Chỉ tạo một intent family/candidate với semantic/deterministic examples và fail-safe.

### P0-WP5 — N8n không che lỗi chatbot

Phần này có conflict/drift live, vì vậy quy trình bắt buộc:

1. Chạy lại env status/list.
2. Pull từng workflow chatbot sau khi chọn hướng conflict; không overwrite remote âm thầm.
3. Tách success và error output của FastAPI HTTP node.
4. Error route:
   - không dùng JSON lỗi như answer;
   - ghi alert có workflow/execution/request ID, không chứa PII;
   - gửi thông báo tạm thời đã duyệt hoặc suppress, tùy business rule.
5. CFC bổ sung guard duplicate/suppress-send/takeover tương đương ZeO.
6. Test HTTP 500, timeout, malformed JSON và `answer` rỗng.

File dự kiến:

- `workflows/local-n8n/cfc_cobay_chatbot.workflow.ts`
- `workflows/local-n8n/zeo_chatbot.workflow.ts`
- operations alert workflow nếu contract cần đổi

Không push/activate trong bước code local nếu chưa có phê duyệt live.

### P0-WP6 — Sync success contract tối thiểu

Việc làm:

1. Phân biệt ba trạng thái:
   - `snapshot_written`;
   - `vector_rebuilt`;
   - `hot_cache_refreshed`.
2. Chỉ ghi `last_success` sau khi ba bước bắt buộc đạt.
3. HTTP refresh lỗi phải làm execution fail/alert, không continue như thành công.
4. Giữ snapshot cũ nếu validation hoặc rebuild fail.
5. Không đổi format key lớn ở P0; Phase 1 mới chuẩn hóa status endpoint.

File dự kiến:

- hai knowledge workflow trong `workflows/local-n8n/`
- `chatbot/server/knowledge_sync.py`
- tests sync/cache contract

### P0-WP7 — AMIS containment cần phê duyệt live

Code/local analysis có thể làm trước, nhưng các action sau cần chủ hệ thống đồng ý:

1. Deactivate/cô lập `AMIS CRM Full Warm`.
2. Giữ một public writer được chọn.
3. Rotate/revoke secret cũ.
4. Tạm tắt `pilot_approve_all` hoặc đóng public locations cho tới khi có allowlist.
5. Kiểm tra execution retention/raw payload theo quy trình bảo mật.

P0 không triển khai realtime AMIS. Nếu AMIS audit timeout, chatbot phải trả capability boundary.

## 5. Test matrix bắt buộc

### Grounding

| Case | Kỳ vọng |
|---|---|
| CFC no facts/catalog | không gọi customer-facing generator |
| LLM output có `ollama:*` source | grounding reject |
| `REQUIRES_EXPERT_REVIEW` | intake/handoff, không gửi draft |
| provider fallback thành công | provider không trở thành fact source |
| ZeO FAQ/catalog có source | vẫn trả đúng answer |

### Sầu riêng

- “Có phân bón cho cây sầu riêng không?” → answer ngắn, không liều/chính sách.
- “Bón bao nhiêu kg/ha?” khi không có approved dosage → hỏi slot/chuyển kỹ sư.
- “Dữ liệu đó có thật không?” sau answer không nguồn → rút lại.
- Không xuất hiện các claim incident: 150/180/200/300 kg/ha, mốc 0–24 tháng, chính sách 5/10ha nếu không có fact.

### Protected regressions

- privacy third-party;
- order status unavailable;
- inventory unavailable;
- loyalty unavailable;
- dealer lookup/contact;
- purchase intake 200kg/30 tấn;
- complaint và clarification;
- ZeO price/link/catalog.

### N8n/sync

- FastAPI 500/timeout/malformed/empty answer;
- duplicate MID;
- takeover/suppress-send;
- snapshot write ok nhưng vector fail;
- vector ok nhưng hot-cache fail;
- alert không chứa sender/raw payload.

## 6. Entry gate

- Baseline working tree và runtime fingerprint được ghi nhận.
- Người dùng xác nhận phạm vi code local.
- Các action live AMIS/n8n được tách thành approval riêng.
- Không có unreviewed conflict resolution.

## 7. Exit gate

- 0 unsupported critical agronomy/policy/price/stock/order claim trong suite.
- Empty facts không customer-facing generate.
- Model/provider không thỏa `source_id` nghiệp vụ.
- Source challenge tối thiểu biết rút lại claim không nguồn.
- Protected flows không regression.
- N8n HTTP error không bị route như successful answer.
- Sync `last_success` chỉ xuất hiện sau required checkpoints.
- Quyết định AMIS containment được ghi rõ; nếu chưa duyệt thì phase trạng thái `BLOCKED`, không tự ghi `DONE`.

## 8. Rollout và rollback

Rollout đề xuất:

1. unit/static tests;
2. real-pipeline replay với sender test riêng;
3. server feature flag off/on local;
4. shadow trace nếu còn giữ internal agronomy draft;
5. deploy server containment trước;
6. n8n workflow thay đổi sau conflict-safe pull/validate và phê duyệt;
7. theo dõi unsupported/fallback/error trong 24–48 giờ.

Rollback:

- rollback theo commit/runtime manifest;
- feature flag customer generation vẫn giữ `off` khi rollback lỗi khác;
- n8n dùng revision đã pull/verify, không force local cũ;
- không restore Full Warm raw path;
- không xóa Redis session/snapshot.

## 9. Checklist nghiệm thu

- [ ] Guard empty-facts được khôi phục.
- [ ] CFC no-grounded route fail-safe.
- [ ] AI agronomy không customer-facing.
- [ ] Provider source bị grounding reject.
- [ ] Source challenge safe fallback có regression.
- [ ] Câu sầu riêng không còn claim incident.
- [ ] Protected flow suite xanh.
- [ ] N8n error route test xanh.
- [ ] Sync checkpoint contract test xanh.
- [ ] AMIS action live có quyết định/owner.
- [ ] Rollback đã được diễn tập local.

