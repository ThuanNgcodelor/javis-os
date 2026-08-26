"""Internal admin endpoints for AMIS CRM audit and snapshot sync."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from .client import AmisApiError, AmisConfigurationError, AmisContractError
from .config import load_amis_config
from .service import AmisSyncSafetyError, sync_public_snapshots


class AmisRawDataset(BaseModel):
    """Raw data fetched by n8n directly from AMIS API.

    n8n POSTs this to /admin/amis/warm; FastAPI then runs projection
    and writes safe public snapshots to Redis — no credentials needed here.
    """

    customers: list[dict[str, Any]]
    products: list[dict[str, Any]]
    sale_orders: list[dict[str, Any]]


router = APIRouter(prefix="/amis", tags=["AMIS CRM Read-Only Sync"])


def _require_internal(request: Request, provided_token: str = "") -> None:
    config = load_amis_config()
    if config.internal_token:
        if not provided_token or not secrets.compare_digest(config.internal_token, provided_token):
            raise HTTPException(status_code=403, detail="AMIS sync endpoint is internal")
        return

    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="AMIS sync endpoint requires loopback access")


@router.get("/status")
async def amis_status(
    request: Request,
    x_internal_token: str = Header("", alias="X-Internal-Token"),
):
    _require_internal(request, x_internal_token)
    return {"status": "ok", "config": load_amis_config().safe_status()}


@router.post("/audit")
async def amis_audit(
    request: Request,
    x_internal_token: str = Header("", alias="X-Internal-Token"),
):
    _require_internal(request, x_internal_token)
    try:
        return await sync_public_snapshots(dry_run=True)
    except AmisConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (AmisApiError, AmisContractError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/sync")
async def amis_sync(
    request: Request,
    dry_run: bool = Query(False),
    x_internal_token: str = Header("", alias="X-Internal-Token"),
):
    _require_internal(request, x_internal_token)
    try:
        return await sync_public_snapshots(dry_run=dry_run)
    except AmisConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AmisSyncSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (AmisApiError, AmisContractError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


import logging

logger = logging.getLogger(__name__)


@router.post("/warm")
async def amis_warm(
    request: Request,
    body: AmisRawDataset,
    dry_run: bool = Query(False),
    x_internal_token: str = Header("", alias="X-Internal-Token"),
):
    """Accept raw datasets fetched by n8n, run projection, write to Redis.

    n8n holds AMIS credentials; FastAPI only receives the already-fetched
    records and applies the Python projection/safety pipeline.
    No AMIS_CLIENT_SECRET required on the FastAPI side when using this endpoint.
    """
    _require_internal(request, x_internal_token)
    try:
        return await sync_public_snapshots(
            dry_run=dry_run,
            raw_datasets={
                "products": body.products,
                "customers": body.customers,
                "sale_orders": body.sale_orders,
            },
        )
    except AmisConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AmisSyncSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (AmisApiError, AmisContractError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("AMIS warm endpoint failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"AMIS projection/sync error: {exc}") from exc

