from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import config as cfgmod
import javis_control_bridge


class JavisControlCall(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    body: Any | None = None
    confirm: str | None = None


def _make_router() -> APIRouter:
    router = APIRouter(prefix="/javis-control", tags=["Legacy Javis Control Bridge"])

    @router.get("/capabilities")
    async def javis_control_capabilities():
        return javis_control_bridge.capabilities(cfgmod.read_settings())

    @router.get("/status")
    async def javis_control_status():
        settings = cfgmod.read_settings()
        caps = javis_control_bridge.capabilities(settings)
        health = None
        if caps.get("enabled"):
            try:
                health = await asyncio.to_thread(javis_control_bridge.call_action, settings, "health")
            except javis_control_bridge.JavisControlError as exc:
                health = {"ok": False, "error": str(exc), "status_code": exc.status_code}
        return {
            "enabled": caps.get("enabled"),
            "base_url": caps.get("base_url"),
            "allow_writes": caps.get("allow_writes"),
            "confirm_writes": caps.get("confirm_writes"),
            "read_actions": [a["name"] for a in caps["actions"] if a["mode"] == "read"],
            "write_actions": [a["name"] for a in caps["actions"] if a["mode"] == "write"],
            "health": health,
        }

    @router.post("/call")
    async def javis_control_call(req: JavisControlCall):
        settings = cfgmod.read_settings()
        try:
            return await asyncio.to_thread(
                javis_control_bridge.call_action,
                settings,
                req.action,
                req.params,
                req.body,
                req.confirm,
            )
        except javis_control_bridge.JavisControlError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status_code)

    return router


def register(app):
    router = _make_router()
    app.include_router(router)
    return router
