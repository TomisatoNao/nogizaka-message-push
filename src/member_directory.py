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

import httpx

import config.config as cfg
from config.credentials import (
    ACCOUNT_CREDS,
    get_mobile_api_base,
    get_mobile_headers,
    get_web_headers,
)


def is_mobile(acc_cfg: dict) -> bool:
    return acc_cfg.get("auth_method") == "mobile"


def api_base(account_id: str, acc_cfg: dict) -> str:
    """该账号列成员时应该打的 API 根地址。"""
    if is_mobile(acc_cfg):
        return get_mobile_api_base(account_id)
    if acc_cfg.get("api_base"):
        return acc_cfg["api_base"]
    return f"https://api.message.{acc_cfg.get('group_type', '')}.com"


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
    except Exception as e:
        return None, f"请求失败: {type(e).__name__}: {e}"

    if resp.status_code == 401:
        return None, "HTTP 401：凭证已失效或 Token 过期。主程序运行中会自动续期，稍后重试；仍失败则需重新填凭证。"
    if resp.status_code == 404:
        return None, f"HTTP 404：该地址没有成员列表接口（毕业生专用域名可能不提供），可改用同团其他账号查询。({url})"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"

    try:
        groups = normalize_groups(resp.json())
    except ValueError:
        return None, f"响应不是合法 JSON: {resp.text[:200]}"
    if groups is None:
        return None, f"响应结构不认识: {str(resp.json())[:200]}"
    return groups, None
