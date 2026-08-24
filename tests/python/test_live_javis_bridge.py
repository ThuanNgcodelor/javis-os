import sys
from pathlib import Path

from _paths import ROOT, SERVER  # noqa: E402,F401

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from live_javis_bridge import build_live_javis_context


def test_live_javis_bridge_collects_runtime_state(tmp_path):
    brain = tmp_path / "brain"
    brain.mkdir()

    responses = {
        "/health": {
            "service": "ok",
            "ollama": "ok",
            "redis": "ok",
            "embed_model": "bge-m3",
            "vector_indexes": ["cfc", "zeo"],
        },
        "/admin/n8n/workflows": {
            "workflows": [
                {"name": "Zeo chatbot", "active": True, "updatedAt": "2026-08-24T00:00:00Z", "tags": ["bot"]},
                {"name": "CFC chatbot", "active": False, "updatedAt": "2026-08-23T00:00:00Z", "tags": ["bot"]},
            ],
            "total": 2,
        },
        "/admin/n8n/executions?limit=20": {
            "executions": [
                {"workflowName": "Zeo chatbot", "status": "success", "startedAt": "2026-08-24T01:00:00Z"},
                {"workflowName": "CFC chatbot", "status": "error", "startedAt": "2026-08-24T01:05:00Z"},
            ],
            "stats": {"success": 1, "error": 1, "running": 0, "waiting": 0},
        },
        "/admin/n8n/file-status": {
            "files": [
                {
                    "workflow_file": "zeo_chatbot.workflow.ts",
                    "has_changes": True,
                    "baseline_source": "n8n",
                }
            ],
            "note": "1 workflow changed",
        },
        "/admin/assistant/quick-prompts": {
            "prompts": [
                {"label": "Danh sách n8n", "query": "Liệt kê danh sách các workflow n8n và trạng thái hoạt động"},
                {"label": "Kiểm tra lỗi n8n", "query": "Kiểm tra xem có workflow n8n nào bị lỗi gần đây không?"},
            ]
        },
    }

    def fetcher(base_url: str, endpoint: str, timeout_seconds: float):
        assert base_url == "http://127.0.0.1:8000"
        assert timeout_seconds > 0
        return responses[endpoint]

    settings = {
        "context_runtime": {
            "live_javis": {
                "enabled": True,
                "base_url": "http://127.0.0.1:8000",
                "timeout_seconds": 4,
                "max_workflows": 2,
                "max_executions": 2,
                "max_prompts": 2,
            }
        }
    }

    result = build_live_javis_context(brain, "Javis đang chạy gì?", settings, fetcher=fetcher)
    assert result.endpoint_count == 5
    assert result.source_count == 5
    assert result.error_count == 0
    assert result.roots == ("http://127.0.0.1:8000",)
    assert any(item.kind == "live_javis_health" for item in result.items)
    assert any("workflows" in item.content.lower() for item in result.items)
    assert any("executions" in item.content.lower() for item in result.items)
    assert any("quick prompts" in item.content.lower() for item in result.items)


def test_live_javis_bridge_disabled_returns_empty():
    result = build_live_javis_context(
        Path("/tmp/brain"),
        "whatever",
        {"context_runtime": {"live_javis": {"enabled": False}}},
    )
    assert result.items == ()
    assert result.endpoint_count == 0
    assert result.source_count == 0
