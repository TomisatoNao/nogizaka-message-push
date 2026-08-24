# ============================================================
# main.py — 兼容启动入口
# ============================================================
import asyncio
import sys

# Windows 终端 Unicode/Emoji 输出与 asyncio Proactor IOCP 保护
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        from asyncio.windows_events import _OverlappedFuture
        _orig_set_exception = _OverlappedFuture.set_exception

        def _safe_set_exception(self, exception):
            if not self.done():
                try:
                    _orig_set_exception(self, exception)
                except asyncio.InvalidStateError:
                    pass

        _OverlappedFuture.set_exception = _safe_set_exception
    except Exception:
        pass

from src.app import main


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise
    sys.exit(0)
