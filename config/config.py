# ============================================================
# config.py — 全部常量配置，不依赖项目内其他模块
# ============================================================
import os as _os

# 项目根目录（config 目录的上一级），无论从哪里运行都指向这里
_BASE_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
DATA_DIR = _os.path.join(_BASE_DIR, "data")
LOG_DIR = _os.path.join(_BASE_DIR, "logs")

# ── 改进 6：从 .env 文件加载环境变量 ──────────────────────────
# 如果安装了 python-dotenv（pip install python-dotenv），
# 程序会自动从同目录的 .env 文件读取配置；
# 未安装时退化为只读系统环境变量，行为不变。
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_os.path.join(_BASE_DIR, ".env"))
except ImportError:
    pass   # python-dotenv 未安装，继续从系统环境变量读取

def _env(key: str, default: str = "") -> str:
    """读取环境变量，不存在时返回 default。"""
    return _os.getenv(key, default)

def _env_bool(key: str, default: bool = False) -> bool:
    """读取布尔环境变量，支持 1/true/yes/on 和 0/false/no/off。"""
    raw = _env(key, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}

# ---- QQ 推送通道 ----
ENABLE_NAPCAT_QQ       = _env_bool("ENABLE_NAPCAT_QQ", True)
ENABLE_QQ_OFFICIAL_BOT = _env_bool("ENABLE_QQ_OFFICIAL_BOT", False)

# ---- NapCat / OneBot HTTP ----
QQ_BOT_API    = _env("QQ_BOT_API", "http://127.0.0.1:3000/send_group_msg")
QQ_USER_AGENT = "Mozilla/5.0 (Windows NT; Windows NT 10.0; en-US) WindowsPowerShell/5.1"

# ---- QQ 开放平台官方 Bot（支持多个）----
QQ_OFFICIAL_TOKEN_URL       = "https://bots.qq.com/app/getAppAccessToken"
QQ_OFFICIAL_API_BASE        = "https://api.sgroup.qq.com"
QQ_OFFICIAL_MIN_INTERVAL    = 1.2
QQ_OFFICIAL_TIMEOUT         = 15
QQ_OFFICIAL_MEDIA_MAX_BYTES = 25 * 1024 * 1024

# 多 Bot 配置列表
QQ_OFFICIAL_BOTS: list[dict] = [
    {
        "name":          "bot_1",
        "app_id":        _env("QQ_OFFICIAL_BOT1_APP_ID"),
        "client_secret": _env("QQ_OFFICIAL_BOT1_CLIENT_SECRET"),
        "target_openid": _env("QQ_OFFICIAL_BOT1_TARGET_OPENID"),
    },
    {
        "name":          "bot_2",
        "app_id":        _env("QQ_OFFICIAL_BOT2_APP_ID"),
        "client_secret": _env("QQ_OFFICIAL_BOT2_CLIENT_SECRET"),
        "target_openid": _env("QQ_OFFICIAL_BOT2_TARGET_OPENID"),
    },
]

# 向后兼容：如果新配置为空但旧配置存在，自动迁移
_old_app_id = _env("QQ_OFFICIAL_APP_ID")
if _old_app_id and not any(b["app_id"] for b in QQ_OFFICIAL_BOTS):
    QQ_OFFICIAL_BOTS = [{
        "name":          "default",
        "app_id":        _old_app_id,
        "client_secret": _env("QQ_OFFICIAL_CLIENT_SECRET"),
        "target_openid": _env("QQ_OFFICIAL_TARGET_OPENID"),
    }]

# ---- 账号池（凭证全部从环境变量读取） ----
ACCOUNTS: dict[str, dict] = {
    "nogizaka_main": {
        "group_type":  "nogizaka46",
        "init_token":  _env("ACCOUNT_NOGIZAKA_MAIN_TOKEN"),
        "init_cookie": _env("ACCOUNT_NOGIZAKA_MAIN_COOKIE"),
    },
    "hinata_shared": {
        "group_type":  "hinatazaka46",
        "init_token":  _env("ACCOUNT_HINATA_SHARED_TOKEN"),
        "init_cookie": _env("ACCOUNT_HINATA_SHARED_COOKIE"),
    },
    "hinata_main": {
        "group_type":  "hinatazaka46",
        "init_token":  _env("ACCOUNT_HINATA_MAIN_TOKEN"),
        "init_cookie": _env("ACCOUNT_HINATA_MAIN_COOKIE"),
    },
}

# ---- 监控列表 ----
MONITOR_LIST: list[dict] = [
    {
        "account_id":       "nogizaka_main",
        "group_type":       "nogizaka46",
        "m_id":             "55",
        "m_name":           "冨里 奈央",
        "target_group":     533072575,
        "post_to_bilibili": False,
        # 未配置 MEMBER_55_BILIBILI_COOKIE 时使用全局默认值
    },
    {
        "account_id":       "hinata_shared",
        "group_type":       "hinatazaka46",
        "m_id":             "34",
        "m_name":           "金村 美玖",
        "target_group":     752269366,
        "post_to_bilibili": False,
    },
    {
        "account_id":       "hinata_shared",
        "group_type":       "hinatazaka46",
        "m_id":             "36",
        "m_name":           "小坂 菜绪",
        "target_group":     752269366,
        "post_to_bilibili": False,
    },
    {
        "account_id":       "hinata_main",
        "group_type":       "hinatazaka46",
        "m_id":             "84",
        "m_name":           "大野 愛実",
        "target_group":     752269366,
        "post_to_bilibili": False,
    },
    {
        "account_id":       "hinata_shared",
        "group_type":       "hinatazaka46",
        "m_id":             "85",
        "m_name":           "片山 紗希",
        "target_group":     752269366,
        "post_to_bilibili": False,
        "bilibili_cookie":  _env("MEMBER_85_BILIBILI_COOKIE"),   # 空字符串时自动回退全局
    },
    {
        "account_id":       "hinata_shared",
        "group_type":       "hinatazaka46",
        "m_id":             "88",
        "m_name":           "佐藤 優羽",
        "target_group":     752269366,
        "post_to_bilibili": False,
    },
]

# ---- 功能开关 ----
ENABLE_TRANSLATION = True

# ---- 消息过滤 ----
SKIP_PUBLISH_TYPES: set[str] = {"birthday"}
MEDIA_TYPE_MAP: dict[str, str] = {
    "video":   "video",
    "voice":   "record",
    "image":   "image",
    "picture": "image",
}

# ---- 轮询间隔 (秒) ----
DAY_START_HOUR   = 7
NIGHT_START_HOUR = 0
DAY_INTERVAL     = (120, 180)
NIGHT_INTERVAL   = (1500, 1800)
BACKTRACK_HOURS  = 24

# ---- 报警冷却 ----
ALERT_COOLDOWN_SECONDS = 300

# ---- 并发控制 ----
HTTP_SEMAPHORE_LIMIT = 3
QQ_SEND_INTERVAL     = 1.5   # 同一成员多条消息之间的发送间隔（秒）

# ---- Token 主动刷新阈值 ----
# Token 有效期约 3600s，剩余时间低于此值时提前刷新（秒）
TOKEN_REFRESH_BEFORE_SECONDS = 300   # 提前 5 分钟

# ---- 文件路径（均锚定到项目根目录，与运行时工作目录无关） ----
CRED_DIR          = _os.path.join(DATA_DIR, "web_credentials")
TIME_RECORD_DIR   = _os.path.join(DATA_DIR, "time_records")
SENT_IDS_DIR      = _os.path.join(DATA_DIR, "sent_ids")
ERROR_LOG_FILE    = _os.path.join(LOG_DIR, "error_debug.log")
RESPONSE_LOG_FILE = _os.path.join(LOG_DIR, "response_debug.log")
SENT_IDS_MAX      = 500

# ---- 调试 ----
DEBUG_LOG_RESPONSE = True
DEBUG_LOG_QQ_PAYLOAD = _env_bool("DEBUG_LOG_QQ_PAYLOAD", False)

# ---- Gemini 翻译 ----
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODELS: list[dict] = [
    {
        "name": "gemini-2.5-flash",
        "url":  "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        "rpm":  10,
    },
    {
        "name": "gemini-2.5-flash-lite",
        "url":  "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent",
        "rpm":  15,
    },
    {
        "name": "gemini-2.5-pro",
        "url":  "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent",
        "rpm":  5,
    },
]
GEMINI_MIN_INTERVAL  = 7.0   # 两次翻译请求最小间隔（秒）
TRANSLATE_MAX_LENGTH = 2500
TRANSLATE_TIMEOUT    = 30

# ---- B站动态发布（全局默认 Cookie） ----
BILIBILI_FULL_COOKIE  = _env("BILIBILI_FULL_COOKIE")
BILIBILI_BILI_JCT     = _env("BILIBILI_BILI_JCT")
BILIBILI_POST_API     = "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/create"
BILIBILI_MIN_INTERVAL = 3.0   # 两次 B站发帖最小间隔（秒）
