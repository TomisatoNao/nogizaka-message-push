# ============================================================
# watcher.py — 可选的 config.json 文件监控热重载
# ============================================================
# 如果安装了 watchdog（pip install watchdog），启动后自动监控
# config.json 的变更，检测到修改时调用 config.reload() 热重载。
# 未安装 watchdog 时静默跳过，不影响程序正常运行。
# ============================================================
from __future__ import annotations

import time
from pathlib import Path


def start_watcher(
    config_path: Path,
    on_reload: object = None,
    *,
    debounce_seconds: float = 1.0,
):
    """
    启动 config.json 文件监控。

    参数:
        config_path:   config.json 的 Path 对象
        on_reload:     重载成功/失败时的回调，签名为 on_reload(success: bool)
        debounce_seconds: 防抖时间（编辑器可能多次写入）
    返回:
        Observer 实例（用于 observer.stop() 清理），或 None（不可用时）
    """
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        print("ℹ️  watchdog 未安装，config.json 自动热重载不可用")
        print("   如需启用，请执行: pip install watchdog")
        return None

    from config.config import reload as _reload

    _last_reload = 0.0

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event):
            nonlocal _last_reload
            # 只监控 config.json，忽略目录和其他文件
            if not event.src_path.endswith("config.json"):
                return
            # 防抖 — 避免编辑器多次保存连续触发
            now = time.time()
            if now - _last_reload < debounce_seconds:
                return
            _last_reload = now

            ok = _reload()
            if ok:
                print("🔄 config.json 热重载成功")
            else:
                print("🚨 config.json 热重载失败，继续使用旧配置")

            if on_reload is not None:
                on_reload(ok)  # type: ignore[call-arg]

    observer = Observer()
    observer.schedule(_Handler(), str(config_path.parent), recursive=False)
    observer.start()
    print("👁️  config.json 文件监控已启动（自动热重载）")
    return observer
