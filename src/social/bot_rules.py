"""
social/bot_rules.py — QQ 机器人推送规则

**为什么需要**：原实现里 `_bots` 是一个扁平列表，任何内容都无差别地发给
所有机器人。实际使用中往往需要分流 —— 比如一个号只收某位成员、另一个号
只收直播提醒、还有的号不想收几十 MB 的录像文件。

规则挂在既有的 `qq_bot.bots[]` 每一项下面（新增 `name` / `enabled` /
`filters` 三个键），**不新增任何顶层配置**，也完全向后兼容：
老配置没有 filters 时视为「全部接收」，行为与改动前一致。

过滤维度：
  platforms      —— 只接收这些平台（空 = 全部）
  people         —— 只接收这些人物（空 = 全部，按人物分类的 id）
  melink_rooms   —— 只接收这些 ME LINK 成员房间（空 = 全部）
  joylink_rooms  —— 只接收这些 JOY LINK 成员房间（空 = 全部）
  kinds          —— 只接收这些内容形态（story / live_start / photo …，空 = 全部）
  send_text / send_media / send_recordings / send_alerts —— 分类总开关
  translate      —— 该机器人是否附带中文译文
"""

import logging

log = logging.getLogger("collink")

# 默认规则：什么都收（与改动前的行为完全一致）
DEFAULT_FILTERS = {
    "platforms": [],
    "people": [],
    "melink_rooms": [],
    "joylink_rooms": [],
    "kinds": [],
    "send_text": True,
    "send_media": True,
    "send_recordings": True,
    "send_alerts": True,
    "translate": True,
}


def bot_entry(config: dict, bot: dict) -> dict:
    """在配置里找到该运行时 bot 对应的条目（按 app_id + openid 匹配）。"""
    for b in ((config.get("qq_bot") or {}).get("bots") or []):
        if not isinstance(b, dict):
            continue
        if (str(b.get("app_id", "")) == str(bot.get("app_id", ""))
                and b.get("target_openid", "") == bot.get("oid", "")):
            return b
    return {}


def bot_filters(config: dict, bot: dict) -> dict:
    """取该机器人的推送规则（缺省项用「全部接收」补齐）。"""
    entry = bot_entry(config, bot)
    f = dict(DEFAULT_FILTERS)
    raw = entry.get("filters")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in f:
                f[k] = v
    return f


def bot_enabled(config: dict, bot: dict) -> bool:
    entry = bot_entry(config, bot)
    # 未显式声明 enabled 视为启用（老配置兼容）
    return bool(entry.get("enabled", True)) if entry else True


def bot_name(config: dict, bot: dict) -> str:
    entry = bot_entry(config, bot)
    return entry.get("name") or (bot.get("oid", "")[:8] + "…")


# ── 判定 ──────────────────────────────────────────────────

def _person_of(config: dict, post) -> str:
    """判断一条内容属于哪个人物（找不到返回空串）。"""
    try:
        from src.web import people as people_mod
        from src.web.archive import account_of
        people = people_mod.load_people(config)
        p = people_mod.resolve(people, post.platform, account_of(post))
        return p["id"] if p else ""
    except Exception:
        return ""


def accepts_post(config: dict, bot: dict, post) -> bool:
    """该机器人是否应当收到这条内容。"""
    f = bot_filters(config, bot)

    plats = f.get("platforms") or []
    if plats and post.platform not in plats:
        return False

    kinds = f.get("kinds") or []
    if kinds and (post.extra.get("kind") or "post") not in kinds:
        return False

    rooms = f.get("melink_rooms") or []
    if rooms and post.platform == "melink":
        if str(post.extra.get("room_id", "")) not in {str(r) for r in rooms}:
            return False

    joylink_rooms = f.get("joylink_rooms") or []
    if joylink_rooms and post.platform == "joylink":
        if str(post.extra.get("room_id", "")) not in {str(r) for r in joylink_rooms}:
            return False

    people = f.get("people") or []
    if people:
        pid = _person_of(config, post)
        if pid not in people:
            return False

    # 纯媒体内容（无正文）在关闭媒体推送时没有意义
    if not f.get("send_media", True) and not f.get("send_text", True):
        return False
    return True


def accepts_recording(config: dict, bot: dict, account: str = "") -> bool:
    f = bot_filters(config, bot)
    if not f.get("send_recordings", True):
        return False
    plats = f.get("platforms") or []
    if plats and "tiktok_live" not in plats:
        return False
    return True


def accepts_alert(config: dict, bot: dict) -> bool:
    return bool(bot_filters(config, bot).get("send_alerts", True))


# ── 筛选 ──────────────────────────────────────────────────

def targets_for_post(config: dict, bots: list, post) -> list:
    """返回应当接收该内容的机器人列表。"""
    out = []
    for b in bots:
        if not bot_enabled(config, b):
            continue
        if accepts_post(config, b, post):
            out.append(b)
    return out


def targets_for_recording(config: dict, bots: list, account: str = "") -> list:
    return [b for b in bots
            if bot_enabled(config, b) and accepts_recording(config, b, account)]


def targets_for_alert(config: dict, bots: list) -> list:
    return [b for b in bots
            if bot_enabled(config, b) and accepts_alert(config, b)]


def wants_media(config: dict, bot: dict) -> bool:
    return bool(bot_filters(config, bot).get("send_media", True))


def wants_text(config: dict, bot: dict) -> bool:
    return bool(bot_filters(config, bot).get("send_text", True))


def wants_translation(config: dict, bot: dict) -> bool:
    return bool(bot_filters(config, bot).get("translate", True))
