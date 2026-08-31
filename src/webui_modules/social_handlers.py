"""WebUI 社媒凭证与成员订阅状态接口。"""

from __future__ import annotations

import asyncio
import os

from src.webui_modules.static_handler import send_json


def handle_subscriptions(handler) -> None:
    """GET /api/subscriptions：返回本地缓存的成员订阅状态。"""
    from src.member_directory import get_all_subscriptions

    send_json(handler, {"ok": True, "subscriptions": get_all_subscriptions()})


def handle_subscriptions_sync(handler) -> bool:
    """POST /api/subscriptions/sync：向官方服务同步订阅状态。"""
    try:
        from src.member_directory import get_all_subscriptions, sync_all_accounts_subscriptions

        stats = asyncio.run(sync_all_accounts_subscriptions())
        send_json(handler, {"ok": True, "stats": stats, "subscriptions": get_all_subscriptions()})
        return True
    except Exception as exc:
        send_json(handler, {"ok": False, "errors": [f"同步订阅状态失败: {type(exc).__name__}: {exc}"]}, 500)
        return False


def handle_ig_session_status(handler) -> None:
    """GET /api/social/ig_session：返回 Instagram Cookie 的脱敏状态。"""
    from src.social import ig_session

    send_json(handler, {"ok": True, **ig_session.status()})


def _get_proxy(load_raw_config) -> str:
    """解析社媒检查所使用的代理地址。"""
    try:
        raw_cfg = load_raw_config()
    except Exception:
        raw_cfg = {}
    proxy = ""
    if isinstance(raw_cfg, dict):
        social_cfg = raw_cfg.get("social") or {}
        proxy = raw_cfg.get("proxy") or (social_cfg.get("proxy") if isinstance(social_cfg, dict) else "") or ""
    try:
        import config.config as app_cfg

        proxy = proxy or getattr(app_cfg, "PROXY", "")
    except Exception:
        pass
    return str(proxy).strip()


def handle_ig_session_save(handler, body: dict, *, load_raw_config, trigger_reload,
                           update_env_file, mutation_lock) -> tuple[bool, int]:
    """POST /api/social/ig_session：解析并持久化 Instagram Cookies。"""
    raw_cookies = str(body.get("cookies") or body.get("raw") or "").strip()
    if not raw_cookies:
        send_json(handler, {"ok": False, "errors": ["Cookie 内容不能为空"]}, 400)
        return False, 0

    from src.social import ig_session

    cookies = ig_session.parse_cookies(raw_cookies)
    if not cookies:
        send_json(handler, {"ok": False, "errors": ["未能解析出任何有效 Cookie，请检查格式"]}, 400)
        return False, 0

    env_updates = {}
    if cookies.get("sessionid"):
        env_updates["INSTAGRAM_SESSIONID"] = cookies["sessionid"]
    if cookies.get("ds_user_id"):
        env_updates["INSTAGRAM_DS_USER_ID"] = cookies["ds_user_id"]

    try:
        with mutation_lock:
            ig_session.write_cookie_file(cookies)
            if env_updates:
                update_env_file(env_updates)
                os.environ.update(env_updates)
            reloaded = trigger_reload()
    except Exception as exc:
        send_json(handler, {"ok": False, "errors": [f"保存 Instagram Cookies 失败: {type(exc).__name__}: {exc}"]}, 500)
        return False, 0

    assessment = ig_session.assess(cookies)
    health = ig_session.check_session(cookies, proxy=_get_proxy(load_raw_config))
    send_json(handler, {
        "ok": True,
        "assessment": assessment,
        "health": health,
        "reloaded": reloaded,
        "status": ig_session.status(),
    })
    return True, len(cookies)


def handle_ig_session_check(handler, *, load_raw_config) -> bool:
    """POST /api/social/ig_session/check：检测当前 Cookie 的有效性。"""
    from src.social import ig_session

    cookies = ig_session.read_cookie_file()
    if not cookies:
        send_json(handler, {"ok": False, "errors": ["尚未配置任何 Instagram Cookies"]}, 400)
        return False
    health = ig_session.check_session(cookies, proxy=_get_proxy(load_raw_config))
    send_json(handler, {
        "ok": True,
        "health": health,
        "assessment": ig_session.assess(cookies),
        "status": ig_session.status(),
    })
    return True


def handle_ig_session_clear(handler, *, trigger_reload, update_env_file, mutation_lock) -> bool:
    """POST /api/social/ig_session/clear：清理 Cookie 与兼容环境变量。"""
    from src.social import ig_session

    try:
        with mutation_lock:
            ig_session.clear()
            update_env_file({}, remove=["INSTAGRAM_SESSIONID", "INSTAGRAM_DS_USER_ID"])
            os.environ.pop("INSTAGRAM_SESSIONID", None)
            os.environ.pop("INSTAGRAM_DS_USER_ID", None)
            reloaded = trigger_reload()
    except Exception as exc:
        send_json(handler, {"ok": False, "errors": [f"清理 Instagram Cookies 失败: {type(exc).__name__}: {exc}"]}, 500)
        return False
    send_json(handler, {"ok": True, "reloaded": reloaded, "status": ig_session.status()})
    return True
