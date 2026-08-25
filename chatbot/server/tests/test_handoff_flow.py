import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import chat_pipeline  # noqa: E402
from chat_pipeline import ChatPipelineRequest, process_chat_pipeline  # noqa: E402


class FakeRedis:
    async def get(self, key):
        return None


class HandoffFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        chat_pipeline._local_session_cache.clear()
        chat_pipeline._local_customer_cache.clear()

    async def test_human_request_pauses_following_bot_replies(self):
        with patch("chat_pipeline.get_redis", new=AsyncMock(return_value=FakeRedis())), \
                patch("chat_pipeline.notify_admin_unanswered", new=AsyncMock()), \
                patch("chat_pipeline._llm_nlu_config", return_value=("off", 0.3, 0.72)):
            first = await process_chat_pipeline(ChatPipelineRequest(
                brand="zeo",
                sender_id="handoff-user",
                text="Cho mình gặp nhân viên",
            ))
            second = await process_chat_pipeline(ChatPipelineRequest(
                brand="zeo",
                sender_id="handoff-user",
                text="Mình cần xử lý đơn hàng",
            ))

        self.assertEqual(first.intent, "human_handoff_requested")
        self.assertFalse(first.suppress_send)
        self.assertEqual(second.intent, "human_handoff_active")
        self.assertTrue(second.suppress_send)
        state = chat_pipeline._local_session_cache[
            "zeo:session:messenger:handoff-user"
        ]["conversation_state"]
        self.assertEqual(state["takeover_state"]["status"], "pending")
        self.assertEqual(state["takeover_state"]["reason"], "human_handoff_requested")

    async def test_escalated_damage_does_not_promise_automatic_refund(self):
        async def faq(brand, intent):
            return {"source_id": "policy-source", "answer": "CSKH kiểm tra điều kiện đổi trả."}

        with patch("chat_pipeline.get_redis", new=AsyncMock(return_value=FakeRedis())), \
                patch("chat_pipeline.get_faq_by_intent", side_effect=faq), \
                patch("chat_pipeline.notify_urgent_complaint", new=AsyncMock()), \
                patch("chat_pipeline._llm_nlu_config", return_value=("off", 0.3, 0.72)):
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="zeo",
                sender_id="damage-user",
                text="Đơn giao bị bể nắp chảy nước hết",
            ))

        self.assertEqual(result.intent, "urgent_damage_complaint")
        self.assertNotIn("cam kết hỗ trợ đổi mới 100%", result.answer)
        trace = chat_pipeline._local_session_cache["zeo:session:messenger:damage-user"]["last_trace"]
        self.assertEqual(trace["source_id"], "policy-source")
        self.assertEqual(trace["grounding"]["status"], "grounded")


if __name__ == "__main__":
    unittest.main()
