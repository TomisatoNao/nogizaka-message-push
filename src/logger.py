# ============================================================
# logger.py — 日志系统：初始化、彩色终端输出、文件滚动日志
# ============================================================
import logging
import os
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

import config.config as cfg

# ---- ANSI 彩色支持检测 ----
_ANSI_SUPPORTED: bool = (
    hasattr(sys.stdout, "isatty")
    and sys.stdout.isatty()
    and (
        os.name != "nt"
        or "ANSICON" in os.environ
        or os.environ.get("TERM_PROGRAM") == "vscode"
        or "WT_SESSION" in os.environ
        or "COLORTERM" in os.environ
    )
)

error_logger:    logging.Logger | None = None
response_logger: logging.Logger | None = None

# 终端单行最大长度（超出部分截断，多行内容逐行判断）
_MAX_LINE_LENGTH = 120


def _make_rotating_logger(name: str, filepath: str) -> logging.Logger:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = RotatingFileHandler(
            filepath, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
    return logger


def init_loggers() -> None:
    global error_logger, response_logger
    error_logger    = _make_rotating_logger("error",    cfg.ERROR_LOG_FILE)
    response_logger = _make_rotating_logger("response", cfg.RESPONSE_LOG_FILE)


def redact_sensitive(content: str) -> str:
    """脱敏日志内容中的 token / cookie / key 等敏感信息。"""
    safe = str(content)
    safe = re.sub(
        r'"(access_token|refresh_token|token|session|authorization|cookie|set-cookie)"\s*:\s*"[^"]*"',
        r'"\1":"***HIDDEN***"',
        safe,
        flags=re.IGNORECASE,
    )
    safe = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', "***JWT***", safe)
    safe = re.sub(r'\bBearer\s+[A-Za-z0-9._~+/=-]+', "Bearer ***HIDDEN***", safe, flags=re.IGNORECASE)
    safe = re.sub(r'\bQQBot\s+[A-Za-z0-9._~+/=-]+', "QQBot ***HIDDEN***", safe, flags=re.IGNORECASE)
    safe = re.sub(r'([?&]key=)[^&\s]+', r'\1***HIDDEN***', safe, flags=re.IGNORECASE)
    return safe


def log_all(content: str, *, is_error: bool = False, is_debug: bool = False) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    if is_error:
        tag, color = "ERROR", "\033[31m"
    elif is_debug:
        tag, color = "DEBUG", "\033[90m"
    else:
        tag, color = "INFO ", "\033[32m"

    safe_content = redact_sensitive(content)
    # 逐行截断：多行内容（如状态摘要）每行独立判断，不会被整体切掉
    safe = "\n".join(
        line if len(line) <= _MAX_LINE_LENGTH else line[:_MAX_LINE_LENGTH] + "...[TRUNCATED]"
        for line in safe_content.split("\n")
    )
    if _ANSI_SUPPORTED:
        print(f"{ts} {color}[{tag}]\033[0m {safe}")
    else:
        print(f"{ts} [{tag}] {safe}")

    if not is_debug and error_logger:
        (error_logger.error if is_error else error_logger.info)(safe_content)


def log_response(content: str) -> None:
    if not cfg.DEBUG_LOG_RESPONSE or not response_logger:
        return
    safe = redact_sensitive(content)
    response_logger.debug(safe)


def format_httpx_error(e: Exception) -> str:
    """将 httpx 异常格式化为包含 URL 和底层原因的详细错误信息。"""
    detail = str(e) if str(e) else type(e).__name__
    if hasattr(e, "request") and e.request is not None:
        detail += f" | URL: {e.request.url}"
    if getattr(e, "__cause__", None) is not None:
        cause_msg = str(e.__cause__) if str(e.__cause__) else type(e.__cause__).__name__
        detail += f" | 原因: {cause_msg}"
    return detail
