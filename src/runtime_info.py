"""运行版本元数据。

Docker 构建会注入这些环境变量；原生 Python 开发环境使用稳定的本地默认值。
"""
from __future__ import annotations

import os


def runtime_metadata() -> dict[str, str]:
    """返回可安全公开在健康接口中的构建信息。"""
    return {
        "version": os.getenv("APP_VERSION", "dev").strip() or "dev",
        "git_sha": os.getenv("APP_GIT_SHA", "unknown").strip() or "unknown",
        "build_time": os.getenv("APP_BUILD_TIME", "unknown").strip() or "unknown",
    }
