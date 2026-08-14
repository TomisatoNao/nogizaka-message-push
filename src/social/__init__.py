"""
social — 社交媒体监控模块（X / Instagram / TikTok / TikTok Live）

纯新增模块，不修改任何既有功能。设计要点：

  * 复用既有配置 —— gemini / qq_bot 配置直接沿用，不重复定义
  * 复用既有数据模型 —— 全部产出 src.models.Post / MediaItem
  * 模块化 —— 每个平台可通过 config.json → platforms.<name>.enabled 独立开关
  * 异步隔离 —— 每个平台一个独立线程，单平台异常不影响其它平台
  * 统一媒体层 —— 基于 yt-dlp 封装，未来接新平台只需实现 fetch()

子模块：
  settings.py       配置读取助手（集中默认值）
  store.py          SQLite 去重 + 直播会话状态（防重复发送 / 防重复录制）
  downloader.py     统一媒体下载器（requests 直下 + yt-dlp），重试 + 线程池
  formatter.py      QQ 推送格式化 + 社交翻译提示词
  live_recorder.py  直播录制引擎（ffmpeg 分段 / 断流重连 / 完整性检查）
  scheduler.py      异步轮询调度器（每平台独立线程与间隔）
  config_watcher.py 配置热更新（修改 config.json 无需重启）

模块内部对 yt-dlp 的 import 全部是惰性的：即使未安装 yt-dlp，
既有的 melink / showroom / youtube 功能也完全不受影响。
"""

from src.social.settings import (
    SOCIAL_PLATFORMS,
    media_settings,
    platform_settings,
    social_settings,
)
from src.social.store import SocialStore

__all__ = [
    "SOCIAL_PLATFORMS",
    "SocialStore",
    "media_settings",
    "platform_settings",
    "social_settings",
]
