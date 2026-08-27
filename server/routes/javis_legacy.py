from __future__ import annotations

from typing import Any

# pyrefly: ignore [missing-import]
import httpx
# pyrefly: ignore [missing-import]
from fastapi import APIRouter, HTTPException, Query
# pyrefly: ignore [missing-import]
from pydantic import BaseModel

import config as cfgmod
import legacy_javis_runtime


class LegacySearchRequest(BaseModel):
    query: str
    brand: str = "zeo"
    top_k: int = 5
    category_filter: str | None = None


class LegacyRewriteRequest(BaseModel):
    query: str
    answer: str
    brand: str = "zeo"
    tone: str = "friendly"


def _legacy_settings() -> dict:
    return cfgmod.read_settings()


def _legacy_error(exc: Exception, status_code: int = 500) -> HTTPException:
    return HTTPException(status_code=status_code, detail=f"{type(exc).__name__}: {exc}")


def _make_router() -> APIRouter:
    router = APIRouter(tags=["Legacy Javis Runtime Compatibility"])

    @router.get("/legacy-javis/status")
    async def legacy_javis_status():
        return legacy_javis_runtime.status(_legacy_settings())

    @router.post("/api/chat-pipeline")
    async def chat_pipeline_endpoint(payload: dict[str, Any]):
        try:
            return await legacy_javis_runtime.chat_pipeline(payload, _legacy_settings())
        except Exception as exc:  # noqa: BLE001 - compatibility boundary
            raise _legacy_error(exc) from exc

    @router.get("/api/chat-handoffs")
    async def list_chat_handoffs_endpoint(brand: str = Query("all")):
        try:
            return await legacy_javis_runtime.list_chat_handoffs(brand, _legacy_settings())
        except legacy_javis_runtime.LegacyJavisRuntimeError as exc:
            raise _legacy_error(exc, 400) from exc

    @router.post("/api/chat-handoffs/{brand}/{sender_id}/resolve")
    async def resolve_chat_handoff_endpoint(brand: str, sender_id: str):
        try:
            return await legacy_javis_runtime.resolve_chat_handoff(brand, sender_id, _legacy_settings())
        except legacy_javis_runtime.LegacyJavisRuntimeError as exc:
            raise _legacy_error(exc, 400) from exc

    @router.post("/sync")
    async def sync_knowledge_endpoint(brand: str = Query("zeo", description="'zeo', 'cfc', hoặc 'all'")):
        try:
            return await legacy_javis_runtime.sync(brand, _legacy_settings())
        except legacy_javis_runtime.LegacyJavisRuntimeError as exc:
            raise _legacy_error(exc, 400) from exc
        except Exception as exc:  # noqa: BLE001 - compatibility boundary
            raise _legacy_error(exc) from exc

    @router.post("/search")
    async def search_endpoint(req: LegacySearchRequest):
        try:
            return await legacy_javis_runtime.search(req.model_dump(), _legacy_settings())
        except legacy_javis_runtime.LegacyJavisRuntimeError as exc:
            raise _legacy_error(exc, 400) from exc
        except Exception as exc:  # noqa: BLE001 - compatibility boundary
            raise _legacy_error(exc) from exc

    @router.post("/rewrite")
    async def rewrite_endpoint(req: LegacyRewriteRequest):
        settings = _legacy_settings()
        legacy_dir = legacy_javis_runtime.legacy_server_dir(settings)
        cfg_path = legacy_dir / "settings.json"
        try:
            import json
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            base_url = cfg.get("ollama", {}).get("base_url", "http://127.0.0.1:11434")
        except Exception:
            base_url = "http://127.0.0.1:11434"

        brand_name = "ZeO" if req.brand.lower() == "zeo" else "Cò Bay"
        tone = "thân thiện, gần gũi, dùng 'bạn' và 'mình'" if req.tone == "friendly" else "lịch sự, chuyên nghiệp"
        system_prompt = (
            f"Bạn là nhân viên CSKH của {brand_name}. Viết lại câu trả lời sau theo giọng {tone}. "
            "Giữ nguyên tất cả thông tin thực tế. KHÔNG thêm thông tin mới hoặc bịa đặt."
        )
        user_prompt = f"Khách hỏi: {req.query}\nCâu trả lời thô: {req.answer}\nHãy viết lại tự nhiên hơn:"
        rewritten = req.answer
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{base_url}/api/chat",
                    json={
                        "model": "qwen2.5:7b-instruct",
                        "stream": False,
                        "options": {"temperature": 0.3, "top_p": 0.9, "num_predict": 300},
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                resp.raise_for_status()
                rewritten = (resp.json().get("message", {}) or {}).get("content", req.answer).strip()
        except Exception:
            rewritten = req.answer
        return {"original_answer": req.answer, "rewritten_answer": rewritten, "brand": req.brand}

    @router.post("/api/chat/pipeline")
    async def chat_pipeline_slash_endpoint(payload: dict[str, Any]):
        try:
            return await legacy_javis_runtime.chat_pipeline(payload, _legacy_settings())
        except Exception as exc:  # noqa: BLE001 - compatibility boundary
            raise _legacy_error(exc) from exc

    @router.post("/api/shopee/refresh-cache")
    async def refresh_shopee_cache_endpoint(brand: str = Query("all", description="'zeo', 'cfc', hoặc 'all'")):
        try:
            return await legacy_javis_runtime.refresh_shopee_cache(brand, _legacy_settings())
        except Exception as exc:  # noqa: BLE001 - compatibility boundary
            raise _legacy_error(exc) from exc

    @router.post("/api/web/refresh-cache")
    async def refresh_web_cache_endpoint(brand: str = Query("all", description="'zeo', 'cfc', hoặc 'all'")):
        try:
            return await legacy_javis_runtime.refresh_web_cache(brand, _legacy_settings())
        except Exception as exc:  # noqa: BLE001 - compatibility boundary
            raise _legacy_error(exc) from exc

    try:
        import sys
        server_dir = legacy_javis_runtime.legacy_server_dir(_legacy_settings())
        if str(server_dir) not in sys.path:
            sys.path.append(str(server_dir))
        from domains.amis import routes as amis_routes
        router.include_router(amis_routes.router, prefix="/admin")
    except Exception as exc:  # noqa: BLE001
        pass

    return router


def register(app):
    router = _make_router()
    app.include_router(router)
    return router
