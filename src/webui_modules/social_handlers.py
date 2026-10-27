"""WebUI 社媒凭证与成员订阅状态接口。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

from src.logger import log_all
from src.webui_modules.static_handler import send_json


_IG_ENV_KEYS = ("INSTAGRAM_SESSIONID", "INSTAGRAM_DS_USER_ID")


def _ig_env_path(env_path=None) -> Path:
    if env_path is not None:
        return Path(env_path)
    # The save route injects an update callback already bound to webui.ENV_PATH.
    # Read the same path here so rollback covers that exact file in tests and at runtime.
    from src import webui

    return Path(webui.ENV_PATH)


def _read_file_snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore_file_if_changed(path: Path, before: bytes | None) -> None:
    if _read_file_snapshot(path) == before:
        return
    from src.webui_modules.config_service import _restore_file_bytes

    _restore_file_bytes(path, before)


def _snapshot_process_env(keys: tuple[str, ...]) -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in keys}


def _restore_process_env(before: dict[str, str | None]) -> None:
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _rollback_ig_env_commit(
    *, path: Path | None, file_before: bytes | None,
    process_before: dict[str, str | None], action: str,
) -> None:
    failure = None
    try:
        if path is not None:
            _restore_file_if_changed(path, file_before)
    except Exception as exc:
        failure = exc
    finally:
        try:
            _restore_process_env(process_before)
        except Exception as exc:
            if failure is None:
                failure = exc

    if failure is not None:
        # Never include exception text here: a failed storage operation may carry
        # cookie values in its diagnostic string.
        log_all(f"🚨 {action}后回滚凭据状态失败 ({type(failure).__name__})", is_error=True)
        raise RuntimeError("Instagram credential rollback failed") from failure


def handle_subscriptions(handler) -> None:
    """GET /api/subscriptions：返回本地缓存的成员订阅状态。"""
    from src.member_directory import get_all_subscriptions

    send_json(handler, {"ok": True, "subscriptions": get_all_subscriptions()})


def handle_subscriptions_sync(handler) -> bool:
    """POST /api/subscriptions/sync：向官方服务同步订阅状态。"""
    try:
        from src.member_directory import get_all_subscriptions, sync_all_accounts_subscriptions

        stats, failures = asyncio.run(sync_all_accounts_subscriptions(include_errors=True))
        if failures and not stats:
            send_json(handler, {
                "ok": False,
                "errors": [f"{account}: {reason}" for account, reason in failures.items()],
                "failure_details": failures,
                "stats": stats,
            }, 502)
            return False
        send_json(handler, {
            "ok": True,
            "partial": bool(failures),
            "warnings": failures,
            "stats": stats,
            "subscriptions": get_all_subscriptions(),
        })
        return True
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        log_all(f"🚨 WebUI 订阅同步失败: {type(exc).__name__}: {exc}", is_error=True)
        send_json(handler, {"ok": False, "errors": [f"同步订阅状态失败: {type(exc).__name__}"]}, 500)
        return False
    except Exception as exc:  # 最后兜底：请求线程不崩溃，但错误必须可追踪。
        log_all(f"🚨 WebUI 订阅同步未处理异常: {type(exc).__name__}: {exc}", is_error=True)
        send_json(handler, {"ok": False, "errors": ["同步订阅状态发生内部错误，请查看系统日志"]}, 500)
        return False


def handle_ig_session_status(handler) -> None:
    """GET /api/social/ig_session：返回 Instagram Cookie 的脱敏状态。"""
    from src.social import ig_session

    send_json(handler, {"ok": True, **ig_session.status()})


def _get_proxy(load_raw_config) -> str:
    """解析社媒检查所使用的代理地址。"""
    try:
        raw_cfg = load_raw_config()
    except (OSError, ValueError) as exc:
        log_all(f"⚠️ 读取社媒代理配置失败，已回退默认代理: {type(exc).__name__}: {exc}", is_error=True)
        raw_cfg = {}
    proxy = ""
    if isinstance(raw_cfg, dict):
        social_cfg = raw_cfg.get("social") or {}
        proxy = raw_cfg.get("proxy") or (social_cfg.get("proxy") if isinstance(social_cfg, dict) else "") or ""
    try:
        import src.config.config as app_cfg

        proxy = proxy or getattr(app_cfg, "PROXY", "")
    except ImportError:
        # 独立工具模式下配置模块可能不可用，保留空代理作为安全降级。
        pass
    return str(proxy).strip()


def handle_ig_session_save(handler, body: dict, *, load_raw_config, trigger_reload,
                           update_env_file, mutation_lock, expected_revision,
                           check_revision_locked, get_revision) -> tuple[bool, int]:
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
            if check_revision_locked(handler, expected_revision) is None:
                return False, 0
            env_path = _ig_env_path() if env_updates else None
            env_before = _read_file_snapshot(env_path) if env_path is not None else None
            process_before = _snapshot_process_env(tuple(env_updates))
            try:
                if env_updates:
                    update_env_file(env_updates)
                    os.environ.update(env_updates)
                # The SQLite write is transactional. If it fails, restore the
                # already-atomic .env update and the process environment.
                ig_session.write_cookie_file(cookies)
            except Exception:
                _rollback_ig_env_commit(
                    path=env_path,
                    file_before=env_before,
                    process_before=process_before,
                    action="保存 Instagram Cookies",
                )
                raise
            reload_result = trigger_reload()
            reloaded = bool(reload_result.get("reloaded")) if isinstance(reload_result, dict) else bool(reload_result)
            revision = get_revision()
    except (OSError, sqlite3.Error, ValueError) as exc:
        log_all(f"🚨 保存 Instagram Cookies 失败 ({type(exc).__name__})", is_error=True)
        send_json(handler, {"ok": False, "errors": [f"保存 Instagram Cookies 失败: {type(exc).__name__}"]}, 500)
        return False, 0
    except Exception as exc:  # 最终边界：不能泄露 Cookie 内容到响应。
        log_all(f"🚨 保存 Instagram Cookies 未处理异常 ({type(exc).__name__})", is_error=True)
        send_json(handler, {"ok": False, "errors": ["保存 Instagram Cookies 发生内部错误，请查看系统日志"]}, 500)
        return False, 0

    assessment = ig_session.assess(cookies)
    health = ig_session.check_session(cookies, proxy=_get_proxy(load_raw_config))
    response = {
        "ok": True,
        "assessment": assessment,
        "health": health,
        "reloaded": reloaded,
        "revision": revision,
        "status": ig_session.status(),
    }
    if isinstance(reload_result, dict) and isinstance(reload_result.get("reload"), dict):
        response["reload"] = reload_result["reload"]
    send_json(handler, response)
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


def handle_ig_session_clear(handler, *, trigger_reload, update_env_file, mutation_lock,
                            expected_revision, check_revision_locked, get_revision,
                            env_path=None) -> bool:
    """POST /api/social/ig_session/clear：清理 Cookie 与兼容环境变量。"""
    from src.social import ig_session

    try:
        with mutation_lock:
            if check_revision_locked(handler, expected_revision) is None:
                return False
            resolved_env_path = _ig_env_path(env_path)
            env_before = _read_file_snapshot(resolved_env_path)
            process_before = _snapshot_process_env(_IG_ENV_KEYS)
            try:
                update_env_file(
                    {}, path=resolved_env_path,
                    remove=list(_IG_ENV_KEYS),
                )
                for key in _IG_ENV_KEYS:
                    os.environ.pop(key, None)
                # The SQLite transaction is the second persistence step. If it
                # fails, put back the exact .env bytes and process values.
                ig_session.clear()
            except Exception:
                _rollback_ig_env_commit(
                    path=resolved_env_path,
                    file_before=env_before,
                    process_before=process_before,
                    action="清理 Instagram Cookies",
                )
                raise
            reload_result = trigger_reload()
            reloaded = bool(reload_result.get("reloaded")) if isinstance(reload_result, dict) else bool(reload_result)
            revision = get_revision()
    except (OSError, sqlite3.Error, ValueError) as exc:
        log_all(f"🚨 清理 Instagram Cookies 失败 ({type(exc).__name__})", is_error=True)
        send_json(handler, {"ok": False, "errors": [f"清理 Instagram Cookies 失败: {type(exc).__name__}"]}, 500)
        return False
    except Exception as exc:  # 最终边界：保持请求线程可用并记录完整诊断。
        log_all(f"🚨 清理 Instagram Cookies 未处理异常 ({type(exc).__name__})", is_error=True)
        send_json(handler, {"ok": False, "errors": ["清理 Instagram Cookies 发生内部错误，请查看系统日志"]}, 500)
        return False
    response = {"ok": True, "reloaded": reloaded, "revision": revision, "status": ig_session.status()}
    if isinstance(reload_result, dict) and isinstance(reload_result.get("reload"), dict):
        response["reload"] = reload_result["reload"]
    send_json(handler, response)
    return True
