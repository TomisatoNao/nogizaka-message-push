"""
social/ig_safety.py — Instagram 风控防护闸门

Instagram 对自动化访问的判定非常严，一旦被判定异常，轻则要求 checkpoint
验证，重则限制账号功能甚至封号。**账号一旦出事无法挽回**，因此这里采取
"宁可少抓、不可冒进"的策略：所有 Instagram 网络行为都必须经过本闸门。

防护手段（按对降低风险的贡献排序）：

  1. **低频轮询** —— 最关键。真人不会每 90 秒刷一次主页。默认 30 分钟，
     并叠加 ±25% 随机抖动，避免出现机器般精确的固定节律。
  2. **熔断** —— 连续遇到 401/403/429 时直接停掉 Instagram 数小时，
     而不是继续重试。持续撞墙是账号出事的最主要原因。
  3. **请求间隔与配额** —— 单次轮询内也不允许连续快速请求；
     每小时总请求数设硬上限，超了就等下一个小时窗口。
  4. **静默时段** —— 深夜不轮询。24 小时不间断访问本身就是自动化特征。
  5. **Story 单独限频** —— Story 是强登录态接口，审查更严，默认比 Feed 更慢。

以上都可以在 `platforms.instagram.safety` 里调整，但**默认值已按"安全优先"
设定**，不建议调激进。
"""

import logging
import random
import threading
import time

log = logging.getLogger("collink")

DEFAULTS = {
    "enabled": True,
    # 两次请求之间的最小间隔（秒）—— 防止一轮里连续快速打接口
    "min_request_gap": 15,
    # 每小时最多请求次数（含 yt-dlp 的解析与下载）
    "max_requests_per_hour": 40,
    # 连续多少次鉴权/限流失败后熔断
    "failure_threshold": 3,
    # 熔断后暂停多久（秒），默认 6 小时
    "cooldown_seconds": 6 * 3600,
    # 静默时段（本地时间小时，左闭右开）；期间完全不请求
    "quiet_hours": [1, 7],
    # 轮询间隔随机抖动比例
    "jitter": 0.25,
}


def settings(config: dict) -> dict:
    icfg = (config.get("platforms") or {}).get("instagram") or {}
    out = dict(DEFAULTS)
    raw = icfg.get("safety")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out:
                out[k] = v
    return out


class Blocked(RuntimeError):
    """当前不允许请求（熔断中 / 静默时段 / 超配额）。"""


class IgSafety:
    """Instagram 请求闸门（单例，全局共享计数与熔断状态）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._last_request = 0.0
        self._window_start = time.time()
        self._window_count = 0
        self._fail_streak = 0
        self._blocked_until = 0.0
        self._block_reason = ""
        self._total = 0

    # ── 准入 ────────────────────────────────────────────

    def check(self, config: dict, *, what: str = "请求") -> None:
        """请求前调用。不允许时抛 Blocked（调用方应安静跳过本轮）。"""
        s = settings(config)
        if not s.get("enabled", True):
            return
        now = time.time()
        with self._lock:
            if now < self._blocked_until:
                left = int(self._blocked_until - now)
                raise Blocked(
                    f"Instagram 已熔断（{self._block_reason}），"
                    f"还需等待 {left // 60} 分钟。这是为保护账号而主动暂停。")

            qh = s.get("quiet_hours") or []
            if len(qh) == 2:
                start, end = int(qh[0]) % 24, int(qh[1]) % 24
                # 起止相同 = 零长度窗口 = 不启用静默（若按跨天逻辑处理，
                # hour < end 恒真会误判成全天静默）
                if start != end:
                    hour = time.localtime(now).tm_hour
                    in_quiet = (start <= hour < end if start < end
                                else hour >= start or hour < end)
                    if in_quiet:
                        raise Blocked(f"处于静默时段（{start}:00-{end}:00），"
                                      f"暂不访问 Instagram")

            # 每小时配额
            if now - self._window_start >= 3600:
                self._window_start = now
                self._window_count = 0
            limit = int(s.get("max_requests_per_hour", 40))
            if self._window_count >= limit:
                left = int(3600 - (now - self._window_start))
                raise Blocked(f"本小时 Instagram 请求已达上限 {limit} 次，"
                              f"{left // 60} 分钟后重置")

            # 最小间隔
            gap = float(s.get("min_request_gap", 15))
            wait = gap - (now - self._last_request)
        if wait > 0:
            time.sleep(min(wait, gap))
        with self._lock:
            self._last_request = time.time()
            self._window_count += 1
            self._total += 1

    # ── 结果反馈 ────────────────────────────────────────

    def record_ok(self) -> None:
        with self._lock:
            self._fail_streak = 0

    def record_failure(self, config: dict, status: int, detail: str = "") -> bool:
        """记录一次鉴权/限流失败。返回 True 表示**刚刚触发熔断**。"""
        s = settings(config)
        with self._lock:
            self._fail_streak += 1
            if self._fail_streak < int(s.get("failure_threshold", 3)):
                log.warning("[instagram] 风控信号 %s（连续 %s 次，达到 %s 次将熔断）",
                            status, self._fail_streak,
                            s.get("failure_threshold", 3))
                return False
            cooldown = float(s.get("cooldown_seconds", 6 * 3600))
            self._blocked_until = time.time() + cooldown
            self._block_reason = f"连续 {self._fail_streak} 次 {status}{detail}"
            self._fail_streak = 0
        log.error("[instagram] 🛑 已熔断 %.1f 小时以保护账号（%s）。"
                  "期间完全不访问 Instagram —— 请到后台检查登录态是否失效。",
                  cooldown / 3600, self._block_reason)
        return True

    def reset(self) -> None:
        """手动解除熔断（后台按钮用）。"""
        with self._lock:
            self._blocked_until = 0.0
            self._block_reason = ""
            self._fail_streak = 0

    # ── 状态 ────────────────────────────────────────────

    def status(self, config: dict) -> dict:
        s = settings(config)
        now = time.time()
        with self._lock:
            return {
                "enabled": bool(s.get("enabled", True)),
                "blocked": now < self._blocked_until,
                "blocked_until": self._blocked_until,
                "blocked_seconds_left": max(0, int(self._blocked_until - now)),
                "block_reason": self._block_reason,
                "fail_streak": self._fail_streak,
                "requests_this_hour": self._window_count,
                "hourly_limit": int(s.get("max_requests_per_hour", 40)),
                "total_requests": self._total,
                "settings": s,
            }


_guard: IgSafety | None = None
_guard_lock = threading.Lock()


def get_guard() -> IgSafety:
    global _guard
    with _guard_lock:
        if _guard is None:
            _guard = IgSafety()
    return _guard


def jittered_interval(base: int, config: dict) -> int:
    """给轮询间隔加随机抖动，避免机器般精确的固定节律。"""
    j = float(settings(config).get("jitter", 0.25))
    return max(60, int(base * random.uniform(1 - j, 1 + j)))
