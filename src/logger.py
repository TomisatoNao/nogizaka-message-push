# ============================================================
# logger.py — 日志系统：初始化、彩色终端输出、文件滚动日志
# ============================================================
import logging
import os
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from config.config import DEBUG_LOG_RESPONSE, ERROR_LOG_FILE, RESPONSE_LOG_FILE

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
    error_logger    = _make_rotating_logger("error",    ERROR_LOG_FILE)
    response_logger = _make_rotating_logger("response", RESPONSE_LOG_FILE)


def log_all(content: str, *, is_error: bool = False, is_debug: bool = False) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    if is_error:
        tag, color = "ERROR", "\033[31m"
    elif is_debug:
        tag, color = "DEBUG", "\033[90m"
    else:
        tag, color = "INFO ", "\033[32m"

    safe = content if len(content) <= 120 else content[:120] + "...[TRUNCATED]"
    if _ANSI_SUPPORTED:
        print(f"{ts} {color}[{tag}]\033[0m {safe}")
    else:
        print(f"{ts} [{tag}] {safe}")

    if not is_debug and error_logger:
        (error_logger.error if is_error else error_logger.info)(content)


def log_response(content: str) -> None:
    if not DEBUG_LOG_RESPONSE or not response_logger:
        return
    # 脱敏：隐藏 token / JWT
    safe = re.sub(
        r'"(access_token|token|session|refresh_token)":"[^"]*"',
        r'"\1":"***HIDDEN***"',
        content,
    )
    safe = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', "***JWT***", safe)
    response_logger.debug(safe)
