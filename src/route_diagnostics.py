"""Read-only diagnostics for monitored members and outbound message routes.

The report describes configured eligibility only. It does not attempt delivery,
refresh credentials, contact upstream services, or treat a missing subscription
cache entry as proof that a member is unsubscribed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import sqlite3
from typing import Any

import src.config.config as cfg
from src.member_directory import is_subscription_active
from src.utils import match_member_filter


def _configured_official_bots() -> list[Any]:
    from src.platforms.qq_official import get_configured_bots

    return get_configured_bots()


def _configured_telegram_bots() -> list[Any]:
    from src.platforms.tgbot import get_configured_bots

    return get_configured_bots()


def _subscription(account_id: str, member_id: str) -> dict[str, Any] | None:
    """Read an existing subscription cache without initializing auth storage."""
    if not account_id or not member_id:
        return None

    from src.auth import AUTH_DB_PATH

    db_path = AUTH_DB_PATH
    try:
        if not db_path.is_file():
            return None
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT state, sub_type, start_at, end_at, auto_renewing, updated_at, member_name
                FROM member_subscriptions
                WHERE account_id = ? AND member_id = ?
                """,
                (account_id, str(member_id)),
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError):
        return None

    if row is None:
        return None
    subscription = {
        "state": row["state"],
        "sub_type": row["sub_type"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "auto_renewing": bool(row["auto_renewing"]),
        "updated_at": row["updated_at"],
        "member_name": row["member_name"],
    }
    subscription["is_active"] = is_subscription_active(subscription)
    return subscription


def _member_identity(member: Mapping[str, Any]) -> tuple[str, Any]:
    # Keep the same fallback order as notifier.matching_member_route_ids.
    name = member.get("m_name") or member.get("name") or "未知成员"
    member_id = member.get("m_id") or member.get("id")
    return str(name), member_id


def _fetch_account(member: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    raw_account_id = member.get("account_id") or member.get("account")
    if raw_account_id is None or not str(raw_account_id).strip():
        return "missing", None, "missing_fetch_account"

    account_id = str(raw_account_id).strip()
    accounts = getattr(cfg, "ACCOUNTS", {})
    if not isinstance(accounts, Mapping) or account_id not in accounts:
        return "missing", None, "missing_fetch_account"

    return "configured", account_id, None


def _subscription_status(
    account_id: str | None,
    member_id: Any,
    reader,
) -> dict[str, str]:
    if not account_id or member_id is None or not str(member_id).strip():
        return {"state": "unknown", "reason": "subscription_not_synced_or_unavailable"}

    try:
        record = reader(account_id, str(member_id))
    except Exception:
        return {"state": "unknown", "reason": "subscription_read_failed"}
    if not isinstance(record, Mapping) or not record:
        return {"state": "unknown", "reason": "subscription_not_synced_or_unavailable"}

    record_state = str(record.get("state") or "").strip().lower()
    known_states = {"active", "cancelled", "canceled", "expired", "closed", "inactive", "unsubscribed"}
    if record_state not in known_states:
        return {"state": "unknown", "reason": "subscription_state_unknown"}

    try:
        active = is_subscription_active(dict(record))
    except Exception:
        return {"state": "unknown", "reason": "subscription_state_unknown"}
    if active:
        return {"state": "active", "reason": "subscription_active"}
    return {"state": "inactive", "reason": "subscription_inactive"}


def _route_result(
    *,
    channel: str,
    label: str,
    channel_enabled: bool,
    route_enabled: bool,
    has_target: bool,
    filters: list[str] | None,
    member_name: str,
    member_id: Any,
) -> dict[str, str]:
    if not channel_enabled:
        return {
            "channel": channel,
            "label": label,
            "status": "blocked",
            "reason": "channel_disabled",
            "filter_status": "not_evaluated",
            "delivery_status": "not_evaluated",
        }
    if not route_enabled:
        return {
            "channel": channel,
            "label": label,
            "status": "blocked",
            "reason": "route_disabled",
            "filter_status": "not_evaluated",
            "delivery_status": "not_evaluated",
        }
    if not has_target:
        return {
            "channel": channel,
            "label": label,
            "status": "blocked",
            "reason": "no_target",
            "filter_status": "not_evaluated",
            "delivery_status": "not_evaluated",
        }
    if not match_member_filter(member_name, filters or [], member_id):
        return {
            "channel": channel,
            "label": label,
            "status": "blocked",
            "reason": "member_filter_miss",
            "filter_status": "miss",
            "delivery_status": "not_evaluated",
        }
    return {
        "channel": channel,
        "label": label,
        "status": "applicable",
        "reason": "applicable_route",
        "filter_status": "match",
        "delivery_status": "not_evaluated",
    }


def _channel_summary(channel: str, enabled: bool, route_results: list[dict[str, str]]) -> dict[str, Any]:
    reasons = sorted({route["reason"] for route in route_results if route["status"] != "applicable"})
    applicable_count = sum(route["status"] == "applicable" for route in route_results)
    if not enabled:
        reasons = sorted(set(reasons) | {"channel_disabled"})
    elif not route_results or not applicable_count:
        reasons = sorted(set(reasons) | {"no_applicable_route"})
    return {
        "channel": channel,
        "enabled": enabled,
        "configured_route_count": len(route_results),
        "applicable_route_count": applicable_count,
        "reasons": reasons,
    }


def diagnose_member_route(member: Mapping[str, Any]) -> dict[str, Any]:
    """Describe fetch association, cached subscription state, and eligible routes.

    Route labels are generated locally and never contain configured targets,
    bot identifiers, remarks, credentials, or message delivery results.
    """
    member_name, member_id = _member_identity(member)
    account_state, account_id, account_reason = _fetch_account(member)
    subscription = _subscription_status(account_id, member_id, _subscription)

    routes: list[dict[str, str]] = []
    channel_summaries: list[dict[str, Any]] = []

    napcat_enabled = bool(getattr(cfg, "ENABLE_NAPCAT_QQ", False))
    napcat_routes = getattr(cfg, "NAPCAT_ROUTES", []) or []
    napcat_results = []
    for index, route in enumerate(napcat_routes, start=1):
        if not isinstance(route, Mapping):
            continue
        napcat_results.append(_route_result(
            channel="napcat",
            label=f"NapCat route {index}",
            channel_enabled=napcat_enabled,
            route_enabled=bool(route.get("push_message", True)),
            has_target=bool(route.get("group_id")),
            filters=route.get("member_filter") or [],
            member_name=member_name,
            member_id=member_id,
        ))
    routes.extend(napcat_results)
    channel_summaries.append(_channel_summary("napcat", napcat_enabled, napcat_results))

    official_enabled = bool(getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False))
    official_results = []
    for index, bot in enumerate(_configured_official_bots(), start=1):
        filters = getattr(bot, "member_filter", None)
        push_enabled = bool(getattr(bot, "push_message", True))
        official_results.append(_route_result(
            channel="qq_official",
            label=f"Official bot {index} private",
            channel_enabled=official_enabled,
            route_enabled=push_enabled,
            has_target=bool(getattr(bot, "target_openid", "")),
            filters=filters,
            member_name=member_name,
            member_id=member_id,
        ))
        official_results.append(_route_result(
            channel="qq_official",
            label=f"Official bot {index} group",
            channel_enabled=official_enabled,
            route_enabled=push_enabled,
            has_target=bool(getattr(bot, "group_openid", "")),
            filters=filters,
            member_name=member_name,
            member_id=member_id,
        ))
    routes.extend(official_results)
    channel_summaries.append(_channel_summary("qq_official", official_enabled, official_results))

    telegram_enabled = bool(getattr(cfg, "ENABLE_TG_BOT", False))
    telegram_results = []
    for index, bot in enumerate(_configured_telegram_bots(), start=1):
        telegram_results.append(_route_result(
            channel="tg",
            label=f"Telegram bot {index}",
            channel_enabled=telegram_enabled,
            route_enabled=bool(getattr(bot, "push_message", True)),
            has_target=bool(getattr(bot, "target_chat", "")),
            filters=getattr(bot, "member_filter", None),
            member_name=member_name,
            member_id=member_id,
        ))
    routes.extend(telegram_results)
    channel_summaries.append(_channel_summary("tg", telegram_enabled, telegram_results))

    applicable_count = sum(route["status"] == "applicable" for route in routes)
    reasons = {summary_reason for summary in channel_summaries for summary_reason in summary["reasons"]}
    if account_reason:
        reasons.add(account_reason)
    if member_id is None or not str(member_id).strip():
        reasons.add("unknown_member_id")
    reasons.add(subscription["reason"])
    if not applicable_count:
        reasons.add("no_applicable_route")

    return {
        "member_name": member_name,
        "member_id_state": "known" if member_id is not None and str(member_id).strip() else "unknown",
        "fetch_account": {
            "status": account_state,
            "account_id": account_id,
        },
        "subscription": subscription,
        "routes": routes,
        "channels": channel_summaries,
        "route_summary": {
            "status": "applicable" if applicable_count else "no_applicable_route",
            "applicable_route_count": applicable_count,
            "reasons": sorted(reasons),
            "delivery_status": "not_evaluated",
        },
    }


def diagnose_monitored_members(
    members: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Diagnose an explicit member list or the current in-memory monitor list."""
    source = members if members is not None else getattr(cfg, "MONITOR_LIST", [])
    return [diagnose_member_route(member) for member in source if isinstance(member, Mapping)]
