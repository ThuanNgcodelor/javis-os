"""Private, freshness-bounded order lookup cache for the CFC chat pipeline.

The AMIS warm job may contain raw customer and sale-order records.  Public
Redis snapshots must never expose those records, so this module publishes only
the minimum server-side matching data: order code variants, HMAC'd phones,
status and timestamps.  A chat lookup must provide both an exact order code and
the phone that belongs to that order.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Any

from .config import AmisConfig


def _phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("84") and len(digits) == 11:
        digits = "0" + digits[2:]
    return digits if len(digits) >= 9 else ""


def _phone_digest(phone: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), phone.encode("utf-8"), hashlib.sha256).hexdigest()


def _account_keys(record: dict[str, Any]) -> set[str]:
    values = (
        record.get("account_name"),
        record.get("account_number"),
        record.get("account_code"),
        record.get("customer_id"),
        record.get("account_id"),
        record.get("id"),
    )
    return {str(value).strip().casefold() for value in values if str(value or "").strip()}


def _order_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _order_keys(order: dict[str, Any]) -> set[str]:
    values = (
        order.get("sale_order_no"),
        order.get("order_no"),
        order.get("order_code"),
        order.get("code"),
        order.get("id"),
    )
    keys: set[str] = set()
    for value in values:
        key = _order_key(value)
        if not key:
            continue
        keys.add(key)
        if key.startswith("DH") and len(key) > 2:
            keys.add(key[2:])
        elif len(key) >= 4:
            keys.add(f"DH{key}")
    return keys


def _display_order_code(order: dict[str, Any]) -> str:
    for field in ("sale_order_no", "order_no", "order_code", "code", "id"):
        value = str(order.get(field) or "").strip()
        if value:
            return value
    return ""


def build_order_lookup_snapshot(
    datasets: dict[str, list[dict[str, Any]]],
    *,
    config: AmisConfig,
    synced_at: str,
) -> dict[str, Any] | None:
    """Create a minimal private cache without raw PII or order-line values."""
    secret = str(config.order_lookup_hmac_secret or "")
    if not secret:
        return None

    phones_by_account: dict[str, set[str]] = {}
    for customer in datasets.get("customers") or []:
        if not isinstance(customer, dict):
            continue
        phones = {
            _phone(customer.get(field))
            for field in ("office_tel", "phone", "mobile_phone", "tel", "chatbot_public_phone")
        }
        phones.discard("")
        if not phones:
            continue
        for key in _account_keys(customer):
            phones_by_account.setdefault(key, set()).update(phones)

    items: list[dict[str, Any]] = []
    for order in datasets.get("sale_orders") or []:
        if not isinstance(order, dict):
            continue
        keys = _order_keys(order)
        display_code = _display_order_code(order)
        if not keys or not display_code:
            continue
        phones = {
            _phone(order.get(field))
            for field in ("customer_phone", "phone", "office_tel", "mobile_phone")
        }
        for account_key in _account_keys(order):
            phones.update(phones_by_account.get(account_key, set()))
        phones.discard("")
        if not phones:
            continue
        status = str(order.get("status") or order.get("order_status") or "Đang xử lý").strip()
        updated_at = str(order.get("modified_date") or order.get("updated_at") or order.get("created_date") or "").strip()
        items.append({
            "order_code": display_code,
            "order_keys": sorted(keys),
            "phone_hmacs": sorted({_phone_digest(phone, secret) for phone in phones}),
            "status": status[:240],
            "updated_at": updated_at[:80],
        })

    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 1,
        "source": "amis_crm_order_warm",
        "synced_at": synced_at,
        "record_count": len(items),
        "snapshot_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "items": items,
    }


def _parse_synced_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def lookup_cached_order_status(
    redis_client: Any,
    *,
    config: AmisConfig,
    order_code: str,
    phone: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Look up one exact code only when its owner phone also matches."""
    key = _order_key(order_code)
    canonical_phone = _phone(phone)
    if not key or not canonical_phone:
        return {"outcome": "missing_input"}
    if not config.order_lookup_hmac_secret:
        return {"outcome": "unavailable", "reason": "ORDER_LOOKUP_SECRET_MISSING"}
    try:
        raw = await redis_client.get(config.redis_order_lookup_key)
    except Exception:
        return {"outcome": "unavailable", "reason": "ORDER_CACHE_READ_FAILED"}
    if not raw:
        return {"outcome": "unavailable", "reason": "ORDER_CACHE_MISSING"}
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        snapshot = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        return {"outcome": "unavailable", "reason": "ORDER_CACHE_INVALID"}
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("items"), list):
        return {"outcome": "unavailable", "reason": "ORDER_CACHE_INVALID"}

    synced_at = _parse_synced_at(str(snapshot.get("synced_at") or ""))
    current = now or datetime.now(timezone.utc)
    if synced_at is None or (current - synced_at).total_seconds() > config.order_lookup_max_age_seconds:
        return {
            "outcome": "unavailable",
            "reason": "ORDER_CACHE_STALE",
            "synced_at": str(snapshot.get("synced_at") or ""),
        }

    matches = [
        item for item in snapshot["items"]
        if isinstance(item, dict) and key in set(item.get("order_keys") or [])
    ]
    if not matches:
        return {
            "outcome": "not_found",
            "synced_at": str(snapshot.get("synced_at") or ""),
            "source_id": "amis:internal:order-warm",
        }
    phone_hmac = _phone_digest(canonical_phone, config.order_lookup_hmac_secret)
    matched = next((item for item in matches if phone_hmac in set(item.get("phone_hmacs") or [])), None)
    if not matched:
        return {
            "outcome": "phone_mismatch",
            "synced_at": str(snapshot.get("synced_at") or ""),
            "source_id": "amis:internal:order-warm",
        }
    return {
        "outcome": "found",
        "order_code": str(matched.get("order_code") or order_code),
        "status": str(matched.get("status") or "Đang xử lý"),
        "order_updated_at": str(matched.get("updated_at") or ""),
        "synced_at": str(snapshot.get("synced_at") or ""),
        "source_id": "amis:internal:order-warm",
    }
