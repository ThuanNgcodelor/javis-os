import sys
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _paths import SERVER  # noqa: E402,F401

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import legacy_javis_runtime


def test_legacy_javis_status_points_to_existing_runtime():
    status = legacy_javis_runtime.status({})
    assert status["mode"] == "in_process"
    assert status["available"] is True
    assert Path(status["chat_pipeline_source"]).samefile(Path(status["server_dir"]) / "chat_pipeline.py")
    assert "/api/chat-pipeline" in status["endpoints"]


def test_legacy_javis_modules_load():
    mods = legacy_javis_runtime.load_modules({})
    assert hasattr(mods.chat_pipeline, "process_chat_pipeline")
    assert hasattr(mods.knowledge_sync, "sync_brand")
    assert hasattr(mods.rag_search, "semantic_search")
    assert hasattr(mods.shopee_matcher, "refresh_shopee_cache")


def test_legacy_sync_forwards_candidate_snapshot_key():
    calls = []

    class FakeKnowledgeSync:
        async def sync_brand(self, brand, snapshot_key=None):
            calls.append((brand, snapshot_key))
            return {"brand": brand, "snapshot_key": snapshot_key}

    fake_modules = SimpleNamespace(knowledge_sync=FakeKnowledgeSync())
    with patch.object(legacy_javis_runtime, "load_modules", return_value=fake_modules):
        result = asyncio.run(legacy_javis_runtime.sync(
            "cfc",
            snapshot_key="cfc:kb:basic:candidate",
            settings={},
        ))

    assert calls == [("cfc", "cfc:kb:basic:candidate")]
    assert result["snapshot_key"] == "cfc:kb:basic:candidate"


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def scan_iter(self, match):
        prefix = match.removesuffix("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key


def test_handoff_list_and_resolve_update_redis_and_runtime_cache():
    redis_client = FakeRedis()
    key = "zeo:session:messenger:sender-1"
    redis_client.values[key] = json.dumps({
        "sender_id": "sender-1",
        "last_intent": "human_handoff_requested",
        "conversation_state": {
            "takeover_state": {
                "status": "pending",
                "reason": "human_handoff_requested",
                "requested_at": "2026-08-25T00:00:00Z",
            }
        },
    })
    local_cache = {}
    fake_pipeline = SimpleNamespace(
        get_redis=lambda: asyncio.sleep(0, result=redis_client),
        _local_session_cache=local_cache,
    )
    fake_modules = SimpleNamespace(chat_pipeline=fake_pipeline)

    with patch.object(legacy_javis_runtime, "load_modules", return_value=fake_modules):
        listed = asyncio.run(legacy_javis_runtime.list_chat_handoffs("zeo", {}))
        resolved = asyncio.run(legacy_javis_runtime.resolve_chat_handoff("zeo", "sender-1", {}))

    assert listed["count"] == 1
    assert listed["items"][0]["sender_id"] == "sender-1"
    assert resolved["status"] == "resolved"
    stored = json.loads(redis_client.values[key])
    assert stored["conversation_state"]["takeover_state"]["status"] == "resolved"
    assert local_cache[key]["conversation_state"]["takeover_state"]["status"] == "resolved"
