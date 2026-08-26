import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import chat_pipeline  # noqa: E402
from chat_pipeline import ChatPipelineRequest, _extract_phone_and_area, _normalize_vn, process_chat_pipeline  # noqa: E402


class FakeRedis:
    async def get(self, key):
        return None


FAQS = {
    "cfc_dealer_location_request": (
        "Dạ bạn gửi giúp mình số điện thoại và khu vực/tỉnh thành cụ thể. "
        "Admin sẽ chuyển thông tin để nhân viên khu vực hỗ trợ ạ."
    ),
    "cfc_dosage_usage_review": (
        "Dạ phần liều lượng, cách bón hoặc phối trộn cần kỹ sư nông nghiệp kiểm tra "
        "theo cây trồng, giai đoạn và khu vực canh tác."
    ),
    "cfc_price_unverified": (
        "Dạ bảng giá phụ thuộc dòng sản phẩm, quy cách và khu vực phân phối."
    ),
    "cfc_company_website": "Dạ website chính thức của CFC Cò Bay là https://cfccobay.com nha bạn.",
}


async def fake_faq(brand, intent):
    answer = FAQS.get(intent, "")
    return {"intent": intent, "answer": answer, "source_id": f"test:{intent}"} if answer else {}


class CfcGroundedMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        chat_pipeline._local_session_cache.clear()
        chat_pipeline._local_customer_cache.clear()

    def _patches(self):
        return (
            patch("chat_pipeline.get_redis", new=AsyncMock(return_value=FakeRedis())),
            patch("chat_pipeline.get_faq_by_intent", side_effect=fake_faq),
            patch("chat_pipeline._async_save_profile_and_notify", new=AsyncMock()),
            patch("chat_pipeline._llm_nlu_config", return_value=("off", 0.3, 0.72)),
        )

    def test_area_extraction_keeps_only_location_phrase(self):
        cases = {
            "Tôi ở gần chợ Ô Môn, muốn mua 10 bao phân NPK": "chợ Ô Môn",
            "Khu vực xã Định Môn, Thới Lai có đại lý nào giao tận nhà không shop?": "xã Định Môn, Thới Lai",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                _, area = _extract_phone_and_area(text, _normalize_vn(text))
                self.assertEqual(area, expected)

    def test_cfc_workflow_forwards_messenger_location_contract(self):
        workflow = (
            SERVER_DIR.parents[1]
            / "workflows"
            / "local-n8n"
            / "cfc_cobay_chatbot.workflow.ts"
        ).read_text(encoding="utf-8")
        for expected in (
            "payload?.coordinates",
            "input_kind: $json.inputKind",
            "latitude: $json.latitude",
            "longitude: $json.longitude",
        ):
            self.assertIn(expected, workflow)

    async def test_inventory_goal_resumes_after_phone_only_turn(self):
        redis_patch, faq_patch, profile_patch, nlu_patch = self._patches()
        with redis_patch, faq_patch, profile_patch, nlu_patch:
            first = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="inventory-resume",
                text="Sản phẩm NPK 16-16-8 TE bao 50kg trong kho còn nhiều không? Lấy 5 tấn có liền không?",
            ))
            second = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="inventory-resume",
                text="0979176415",
            ))

        self.assertEqual(first.intent, "cfc_inventory_unavailable")
        self.assertIn("chưa kết nối tồn kho realtime", first.answer)
        self.assertEqual(second.intent, "cfc_inventory_unavailable")
        state = chat_pipeline._local_session_cache[
            "cfc:session:messenger:inventory-resume"
        ]["conversation_state"]
        self.assertEqual(state["active_goal"]["name"], "inventory_check")
        self.assertEqual(state["confirmed_slots"]["formula"], "16-16-8 TE")
        self.assertEqual(state["confirmed_slots"]["quantity"], "5 tấn")
        self.assertEqual(state["confirmed_slots"]["phone"], "0979176415")
        self.assertEqual(state["pending_slots"], ["area"])

    async def test_phone_with_loyalty_question_is_not_swallowed_by_contact_fast_path(self):
        redis_patch, faq_patch, profile_patch, nlu_patch = self._patches()
        with redis_patch, faq_patch, profile_patch, nlu_patch:
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="loyalty-intent",
                text="Số điện thoại của mình là 0979176415, kiểm tra xem mình có tích điểm hay chiết khấu gì chưa?",
            ))

        self.assertEqual(result.intent, "cfc_loyalty_unavailable")
        self.assertNotEqual(result.intent, "contact_phone_provided")
        self.assertIn("chưa kết nối dữ liệu khách hàng", result.answer)
        self.assertNotIn("đã có điểm", result.answer.lower())
        state = chat_pipeline._local_session_cache[
            "cfc:session:messenger:loyalty-intent"
        ]["conversation_state"]
        self.assertEqual(state["active_goal"]["name"], "loyalty_lookup")
        self.assertEqual(state["confirmed_slots"]["phone"], "0979176415")

    async def test_agronomy_reply_is_expert_intake_without_formula_or_dose(self):
        redis_patch, faq_patch, profile_patch, nlu_patch = self._patches()
        with redis_patch, faq_patch, profile_patch, nlu_patch:
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="agronomy-intake",
                text="Sầu riêng giai đoạn nuôi trái non bị rụng hạt chuỗi thì nên bón công thức NPK nào và liều lượng sao?",
            ))

        self.assertEqual(result.intent, "cfc_dosage_usage_review")
        for expected in ("sầu riêng", "nuôi trái non", "rụng hạt chuỗi", "không tự đưa công thức"):
            self.assertIn(expected, result.answer)
        self.assertNotRegex(result.answer, r"\b\d{1,2}-\d{1,2}-\d{1,2}\b")
        trace = chat_pipeline._local_session_cache[
            "cfc:session:messenger:agronomy-intake"
        ]["last_trace"]
        self.assertEqual(trace["source_id"], "test:cfc_dosage_usage_review")

    async def test_location_attachment_is_acknowledged_without_fake_nearest_dealer(self):
        redis_patch, faq_patch, profile_patch, nlu_patch = self._patches()
        with redis_patch, faq_patch, profile_patch, nlu_patch:
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="live-location",
                text="",
                input_kind="location",
                attachment_type="location",
                latitude=10.1092,
                longitude=105.6235,
            ))

        self.assertEqual(result.intent, "cfc_dealer_location_received")
        self.assertIn("đã nhận vị trí", result.answer)
        self.assertIn("chưa kết nối", result.answer)
        self.assertNotIn("đại lý gần nhất là", result.answer.lower())
        state = chat_pipeline._local_session_cache[
            "cfc:session:messenger:live-location"
        ]["conversation_state"]
        self.assertEqual(state["confirmed_slots"]["location"]["latitude"], 10.1092)

    async def test_cfc_phone_request_does_not_invent_or_reuse_zeo_hotline(self):
        redis_patch, faq_patch, profile_patch, nlu_patch = self._patches()
        with redis_patch, faq_patch, profile_patch, nlu_patch:
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="cfc-contact",
                text="Cho mình xin số hotline chăm sóc khách hàng Cò Bay",
            ))

        self.assertEqual(result.intent, "cfc_contact_information_unavailable")
        self.assertIn("chưa có số hotline", result.answer)
        self.assertIn("https://cfccobay.com", result.answer)
        self.assertNotIn("1900 5307", result.answer)
        self.assertNotIn("0292 3841 818", result.answer)

    async def test_unknown_cfc_question_never_calls_generative_answer(self):
        redis_patch, faq_patch, profile_patch, nlu_patch = self._patches()
        ai_answer = AsyncMock(return_value="Thông tin tự suy đoán")
        with redis_patch, faq_patch, profile_patch, nlu_patch, \
                patch("chat_pipeline.semantic_search", new=AsyncMock(return_value={
                    "answer": "", "intent": "", "score": 0.0, "source_id": "",
                })), \
                patch("chat_pipeline.reason_and_answer_cskh", new=ai_answer), \
                patch("chat_pipeline.notify_admin_unanswered", new=AsyncMock()):
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="strict-grounding",
                text="Phân này có chống chịu mặn tuyệt đối không?",
            ))

        ai_answer.assert_not_awaited()
        self.assertEqual(result.intent, "cfc_grounded_fallback")
        self.assertIn("không tự suy đoán", result.answer)
        self.assertEqual(result.fallback_reason, "NO_GROUNDED_KNOWLEDGE")

    async def test_sourced_cfc_rag_answer_is_not_rewritten_by_llm(self):
        redis_patch, faq_patch, profile_patch, nlu_patch = self._patches()
        synthesizer = AsyncMock(return_value="Câu trả lời có fact được tự thêm")
        with redis_patch, faq_patch, profile_patch, nlu_patch, \
                patch("chat_pipeline.semantic_search", new=AsyncMock(return_value={
                    "answer": "Nội dung nguyên bản từ Knowledge CFC.",
                    "intent": "support_general",
                    "score": 0.91,
                    "source_id": "test:cfc:knowledge",
                    "answer_mode": "rewrite",
                })), \
                patch("chat_pipeline.synthesize_cskh_answer", new=synthesizer):
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="strict-rag-rewrite",
                text="Mình cần hỏi một nội dung hỗ trợ CFC",
            ))

        synthesizer.assert_not_awaited()
        self.assertEqual(result.answer, "Nội dung nguyên bản từ Knowledge CFC.")
        self.assertEqual(result.intent, "support_general")
        trace = chat_pipeline._local_session_cache[
            "cfc:session:messenger:strict-rag-rewrite"
        ]["last_trace"]
        self.assertEqual(trace["source_id"], "test:cfc:knowledge")


if __name__ == "__main__":
    unittest.main()
