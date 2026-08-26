"""Audit and atomically publish AMIS public snapshots to Redis."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Optional

from domains.common.db import get_redis_client

from .client import AmisClient
from .config import AmisConfig, load_amis_config
from .projection import (
    assert_public_projection_safe,
    build_public_products,
    build_public_sales_locations,
)


class AmisSyncSafetyError(RuntimeError):
    """Raised when a new snapshot fails minimum safety gates."""


def _snapshot(items: list[dict[str, Any]], *, synced_at: str) -> dict[str, Any]:
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    envelope = {
        "schema_version": 1,
        "source": "amis_crm",
        "synced_at": synced_at,
        "record_count": len(items),
        "snapshot_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "items": items,
    }
    assert_public_projection_safe(envelope)
    return envelope


async def build_public_bundle(
    *,
    config: Optional[AmisConfig] = None,
    client: Optional[AmisClient] = None,
    now: Optional[datetime] = None,
    raw_datasets: Optional[dict[str, list]] = None,
) -> dict[str, Any]:
    cfg = config or load_amis_config()
    if raw_datasets is not None:
        # Pre-fetched path: n8n (or tests) supply data directly — no network call.
        datasets = raw_datasets
    elif client is None:
        async with AmisClient(cfg) as owned_client:
            datasets = await owned_client.fetch_public_source_datasets()
    else:
        datasets = await client.fetch_public_source_datasets()

    products, product_metrics = build_public_products(datasets["products"])
    locations, location_metrics = build_public_sales_locations(
        datasets["customers"],
        datasets["sale_orders"],
        datasets["products"],
        cfg,
        now=now,
    )
    gate_reasons = []
    if len(products) < cfg.min_public_products:
        gate_reasons.append(
            f"public products {len(products)} < minimum {cfg.min_public_products}"
        )
    if len(locations) < cfg.min_public_locations:
        gate_reasons.append(
            f"public locations {len(locations)} < minimum {cfg.min_public_locations}"
        )

    return {
        "products": products,
        "locations": locations,
        "metrics": {
            "source": {
                "products": len(datasets["products"]),
                "customers": len(datasets["customers"]),
                "sale_orders": len(datasets["sale_orders"]),
            },
            "products": product_metrics,
            "locations": location_metrics,
        },
        "gate": {
            "ready": not gate_reasons,
            "reasons": gate_reasons,
        },
    }


async def _write_bundle_to_redis(
    redis_client: Any,
    *,
    config: AmisConfig,
    products_snapshot: dict[str, Any],
    locations_snapshot: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    pipeline = redis_client.pipeline(transaction=True)
    pipeline.set(
        config.redis_products_key,
        json.dumps(products_snapshot, ensure_ascii=False, separators=(",", ":")),
    )
    pipeline.set(
        config.redis_locations_key,
        json.dumps(locations_snapshot, ensure_ascii=False, separators=(",", ":")),
    )
    pipeline.delete(config.redis_locations_geo_key)

    geo_values: list[Any] = []
    for location in locations_snapshot["items"]:
        longitude = location.get("longitude")
        latitude = location.get("latitude")
        if longitude is None or latitude is None:
            continue
        geo_values.extend([longitude, latitude, location["location_id"]])
    if geo_values:
        pipeline.geoadd(config.redis_locations_geo_key, geo_values)

    pipeline.set(
        config.redis_metadata_key,
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
    )
    await pipeline.execute()


async def sync_public_snapshots(
    *,
    dry_run: bool = False,
    config: Optional[AmisConfig] = None,
    client: Optional[AmisClient] = None,
    redis_client: Any = None,
    now: Optional[datetime] = None,
    raw_datasets: Optional[dict[str, list]] = None,
) -> dict[str, Any]:
    cfg = config or load_amis_config()
    bundle = await build_public_bundle(config=cfg, client=client, now=now, raw_datasets=raw_datasets)
    synced_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    products_snapshot = _snapshot(bundle["products"], synced_at=synced_at)
    locations_snapshot = _snapshot(bundle["locations"], synced_at=synced_at)

    report = {
        "status": "ok" if bundle["gate"]["ready"] else "blocked",
        "dry_run": dry_run,
        "written": False,
        "synced_at": synced_at,
        "gate": bundle["gate"],
        "metrics": bundle["metrics"],
        "snapshots": {
            "products": {
                "key": cfg.redis_products_key,
                "record_count": products_snapshot["record_count"],
                "snapshot_hash": products_snapshot["snapshot_hash"],
            },
            "locations": {
                "key": cfg.redis_locations_key,
                "geo_key": cfg.redis_locations_geo_key,
                "record_count": locations_snapshot["record_count"],
                "snapshot_hash": locations_snapshot["snapshot_hash"],
            },
        },
    }
    if dry_run:
        return report
    if not bundle["gate"]["ready"]:
        raise AmisSyncSafetyError("; ".join(bundle["gate"]["reasons"]))

    metadata = {
        "schema_version": 1,
        "source": "amis_crm",
        "synced_at": synced_at,
        "product_count": products_snapshot["record_count"],
        "location_count": locations_snapshot["record_count"],
        "location_with_coordinates_count": bundle["metrics"]["locations"]["with_coordinates_count"],
        "products_snapshot_hash": products_snapshot["snapshot_hash"],
        "locations_snapshot_hash": locations_snapshot["snapshot_hash"],
    }

    owns_redis = redis_client is None
    target_redis = redis_client or get_redis_client(decode=True)
    try:
        await _write_bundle_to_redis(
            target_redis,
            config=cfg,
            products_snapshot=products_snapshot,
            locations_snapshot=locations_snapshot,
            metadata=metadata,
        )
    finally:
        if owns_redis:
            await target_redis.aclose()

    report["written"] = True
    return report
