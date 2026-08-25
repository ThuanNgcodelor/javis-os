import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from ai_engine import synthesize_cskh_answer  # noqa: E402
from chat_pipeline import _sanitized_chat_history  # noqa: E402


class AiGroundingContextTests(unittest.IsolatedAsyncioTestCase):
    def test_pipeline_history_redacts_pii_and_keeps_roles(self):
        history = _sanitized_chat_history({
            "recent_turns": [{
                "user": "Số mình 0908776655, email test@example.com",
                "bot": "Mình đã nhận số 0908776655",
            }]
        })
        rendered = " ".join(item["content"] for item in history)
        self.assertNotIn("0908776655", rendered)
        self.assertNotIn("test@example.com", rendered)
        self.assertIn("[PHONE]", rendered)
        self.assertIn("[EMAIL]", rendered)
        self.assertEqual([item["role"] for item in history], ["user", "assistant"])

    async def test_synthesis_uses_history_without_default_product_link_or_freeship_claim(self):
        generator = AsyncMock(return_value={
            "success": True,
            "text": "Dạ đây là câu trả lời đã đối chiếu dữ liệu thực tế cho bạn ạ.",
        })
        with patch("ai_engine.generate_ai_text", generator):
            answer = await synthesize_cskh_answer(
                user_query="Loại đó dùng sao?",
                brand="zeo",
                retrieved_facts="Sử dụng theo hướng dẫn trên bao bì.",
                chat_history=[{
                    "role": "user",
                    "content": "Gọi mình theo số 0908776655, mail test@example.com",
                }],
                catalog_products=[{"name": "Sản phẩm thử", "price": "100000"}],
            )

        self.assertIsNotNone(answer)
        prompt = generator.await_args.kwargs["prompt"]
        system_prompt = generator.await_args.kwargs["system_prompt"]
        self.assertNotIn("0908776655", prompt)
        self.assertNotIn("test@example.com", prompt)
        self.assertIn("[PHONE]", prompt)
        self.assertNotIn("shopee.vn/zeovietnamofficial", prompt)
        self.assertNotIn("Freeship Extra toàn quốc", system_prompt)


if __name__ == "__main__":
    unittest.main()
