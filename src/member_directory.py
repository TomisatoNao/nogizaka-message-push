# ============================================================
# member_directory.py — 拉取账号可见的成员目录（/v2/groups）
# ============================================================
# tools/list_members.py 和网页管理端共用的核心逻辑：
# 无打印、无副作用，出错时返回 (None, 错误说明)。
#
# 注意：这里故意不做 Token 续期 —— 网页管理端在独立线程 / 独立
# event loop 里调用，而 credentials 的续期锁绑定主 event loop，
# 跨 loop 使用会炸。Token 过期时直接报 401 让调用方提示重试。
# ============================================================
from __future__ import annotations

import sqlite3
from collections.abc import Iterable

import httpx

import config.config as cfg
from config.credentials import (
    ACCOUNT_CREDS,
    get_mobile_api_base,
    get_mobile_headers,
    get_web_headers,
)
from src.logger import log_all


def is_mobile(acc_cfg: dict) -> bool:
    return acc_cfg.get("auth_method") == "mobile"


def api_base(account_id: str, acc_cfg: dict) -> str:
    """该账号列成员时应该打的 API 根地址。"""
    if is_mobile(acc_cfg):
        return get_mobile_api_base(account_id)
    if acc_cfg.get("api_base"):
        return acc_cfg["api_base"]
    group_type = acc_cfg.get("group_type", "")
    if group_type.lower() == "yodel":
        return "https://api.service.yodel-app.com"
    return f"https://api.message.{group_type}.com"


def build_headers(account_id: str, acc_cfg: dict) -> dict[str, str]:
    """按账号认证方式构造请求头（与 fetcher 保持一致）。"""
    if is_mobile(acc_cfg):
        return get_mobile_headers(account_id)

    cred = ACCOUNT_CREDS.get(account_id) or {}
    headers = get_web_headers(
        acc_cfg.get("group_type", ""),
        cred.get("token", ""),
        app_tag=acc_cfg.get("app_tag"),
        api_base=acc_cfg.get("api_base"),
        web_origin=acc_cfg.get("web_origin"),
    )
    cookies = cred.get("cookies") or {}
    if cookies:
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return headers


def normalize_groups(payload) -> list[dict] | None:
    """/v2/groups 返回的可能是裸数组，也可能包一层 groups/items。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("groups", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


async def fetch_member_directory(
    client: httpx.AsyncClient, account_id: str,
) -> tuple[list[dict] | None, str | None]:
    """拉取账号可见的成员目录。返回 (成员列表, None) 或 (None, 错误说明)。"""
    acc_cfg = cfg.ACCOUNTS.get(account_id)
    if not acc_cfg:
        return None, f"账号未定义: {account_id}"

    url = f"{api_base(account_id, acc_cfg).rstrip('/')}/v2/groups"
    try:
        resp = await client.get(url, headers=build_headers(account_id, acc_cfg))
    except httpx.TimeoutException:
        return None, "请求超时：上游成员接口在限定时间内未响应，可稍后重试或检查代理。"
    except httpx.RequestError as exc:
        return None, f"网络请求失败: {type(exc).__name__}，请检查网络、代理或域名解析。"

    if resp.status_code == 401:
        return None, "HTTP 401：凭证已失效或 Token 过期。主程序运行中会自动续期，稍后重试；仍失败则需重新填凭证。"
    if resp.status_code == 404:
        return None, f"HTTP 404：该地址没有成员列表接口（毕业生专用域名可能不提供），可改用同团其他账号查询。({url})"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"

    try:
        payload = resp.json()
    except ValueError:
        return None, f"响应不是合法 JSON: {resp.text[:200]}"
    groups = normalize_groups(payload)
    if groups is None:
        return None, f"响应结构不认识: {str(payload)[:200]}"

    # 自动保存/刷新该账号成员的订阅元数据至 SQLite 数据库
    try:
        save_account_subscriptions(account_id, groups)
    except (OSError, sqlite3.Error) as exc:
        # 缓存写入失败不能阻塞成员选择，但必须留下可诊断记录。
        log_all(f"⚠️ 账号 {account_id} 的订阅缓存写入失败: {type(exc).__name__}: {exc}", is_error=True)

    return groups, None


def save_account_subscriptions(account_id: str, groups: list[dict]) -> None:
    """持久化保存/更新指定账号下所有成员的订阅状态与元数据至 SQLite。"""
    if not groups or not account_id:
        return
    import time
    from src.auth import get_auth_db, _lock
    conn = get_auth_db()
    now_ts = time.time()
    rows = []
    for g in groups:
        if not isinstance(g, dict):
            log_all(f"⚠️ 账号 {account_id} 收到非对象成员记录，已跳过", is_error=True)
            continue
        mid = str(g.get("id") or "")
        if not mid:
            continue
        mname = str(g.get("name") or "")
        sub = g.get("subscription")
        g_state = str(g.get("state") or "")

        if isinstance(sub, dict) and sub:
            sub_st = str(sub.get("state") or "").lower()
            if sub_st == "active":
                state = "active"
            elif sub_st == "expired":
                state = "expired"
            else:
                state = sub_st or "unsubscribed"
            sub_type = str(sub.get("type") or "")
            start_at = str(sub.get("start_at") or "")
            end_at = str(sub.get("end_at") or "")
            auto_renew = 1 if sub.get("auto_renewing") else 0
        elif g_state in ("closed", "inactive"):
            state = "closed"
            sub_type = ""
            start_at = ""
            end_at = ""
            auto_renew = 0
        else:
            state = "unsubscribed"
            sub_type = ""
            start_at = ""
            end_at = ""
            auto_renew = 0

        thumb = str(g.get("thumbnail") or "")
        if thumb and mname:
            try:
                from src.avatar_manager import save_member_avatar_record
                acc_cfg = cfg.ACCOUNTS.get(account_id) or {}
                grp_k = acc_cfg.get("group_type") or "msg"
                save_member_avatar_record(grp_k, mname, mname, thumb)
            except (OSError, sqlite3.Error) as exc:
                log_all(f"⚠️ 成员 {mname} 的头像缓存更新失败: {type(exc).__name__}: {exc}", is_error=True)

        rows.append((account_id, mid, mname, state, sub_type, start_at, end_at, auto_renew, now_ts))

    with _lock:
        try:
            with conn:
                conn.executemany("""
                    INSERT INTO member_subscriptions (
                        account_id, member_id, member_name, state, sub_type, start_at, end_at, auto_renewing, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, member_id) DO UPDATE SET
                        member_name = excluded.member_name,
                        state = excluded.state,
                        sub_type = excluded.sub_type,
                        start_at = excluded.start_at,
                        end_at = excluded.end_at,
                        auto_renewing = excluded.auto_renewing,
                        updated_at = excluded.updated_at;
                """, rows)
        except sqlite3.Error as exc:
            log_all(f"⚠️ 账号 {account_id} 的订阅缓存数据库写入失败: {type(exc).__name__}: {exc}", is_error=True)
            raise


def get_member_subscription(account_id: str, member_id: str) -> dict | None:
    """查询指定账号+成员ID的最新订阅缓存。"""
    if not account_id or not member_id:
        return None
    from src.auth import get_auth_db, _lock
    conn = get_auth_db()
    with _lock:
        try:
            cur = conn.execute("""
                SELECT state, sub_type, start_at, end_at, auto_renewing, updated_at, member_name
                FROM member_subscriptions
                WHERE account_id = ? AND member_id = ?
            """, (account_id, str(member_id)))
            row = cur.fetchone()
            if row:
                return {
                    "state": row[0],
                    "sub_type": row[1],
                    "start_at": row[2],
                    "end_at": row[3],
                    "auto_renewing": bool(row[4]),
                    "updated_at": row[5],
                    "member_name": row[6],
                }
        except sqlite3.Error as exc:
            log_all(f"⚠️ 查询成员订阅缓存失败: {type(exc).__name__}: {exc}", is_error=True)
            return None
    return None


def get_all_subscriptions(account_id: str = "") -> dict[str, dict]:
    """获取所有已缓存的订阅字典，以 'account_id:member_id' 为 key。"""
    from src.auth import get_auth_db, _lock
    conn = get_auth_db()
    result = {}
    with _lock:
        try:
            if account_id:
                cur = conn.execute("""
                    SELECT account_id, member_id, state, sub_type, start_at, end_at, auto_renewing, updated_at, member_name
                    FROM member_subscriptions WHERE account_id = ?
                """, (account_id,))
            else:
                cur = conn.execute("""
                    SELECT account_id, member_id, state, sub_type, start_at, end_at, auto_renewing, updated_at, member_name
                    FROM member_subscriptions
                """)
            for row in cur.fetchall():
                acc, mid, state, sub_type, start_at, end_at, auto_renew, upd, mname = row
                result[f"{acc}:{mid}"] = {
                    "account_id": acc,
                    "member_id": mid,
                    "member_name": mname,
                    "state": state,
                    "sub_type": sub_type,
                    "start_at": start_at,
                    "end_at": end_at,
                    "auto_renewing": bool(auto_renew),
                    "updated_at": upd,
                }
        except sqlite3.Error as exc:
            log_all(f"⚠️ 读取订阅缓存失败: {type(exc).__name__}: {exc}", is_error=True)
    return result


def is_member_active_subscription(account_id: str, member_id: str) -> bool | None:
    """快速判断成员是否处于活跃订阅中。返回 True（活跃）、False（未订阅/已过期/离线）、None（未缓存）。"""
    sub = get_member_subscription(account_id, member_id)
    if sub is None:
        return None
    return sub.get("state") == "active"


async def sync_all_accounts_subscriptions(
    client: httpx.AsyncClient | None = None, *, include_errors: bool = False,
    account_ids: Iterable[str] | None = None,
) -> dict[str, int] | tuple[dict[str, int], dict[str, str]]:
    """同步各账号订阅状态；单个账号失败不阻塞其余账号。

    ``include_errors=True`` 时同时返回 ``{账号: 原因}``，供 WebUI 展示部分失败。
    ``account_ids`` 用于启动阶段只同步当前监控项引用的账号；省略时保持原有
    行为，遍历配置中的全部账号（供管理端手动同步使用）。
    """
    from config.credentials import is_account_fetch_available, validate_account_cred
    stats = {}
    errors = {}
    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=20)
        should_close = True

    selected_ids = list(account_ids) if account_ids is not None else list(cfg.ACCOUNTS.keys())
    for acc_id in selected_ids:
        fetch_available, refresh_reason = is_account_fetch_available(acc_id)
        if not fetch_available:
            errors[acc_id] = refresh_reason
            log_all(f"⚠️ 订阅同步跳过账号 {acc_id}: {refresh_reason}", is_warning=True)
            continue
        try:
            ok, _ = validate_account_cred(acc_id)
        except Exception as exc:  # 最后一层隔离：第三方凭证模块异常不能中断其他账号。
            message = f"凭证检查异常: {type(exc).__name__}"
            errors[acc_id] = message
            log_all(f"⚠️ 订阅同步账号 {acc_id} {message}", is_warning=True)
            continue
        if not ok:
            errors[acc_id] = "凭证不可用或已过期"
            log_all(f"⚠️ 订阅同步跳过账号 {acc_id}: 凭证不可用", is_warning=True)
            continue
        try:
            groups, err = await fetch_member_directory(client, acc_id)
        except Exception as exc:  # 最后一层隔离：保留每账号上下文并继续后续同步。
            message = f"成员目录处理异常: {type(exc).__name__}"
            errors[acc_id] = message
            log_all(f"⚠️ 订阅同步账号 {acc_id} {message}", is_warning=True)
            continue
        if err:
            errors[acc_id] = err
            log_all(f"⚠️ 订阅同步账号 {acc_id} 失败: {err}", is_warning=True)
            continue
        stats[acc_id] = len(groups or [])
    if should_close:
        await client.aclose()
    return (stats, errors) if include_errors else stats
