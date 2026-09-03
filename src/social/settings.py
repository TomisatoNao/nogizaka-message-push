"""
social/settings.py — 社交模块配置读取助手

集中所有默认值，避免魔法数字散落各处。所有配置都从既有 config.json 读取，
**不新增与 gemini / qq_bot 重复的配置项**：

    platforms.x           X（Twitter）
    platforms.instagram   Instagram
    platforms.tiktok      TikTok
    platforms.tiktok_live TikTok 直播监控与录制
    media.*               全平台共享的下载参数（线程数 / 重试次数 / 超时）
    social.*              社交模块总开关（异步 / 热更新）

每个 getter 都是「配置 → 带默认值的 dict」，读不到就用默认值，
因此老配置文件（没有这些字段）可以直接运行，完全向后兼容。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# 本模块管辖的平台名（sync_manager 用它判断走哪套推送格式）
SOCIAL_PLATFORMS = ("x", "instagram", "tiktok", "tiktok_live")

# ── 平台默认配置 ───────────────────────────────────────────
_PLATFORM_DEFAULTS: dict[str, dict] = {
    "x": {
        "enabled": False,
        "interval_seconds": 60,
        "night_interval_seconds": 0,      # 0 = 与 interval_seconds 相同
        "interval_jitter_min": 0.9,
        "interval_jitter_max": 1.1,
        "accounts": [],                   # screen_name 列表（不带 @）
        "display_names": {},              # screen_name -> 展示名
        "include_retweets": False,        # 是否推送转推
        "include_quotes": True,           # 是否推送引用推文（并附带被引用内容）
        "include_replies": False,         # 是否推送回复
        "backends": ["syndication", "nitter", "apiv2"],
        "nitter_instances": [
            "https://nitter.net",
            "https://nitter.privacydev.net",
        ],
        "bearer_token": "",               # nosec B105 -- 可选：官方 API v2 Bearer Token
        "fetch_alt_text": True,           # 抓取图片 alt 描述并一并翻译
        "download_dir": "data/social_media/x",
        "max_items_per_poll": 5,          # 单次轮询最多处理几条（防止首次刷屏）
        "first_run_skip": True,           # 首次运行只记录不推送（避免历史刷屏）
    },
    "instagram": {
        "enabled": False,
        # ⚠️ Instagram 对自动化访问判定严格，账号出事无法挽回。
        # 以下默认值按「安全优先」设定：30 分钟一轮 + 抖动，远低于其它平台。
        # 真人不会每分钟刷主页，高频轮询是被判定为机器人的首要特征。
        # **每轮在区间内重新随机取值**（30~60 分钟）。固定间隔即使加了抖动
        # 仍然有规律可循，而真人的访问间隔是散乱的 —— 区间随机更像人。
        "interval_range_seconds": [1800, 3600],
        "night_interval_range_seconds": [5400, 10800],   # 夜间 90~180 分钟
        "interval_seconds": 1800,         # 兜底（未配置区间时使用）
        "night_interval_seconds": 5400,
        "interval_jitter_min": 0.75,
        "interval_jitter_max": 1.25,
        # Story 是强登录态接口，审查更严，单独用更低的频率（60~120 分钟）
        "story_interval_seconds": 3600,
        "story_interval_range_seconds": [3600, 7200],
        "accounts": [],
        "display_names": {},
        "include_feed": True,             # Feed 帖子（图片 / 多图 / Reel）
        "include_stories": True,          # Story（图片 / 视频）
        "cookies_file": "",               # 后台粘贴 cookies 后自动填入
        "cookies_from_browser": "",       # 或直接读浏览器，如 "chrome" / "edge"
        "user_agent": "",                 # 建议与导出 cookies 的浏览器一致
        "download_dir": "data/social_media/instagram",
        "max_items_per_poll": 3,          # 单轮少取一些，减少请求
        "first_run_skip": True,
        # 单条公开帖子/ Reel 的匿名 Embed 回退；不读取 Instagram 登录态。
        "public_embed_enabled": True,
        "public_embed_timeout_seconds": 25,
        "public_embed_max_media": 20,
        # 风控防护（详见 social/ig_safety.py）
        "safety": {
            "enabled": True,
            "min_request_gap": 5,
            "max_requests_per_hour": 120,
            "failure_threshold": 3,
            "cooldown_seconds": 7200,             # 登录态失效熔断 2 小时
            "rate_limit_cooldown_seconds": 1800,  # 429 限流熔断 30 分钟
            "quiet_hours": [1, 7],                # 凌晨 1-7 点完全不访问
            "jitter": 0.25,
        },
    },
    "tiktok": {
        "enabled": False,
        "interval_seconds": 120,
        "night_interval_seconds": 0,
        "interval_jitter_min": 0.9,
        "interval_jitter_max": 1.1,
        "accounts": [],
        "display_names": {},
        "include_stories": True,          # Story
        "include_photos": True,           # 图文 Post（轮播图）
        "cookies_file": "",
        "cookies_from_browser": "",
        "download_dir": "data/social_media/tiktok",
        "max_items_per_poll": 5,
        "first_run_skip": True,
    },
    "tiktok_live": {
        "enabled": False,
        # 开播检测间隔。TikTok 无开播推送接口，只能高频轮询；
        # 配合 fast_detect 的轻量探测（约 120 字节 / 0.3s），8s 轮询成本很低。
        "interval_seconds": 8,
        "night_interval_seconds": 0,      # 直播不分昼夜，保持同一频率
        "interval_jitter_min": 1.0,
        "interval_jitter_max": 1.0,       # 直播检测不做抖动，保证及时
        "fast_detect": True,              # 用 webcast 轻量接口探测（关闭则退回 yt-dlp）
        "accounts": [],
        "display_names": {},
        "cookies_file": "",
        "cookies_from_browser": "",
        "output_dir": "recordings/tiktok_live",   # 录像保存目录
        # 录制期间一律写 MPEG-TS（流式容器，进程被强杀也不会损坏）；
        # format=mp4 时结束后无损 remux 成标准 MP4，remux_to_mp4=false 则保留 TS。
        "format": "mp4",                  # 首选容器；失败自动回退 flv → ts
        "remux_to_mp4": True,             # 关掉则直接保留 TS（不再调用 ffmpeg 转封装）
        "quality": "best",                # best = 最高画质（保留原始音轨）
        "segment_enabled": True,          # 长直播自动分段
        "segment_minutes": 30,            # 每段时长（分钟）
        "end_grace_seconds": 90,          # 断流后等待多久判定直播结束（短暂断流会续录）
        "reconnect_delay_seconds": 5,     # 断流重连间隔
        "auto_send_recording": True,      # 录制完成后自动发送视频文件
        "max_send_bytes": 200 * 1024 * 1024, # QQ 分片上传硬上限
        "split_oversize": True,           # 超限时用 ffmpeg 再切小段发送
        "download_url_base": "",          # 配置后超限文件改为推送下载地址
        "verify_recording": True,         # 用 ffprobe 检查录像完整性
    },
}

# ── 媒体下载默认配置（全平台共享）─────────────────────────
_MEDIA_DEFAULTS = {
    "download_threads": 4,        # 并发下载线程数
    "retry_times": 3,             # 单个文件失败重试次数
    "retry_backoff_seconds": 2,   # 重试退避基数（指数增长）
    "timeout_seconds": 60,        # 单次请求超时
    "ytdlp_socket_timeout": 30,   # yt-dlp socket 超时
    "mobile_video_transcode": True,  # TikTok HEVC 自动转 H.264/AAC 兼容手机浏览器
    "ffmpeg_path": "",            # 留空自动从 PATH 查找
    "ffprobe_path": "",
}

# ── 社交模块总开关 ─────────────────────────────────────────
_SOCIAL_DEFAULTS = {
    "async_enabled": True,            # watch 模式下每平台独立线程
    "hot_reload": True,               # 监听 config.json 变化并热更新
    "hot_reload_check_seconds": 20,   # 热更新检查间隔
    "max_text_chars": 1500,           # 单条 QQ 文本消息最大长度（超出自动分条）
    "translate": True,                # 是否对社交平台文本调用 Gemini 翻译
    "idle_sleep_seconds": 30,         # 平台被禁用时线程的空转间隔
    "error_backoff_seconds": 60,      # 单平台异常后的退避基数
    "error_backoff_max": 900,         # 退避上限
}


# ── 运行时通道配置视图 ─────────────────────────────────────
#
# 社交监控器会持有 config.json 的可变字典，以便 manager 热重载时原位更新。
# 投递适配器不应再直接读取 config.config 的模块级变量：那会让测试无法隔离，
# 也会让 WebUI 传入的临时配置与实际投递结果不一致。RuntimeConfig 以注入的
# 字典为第一优先级，旧代码没有传入对应字段时再回退到全局 facade，保证旧
# 配置文件和已有调用方继续工作。

_MISSING = object()


def _as_bool(value: Any, default: bool = False) -> bool:
    """把 JSON/.env 常见值转换为布尔值，不把字符串 ``"false"`` 当真。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "no", "off", "none", "null"}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
    return bool(value)


class RuntimeConfig:
    """提供社交投递所需的最小配置读取接口。

    ``raw_config`` 可以是 WebUI 读取的 JSONC（含 ``channels``）或应用启动
    后的规范化字典（含 ``enable_*``）。对象只保存字典引用而不复制内容，
    因此 ``manager.reload_social_service`` 原位更新配置后，下一次投递立即
    使用新值。
    """

    _ALIASES: dict[str, tuple[str, ...]] = {
        "ENABLE_TG_BOT": ("enable_tg_bot", "tg_enabled"),
        "ENABLE_NAPCAT_QQ": ("enable_napcat_qq", "napcat_enabled"),
        "ENABLE_QQ_OFFICIAL_BOT": (
            "enable_qq_official_bot",
            "qq_official_enabled",
        ),
        "NAPCAT_ROUTES": ("napcat_routes",),
    }
    _CHANNEL_KEYS = {
        "tg": "ENABLE_TG_BOT",
        "telegram": "ENABLE_TG_BOT",
        "napcat": "ENABLE_NAPCAT_QQ",
        "qq": "ENABLE_NAPCAT_QQ",
        "qq_official": "ENABLE_QQ_OFFICIAL_BOT",
        "official": "ENABLE_QQ_OFFICIAL_BOT",
    }
    _CHANNEL_ALIASES = {
        "tg": ("tg", "telegram"),
        "telegram": ("tg", "telegram"),
        "napcat": ("napcat", "qq"),
        "qq": ("napcat", "qq"),
        "qq_official": ("qq_official", "official"),
        "official": ("qq_official", "official"),
    }

    def __init__(self, raw_config: Mapping[str, Any] | None = None):
        self._raw: Mapping[str, Any] = (
            raw_config if isinstance(raw_config, Mapping) else {}
        )

    @property
    def raw_config(self) -> Mapping[str, Any]:
        """返回当前注入的配置视图（只读语义，不复制）。"""
        return self._raw

    def _raw_value(self, name: str) -> Any:
        """读取注入配置，未提供时返回哨兵。"""
        if name in self._raw:
            return self._raw[name]
        canonical = str(name).upper()
        for alias in self._ALIASES.get(canonical, (str(name).lower(),)):
            if alias in self._raw:
                return self._raw[alias]
        return _MISSING

    def value(self, name: str, default: Any = None) -> Any:
        """读取值：注入字典优先，兼容地回退至 config facade。"""
        value = self._raw_value(name)
        if value is not _MISSING:
            return value
        try:
            import config.config as cfg
            canonical = str(name).upper()
            return getattr(cfg, canonical, getattr(cfg, name, default))
        except (ImportError, AttributeError):
            return default

    def channel_enabled(self, channel: str) -> bool:
        """返回 Telegram/NapCat/QQ 官方通道是否启用。"""
        normalized = str(channel or "").strip().lower()
        key = self._CHANNEL_KEYS.get(normalized)
        if key is None:
            return False

        # JSONC 的公开格式优先使用 channels；它比旧版遗留的 enable_* 更
        # 接近用户实际编辑的配置，且能明确用 false 覆盖全局默认值。允许
        # 少量历史别名，避免临时配置使用 ``telegram``/``official`` 时失效。
        channels = self._raw.get("channels")
        if isinstance(channels, Mapping):
            for alias in self._CHANNEL_ALIASES.get(normalized, (normalized,)):
                if alias in channels:
                    return _as_bool(channels[alias])

        value = self._raw_value(key)
        if value is not _MISSING:
            return _as_bool(value)
        return _as_bool(self.value(key, False))

    def enabled(self, channel: str) -> bool:
        """``channel_enabled`` 的简短别名，供适配器调用。"""
        return self.channel_enabled(channel)

    def list(self, name: str, default: Sequence[Any] = ()) -> list[Any]:
        """读取路由等列表配置，避免把错误类型传给投递适配器。"""
        value = self.value(name, default)
        if isinstance(value, list):
            return value
        if isinstance(value, (tuple, set)):
            return list(value)
        return list(default)


def _merged(defaults: dict, raw: dict | None) -> dict:
    """浅合并：默认值 + 用户配置（用户配置优先）。"""
    out = dict(defaults)
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[k] = v
    return out


def platform_settings(config: dict, platform: str) -> dict:
    """读取某个社交平台的配置（含默认值填充）。"""
    raw = (config.get("platforms") or {}).get(platform)
    return _merged(_PLATFORM_DEFAULTS.get(platform, {}), raw)


def media_settings(config: dict) -> dict:
    """读取全平台共享的媒体下载配置。"""
    return _merged(_MEDIA_DEFAULTS, config.get("media"))


def social_settings(config: dict) -> dict:
    """读取社交模块总开关配置。"""
    return _merged(_SOCIAL_DEFAULTS, config.get("social"))


def is_platform_enabled(config: dict, platform: str) -> bool:
    """平台是否启用（热更新时每轮都会重新读取）。"""
    return bool(platform_settings(config, platform).get("enabled"))


def any_social_enabled(config: dict) -> bool:
    """是否有任意社交平台被启用。"""
    return any(is_platform_enabled(config, p) for p in SOCIAL_PLATFORMS)


def platform_defaults(platform: str) -> dict:
    """返回某平台的默认配置副本（供 config 模板生成 / 测试使用）。"""
    return dict(_PLATFORM_DEFAULTS.get(platform, {}))
