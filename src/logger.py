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

error_logger: logging.Logger | None = None
response_logger: logging.Logger | None = None


def _make_rotating_logger(name: str, filepath: str) -> logging.Logger:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = RotatingFileHandler(
            filepath, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        # 改进 1：文件格式加 level 字段，方便 grep
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)-5s] %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
    return logger


def init_loggers() -> None:
    global error_logger, response_logger
    error_logger = _make_rotating_logger("error", ERROR_LOG_FILE)
    response_logger = _make_rotating_logger("response", RESPONSE_LOG_FILE)

    # 改进 5：每次启动写入会话分隔线，便于区分重启边界
    pid = os.getpid()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sep = f"{'=' * 20} 程序启动 PID={pid} @ {ts} {'=' * 20}"
    error_logger.info(sep)


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


# 改进 1：新增 WARN 级别，用于"严重但未崩溃"场景（如 Token 严重过期）
def log_warn(content: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    safe = content if len(content) <= 120 else content[:120] + "...[TRUNCATED]"
    if _ANSI_SUPPORTED:
        print(f"{ts} \033[33m[WARN ]\033[0m {safe}")
    else:
        print(f"{ts} [WARN ] {safe}")
    if error_logger:
        error_logger.warning(content)


# 改进 2：增加 member_name 参数，每行加 [member=xxx] 前缀
def log_response(content: str, member_name: str = "") -> None:
    if not DEBUG_LOG_RESPONSE or not response_logger:
        return
    # 脱敏：隐藏 token / JWT
    safe = re.sub(
        r'"(access_token|token|session|refresh_token)":"[^"]*"',
        r'"\1":"***HIDDEN***"',
        content,
    )
    safe = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', "***JWT***", safe)
    # 改进 2：加成员前缀，方便 grep "member=片山紗希"
    prefix = f"[member={member_name}] " if member_name else ""
    response_logger.debug(f"{prefix}{safe}")
