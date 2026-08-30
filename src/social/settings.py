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
