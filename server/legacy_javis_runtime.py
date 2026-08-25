"""In-process adapter for the ZeO/CFC legacy Javis chatbot runtime.

This is the migration bridge that removes the need to run the old FastAPI
server on :8000. It still loads the legacy Python modules from the existing
workspace, so logic and settings stay exactly where they are until each module
is moved into Javis OS for good.
"""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LEGACY_SERVER = _ROOT / "chatbot" / "server"
if not _DEFAULT_LEGACY_SERVER.exists():
    _DEFAULT_LEGACY_SERVER = _ROOT.parent / "N8n" / "ChatbotN8n" / "javis" / "server"
_MODULE_NAMES = ("chat_pipeline", "rag_search", "knowledge_sync", "shopee_matcher")


class LegacyJavisRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyModules:
    chat_pipeline: Any
    rag_search: Any
    knowledge_sync: Any
    shopee_matcher: Any
    server_dir: Path


_MODULES: LegacyModules | None = None


def legacy_server_dir(settings: dict | None = None) -> Path:
    runtime = (settings or {}).get("context_runtime") or {}
    cfg = runtime.get("legacy_javis_runtime") if isinstance(runtime.get("legacy_javis_runtime"), dict) else {}
    raw = os.getenv("JAVIS_LEGACY_SERVER_DIR") or cfg.get("server_dir") or str(_DEFAULT_LEGACY_SERVER)
    return Path(str(raw)).expanduser().resolve()


def _ensure_path(server_dir: Path) -> None:
    if not server_dir.exists():
        raise LegacyJavisRuntimeError(f"Legacy Javis server dir not found: {server_dir}")
    if not (server_dir / "chat_pipeline.py").exists():
        raise LegacyJavisRuntimeError(f"Legacy Javis chat_pipeline.py not found in: {server_dir}")
    text = str(server_dir)
    if text not in sys.path:
        # Append instead of prepend: Javis OS keeps priority for its own modules, while
        # legacy-only module names still resolve from this directory.
        sys.path.append(text)


def load_modules(settings: dict | None = None) -> LegacyModules:
    global _MODULES
    server_dir = legacy_server_dir(settings)
    if _MODULES is not None and _MODULES.server_dir == server_dir:
        return _MODULES

    _ensure_path(server_dir)
    imported = {name: importlib.import_module(name) for name in _MODULE_NAMES}
    _MODULES = LegacyModules(
        chat_pipeline=imported["chat_pipeline"],
        rag_search=imported["rag_search"],
        knowledge_sync=imported["knowledge_sync"],
        shopee_matcher=imported["shopee_matcher"],
        server_dir=server_dir,
    )
    return _MODULES


def _to_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return value


async def chat_pipeline(payload: dict[str, Any], settings: dict | None = None) -> dict[str, Any]:
    mods = load_modules(settings)
    req = mods.chat_pipeline.ChatPipelineRequest(**payload)
    res = await mods.chat_pipeline.process_chat_pipeline(req)
    return _to_plain(res)


async def search(payload: dict[str, Any], settings: dict | None = None) -> dict[str, Any]:
    mods = load_modules(settings)
    query = str(payload.get("query") or "").strip()
    if not query:
        raise LegacyJavisRuntimeError("query không được để trống")
    result = await mods.rag_search.semantic_search(
        query=query,
        brand=str(payload.get("brand") or "zeo").lower(),
        top_k=min(max(1, int(payload.get("top_k") or 5)), 10),
        category_filter=payload.get("category_filter"),
    )
    return result if isinstance(result, dict) else {"result": result}


async def sync(brand: str = "zeo", settings: dict | None = None) -> dict[str, Any]:
    mods = load_modules(settings)
    brand = str(brand or "zeo").lower()
    if brand == "all":
        return {
            "zeo": await mods.knowledge_sync.sync_brand("zeo"),
            "cfc": await mods.knowledge_sync.sync_brand("cfc"),
        }
    if brand not in ("zeo", "cfc"):
        raise LegacyJavisRuntimeError("brand phải là 'zeo', 'cfc', hoặc 'all'")
    return await mods.knowledge_sync.sync_brand(brand)


async def refresh_shopee_cache(brand: str = "all", settings: dict | None = None) -> dict[str, Any]:
    mods = load_modules(settings)
    brand = str(brand or "all").lower()
    mods.shopee_matcher.refresh_shopee_cache(brand)
    return {"status": "ok", "message": f"Shopee cache refreshed for brand={brand}"}


def status(settings: dict | None = None) -> dict[str, Any]:
    server_dir = legacy_server_dir(settings)
    loaded = _MODULES is not None and _MODULES.server_dir == server_dir
    chat_pipeline_source = (
        str(Path(_MODULES.chat_pipeline.__file__).resolve())
        if loaded and getattr(_MODULES.chat_pipeline, "__file__", None)
        else str((server_dir / "chat_pipeline.py").resolve())
    )
    return {
        "enabled": True,
        "mode": "in_process",
        "server_dir": str(server_dir),
        "chat_pipeline_source": chat_pipeline_source,
        "available": (server_dir / "chat_pipeline.py").exists(),
        "loaded": loaded,
        "endpoints": ["/api/chat-pipeline", "/sync", "/search", "/api/shopee/refresh-cache"],
    }
