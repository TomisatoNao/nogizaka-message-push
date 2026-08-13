# ============================================================
# main.py — 兼容启动入口
# ============================================================
import asyncio
import sys

# Windows 终端 Unicode/Emoji 输出兼容保护
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.app import main


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
