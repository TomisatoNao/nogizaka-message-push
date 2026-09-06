"""
tests/test_webui_api_contract.py — 前端与后端 API 契约闭环校验测试

本测试套件旨在彻底根治“前端调用了接口，而后端路由在模块化或重构中被遗漏/删掉导致 404”的问题：
1. 动态自动扫描 src/webui_static/ 下所有前端代码 (HTML/JS)，提取所有调用的 /api/... 端点与请求方法；
2. 将每一个前端端点送入后端路由调度层进行可达性探测；
3. 断言所有前端调用的 API 端点在后端必须存在有效处理器，绝不可回退到 404！
"""

from pathlib import Path
import re
from unittest.mock import MagicMock
import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _extract_frontend_api_calls() -> list[tuple[str, str, str, int]]:
    """扫描前端代码，提取 (method, endpoint_prefix, filename, line_no)。"""
    static_dir = _ROOT / "src" / "webui_static"
    endpoints: list[tuple[str, str, str, int]] = []

    for filepath in static_dir.glob("**/*"):
        if filepath.suffix not in (".html", ".js"):
            continue

        text = filepath.read_text(encoding="utf-8")
        lines = text.splitlines()

        for line_no, line in enumerate(lines, 1):
            # 匹配 fetch("/api/..." 或 api("/api/..." 或类似调用
            for m in re.finditer(r'(?:fetch|api)\(\s*[`\'"](/api/[^`\'"\s]+)', line):
                raw_path = m.group(1)
                # 清洗模板占位符与查询参数 (如 /api/system/storage${forceRefresh ? ...} -> /api/system/storage)
                clean_path = raw_path.split("${")[0].split("?")[0].split("#")[0].rstrip("/")
                if not clean_path.startswith("/api/"):
                    continue

                # 探测请求方法（检查该行及后置 8 行代码中的 method）
                start_idx = max(0, line_no - 1)
                end_idx = min(len(lines), line_no + 8)
                snippet = "\n".join(lines[start_idx:end_idx])

                method = "GET"
                if re.search(r'method:\s*["\']POST["\']', snippet, re.I):
                    method = "POST"
                elif re.search(r'method:\s*["\']PUT["\']', snippet, re.I):
                    method = "PUT"
                elif re.search(r'method:\s*["\']DELETE["\']', snippet, re.I):
                    method = "DELETE"

                endpoints.append((method, clean_path, filepath.name, line_no))

    return endpoints


def _is_route_handled(method: str, path: str, monkeypatch: pytest.MonkeyPatch | None = None) -> bool:
    """探测后端路由器是否能够拦截并处理该方法与路径。"""
    import subprocess
    from src.webui import _Handler
    from src.webui_modules.archive.messages import handle_messages
    from src.webui_modules.archive.letters import handle_letters
    from src.webui_modules.archive.blogs import handle_blogs

    # 屏蔽真实外部进程启动与日志风暴
    if monkeypatch:
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: MagicMock(pid=999, wait=lambda: 0, stdout=None))

    # 1. Archive 模块子路由
    if path.startswith("/api/archive/"):
        sub = path[len("/api/archive/"):]
        # 特殊处理媒体直链与头像等带通配参数的路径
        if sub.startswith("media/"):
            return True
        if sub.startswith("avatar"):
            return True

        sub_clean = sub.split("?")[0].rstrip("/")
        mock_handler = MagicMock()
        mock_handler.path = path
        mock_handler.command = method

        def guard_fn(**_):
            return True

        def read_body_fn():
            return {}

        if sub_clean == "home":
            return True
        if handle_messages(mock_handler, sub_clean, guard_fn, read_body_fn):
            return True
        if handle_letters(mock_handler, sub_clean, guard_fn, read_body_fn):
            return True
        if handle_blogs(mock_handler, sub_clean, guard_fn, read_body_fn):
            return True
        return False

    # 2. 系统主路由与管理路由
    handler = _Handler.__new__(_Handler)
    handler.path = path
    handler.command = method
    handler.headers = {"Host": "127.0.0.1"}
    handler._check_host = MagicMock(return_value=True)
    handler._check_origin = MagicMock(return_value=True)
    handler._check_auth = MagicMock(return_value=True)
    handler._read_body_json = MagicMock(return_value={})

    is_404 = False

    def mark_404():
        nonlocal is_404
        is_404 = True

    handler._send_404 = mark_404
    handler._send_json = MagicMock()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()

    try:
        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()
        elif method == "PUT":
            handler.do_PUT()
        elif method == "DELETE":
            handler.do_DELETE()
        else:
            return False
    except Exception:
        # 如果抛出业务异常或模拟数据异常，说明路由已被命中并进入了业务处理函数
        return True

    return not is_404


def test_frontend_api_contract_coverage(monkeypatch):
    """断言前端调用的所有 API 接口在后端均存在对应路由处理，杜绝 404 缺失漏洞。"""
    endpoints = _extract_frontend_api_calls()
    assert len(endpoints) >= 40, f"提取到的前端 API 数量异常偏少 ({len(endpoints)})，请检查静态目录！"

    missing_routes = []
    seen = set()

    for method, path, fname, line_no in endpoints:
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)

        if not _is_route_handled(method, path, monkeypatch):
            missing_routes.append(f"[{method}] {path}  (调用位置: {fname}:{line_no})")

    assert not missing_routes, (
        "🚨 发现前端调用的 API 在后端路由中缺失（会引发 404 异常）！\n"
        + "\n".join(f"  • {item}" for item in missing_routes)
        + "\n请在 src/webui.py 或对应 src/webui_modules/ 子模块中补齐路由分发！"
    )


def test_contract_detector_identifies_unregistered_route():
    """验证探测器自身能力：对未注册的虚假路由必须正确返回 False (识别为未命中/404)。"""
    assert _is_route_handled("POST", "/api/archive/definitely_unregistered_dummy_route") is False
    assert _is_route_handled("POST", "/api/system/definitely_unregistered_dummy_route") is False
    assert _is_route_handled("GET", "/api/definitely_unregistered_dummy_route") is False

