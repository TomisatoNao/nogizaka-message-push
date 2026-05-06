# ============================================================
# main.py — 兼容启动入口
# ============================================================
import asyncio

from src.app import main


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
