import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import chat_pipeline  # noqa: E402
from chat_pipeline import ChatPipelineRequest, ChatPipelineResponse, process_chat_pipeline  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def ltrim(self, key, start, end):
        values = self.lists.get(key, [])
        self.lists[key] = values[start:] if start < 0 else values[start:end + 1]

    async def expire(self, key, seconds):
        return None

    async def eval(self, script, count, key, token):
        if self.values.get(key) == token:
            await self.delete(key)
            return 1
        return 0


class PipelineIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        chat_pipeline._local_session_cache.clear()
        chat_pipeline._local_customer_cache.clear()

    async def test_same_message_id_executes_and_persists_once(self):
        redis_client = FakeRedis()
        response = ChatPipelineResponse(
            answer="Hotline ZeO 1900 5307",
            intent="company_contact_information",
            confidence="high",
            score=1.0,
            brand="ZEO",
        )
        execute_once = AsyncMock(return_value=response)
        request = ChatPipelineRequest(
            brand="zeo",
            sender_id="sender-1",
            text="Cho mình xin hotline",
            message_id="mid-1",
        )

        with patch("chat_pipeline.get_redis", new=AsyncMock(return_value=redis_client)), \
                patch("chat_pipeline._process_chat_pipeline_once", execute_once):
            first = await process_chat_pipeline(request)
            second = await process_chat_pipeline(request)

        self.assertFalse(first.duplicate)
        self.assertEqual(first.idempotency_status, "processed")
        self.assertTrue(second.duplicate)
        self.assertEqual(second.idempotency_status, "cached")
        execute_once.assert_awaited_once()
        self.assertEqual(len(redis_client.lists["zeo:history:messenger:sender-1"]), 1)

    async def test_in_flight_duplicate_is_not_executed(self):
        redis_client = FakeRedis()
        request = ChatPipelineRequest(
            brand="zeo",
            sender_id="sender-2",
            text="Alo",
            message_id="mid-busy",
        )
        from message_idempotency import _keys

        lease_key, _ = _keys("zeo", "mid-busy")
        redis_client.values[lease_key] = "another-worker"
        execute_once = AsyncMock()
        with patch("chat_pipeline.get_redis", new=AsyncMock(return_value=redis_client)), \
                patch("chat_pipeline._process_chat_pipeline_once", execute_once):
            result = await process_chat_pipeline(request)

        self.assertTrue(result.duplicate)
        self.assertEqual(result.idempotency_status, "in_flight")
        execute_once.assert_not_awaited()

    async def test_unremembered_fast_path_is_finalized(self):
        redis_client = FakeRedis()
        execute_once = AsyncMock(return_value=ChatPipelineResponse(
            answer="Dạ ZeO chào bạn",
            intent="greeting",
            confidence="high",
            score=1.0,
            brand="ZEO",
        ))
        request = ChatPipelineRequest(brand="zeo", sender_id="sender-3", text="Alo")
        with patch("chat_pipeline.get_redis", new=AsyncMock(return_value=redis_client)), \
                patch("chat_pipeline._process_chat_pipeline_once", execute_once):
            await process_chat_pipeline(request)

        session = chat_pipeline._local_session_cache["zeo:session:messenger:sender-3"]
        self.assertEqual(session["last_intent"], "greeting")
        self.assertEqual(session["conversation_state"]["recent_turns"][-1]["user"], "Alo")
        self.assertEqual(session["revision"], 1)
        self.assertNotIn("zeo:sender-3", chat_pipeline._sender_locks)
        self.assertNotIn("zeo:sender-3", chat_pipeline._sender_lock_users)


if __name__ == "__main__":
    unittest.main()
