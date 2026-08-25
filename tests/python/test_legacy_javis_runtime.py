import sys
from pathlib import Path

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
