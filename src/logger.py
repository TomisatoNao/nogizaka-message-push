# ============================================================
# logger.py — 日志系统：初始化、彩色终端输出、文件滚动日志
# ============================================================
import logging
import os
import re
import sys
import threading
from collections import deque
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
system_logger:   logging.Logger | None = None
response_logger: logging.Logger | None = None

# 终端单行最大长度（超出部分截断，多行内容逐行判断）
_MAX_LINE_LENGTH = 120

# ---- 内存日志环（供网页管理端实时查看；内容已脱敏，含 DEBUG 级）----
_RECENT_MAX = 500
_recent: deque = deque(maxlen=_RECENT_MAX)
_recent_lock = threading.Lock()
_recent_seq = 0


def _push_recent(level: str, ts: str, text: str) -> None:
    global _recent_seq
    with _recent_lock:
        _recent_seq += 1
        _recent.append({"seq": _recent_seq, "ts": ts, "level": level, "text": text[:4000]})


def get_recent(after: int = 0) -> tuple[list[dict], int]:
    """返回 seq > after 的日志条目和当前最大 seq（供增量轮询）。"""
    with _recent_lock:
        return [e for e in _recent if e["seq"] > after], _recent_seq


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
    global error_logger, system_logger, response_logger
    error_logger    = _make_rotating_logger("error",    cfg.ERROR_LOG_FILE)
    system_logger   = _make_rotating_logger("system",   getattr(cfg, "SYSTEM_LOG_FILE", "logs/system_info.log"))
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
    # 逐行处理与格式化，确保控制台输出每一行都具备规范统一的 [时间戳 级别] 前缀
    lines = [
        line if len(line) <= _MAX_LINE_LENGTH else line[:_MAX_LINE_LENGTH] + "...[TRUNCATED]"
        for line in safe_content.split("\n")
    ]
    for line in lines:
        if not line.strip() and len(lines) > 1:
            continue
        try:
            if _ANSI_SUPPORTED:
                print(f"{ts} {color}[{tag}]\033[0m {line}")
            else:
                print(f"{ts} [{tag}] {line}")
        except UnicodeEncodeError:
            line_fallback = line.encode("gbk", "replace").decode("gbk")
            if _ANSI_SUPPORTED:
                print(f"{ts} {color}[{tag}]\033[0m {line_fallback}")
            else:
                print(f"{ts} [{tag}] {line_fallback}")
        except Exception:  # nosec B110
            pass

    _push_recent(tag.strip(), ts, safe_content)

    if is_error and error_logger:
        error_logger.error(safe_content)
    elif not is_error and system_logger:
        if is_debug:
            system_logger.debug(safe_content)
        else:
            system_logger.info(safe_content)


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
