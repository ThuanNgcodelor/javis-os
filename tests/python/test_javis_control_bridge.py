import sys
from pathlib import Path

from _paths import SERVER  # noqa: E402,F401

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import javis_control_bridge


def _settings(**control):
    base = {
        "enabled": True,
        "base_url": "http://127.0.0.1:8000",
        "timeout_seconds": 8,
        "allow_writes": False,
        "confirm_writes": True,
        "confirm_phrase": "JAVIS_WRITE",
    }
    base.update(control)
    return {"context_runtime": {"javis_control": base}}


def test_capabilities_expose_read_and_write_actions():
    caps = javis_control_bridge.capabilities(_settings())
    names = {item["name"] for item in caps["actions"]}
    assert "n8n_workflows" in names
    assert "learning_queue" in names
    assert "workflow_toggle" in names
    assert caps["allow_writes"] is False


def test_read_action_uses_admin_endpoint():
    seen = {}

    def fetcher(base_url, method, endpoint, timeout_seconds, body):
        seen.update(base_url=base_url, method=method, endpoint=endpoint, timeout=timeout_seconds, body=body)
        return {"workflows": []}

    result = javis_control_bridge.call_action(_settings(), "n8n_workflows", fetcher=fetcher)
    assert result["ok"] is True
    assert seen == {
        "base_url": "http://127.0.0.1:8000",
        "method": "GET",
        "endpoint": "/admin/n8n/workflows",
        "timeout": 8.0,
        "body": None,
    }


def test_write_action_requires_enabled_writes_and_confirm():
    try:
        javis_control_bridge.call_action(_settings(), "workflow_toggle", {"workflow_id": "abc"})
    except javis_control_bridge.JavisControlError as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("write action should be blocked")

    writable = _settings(allow_writes=True)
    try:
        javis_control_bridge.call_action(writable, "workflow_toggle", {"workflow_id": "abc"})
    except javis_control_bridge.JavisControlError as exc:
        assert exc.status_code == 428
    else:
        raise AssertionError("write action should require confirmation")


def test_write_action_with_confirm_builds_path():
    seen = {}

    def fetcher(base_url, method, endpoint, timeout_seconds, body):
        seen.update(method=method, endpoint=endpoint)
        return {"success": True}

    result = javis_control_bridge.call_action(
        _settings(allow_writes=True),
        "workflow_toggle",
        {"workflow_id": "abc"},
        confirm="JAVIS_WRITE",
        fetcher=fetcher,
    )
    assert result["ok"] is True
    assert seen == {"method": "POST", "endpoint": "/admin/n8n/workflows/abc/toggle"}
