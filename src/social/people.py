"""
web/people.py — 人物（成员）分类

一个人往往同时拥有多个平台账号（X / Instagram / TikTok / SHOWROOM 房间 /
YouTube 频道 / ME LINK 房间），前台希望按「人」而不是按「平台」来浏览，
所以需要一层「人物 → 各平台账号」的映射。

配置位置：config.json → `people`（数组），例如

    "people": [
      {
        "id": "hitomi",
        "name": "鈴木 瞳美",
        "group": "≠ME",
        "color": "#a15cff",
        "accounts": {
          "x": ["suzuki_hitomi_"],
          "instagram": ["suzuki_hitomi__"],
          "tiktok": ["notequal_me_hitomi"],
          "tiktok_live": ["notequal_me_hitomi"]
        }
      }
    ]

**没配置也能用**：`auto_derive()` 会从各平台的 `display_names` / 账号列表
自动推断人物（同名的会被合并成一个人），因此新增成员时即使忘了配 people，
前台也能按账号自动分出类别。
"""

import hashlib
import re

# 人物色板（未指定 color 时按 id 稳定取色）
_PALETTE = ["#5b86ff", "#e1306c", "#18a058", "#e8a33d", "#a15cff",
            "#ff6b6b", "#12b5b0", "#f0803c", "#8b5cf6", "#0ea5e9"]

# 各平台的账号字段名（不同平台叫法不一样）
ACCOUNT_FIELDS = {
    "x": "accounts",
    "instagram": "accounts",
    "tiktok": "accounts",
    "tiktok_live": "accounts",
    "showroom": "room_slugs",
    "youtube": "channel_handles",
}


def _color_for(pid: str) -> str:
    h = int(hashlib.md5(pid.encode("utf-8"), usedforsecurity=False).hexdigest()[:8], 16)
    return _PALETTE[h % len(_PALETTE)]


def _slug(name: str) -> str:
    """生成 **ASCII 安全且唯一** 的人物 id。

    id 会出现在 URL 查询参数、日志、外部集成里，含 CJK 会导致
    未做 URL 编码的调用直接抛 UnicodeEncodeError，因此这里只保留
    ASCII 字母数字。展示名（name）不受影响，仍然是原始的中文/日文。

    **只要名字里有非 ASCII 字符就必须补哈希**：剥掉 CJK 之后，同一团体的
    不同成员会塌缩成同一个串 —— 「天野 香乃愛（≒JOY）」和
    「大信田 美月（≒JOY）」都只剩下 "joy"。早期实现仅在结果短于 3 字符时
    补哈希，"joy" 正好 3 个字符逃过了这条判断，于是两个人共用一个 id：
    人物筛选会串台、监控对象列表会显示错的归属、按人物的推送规则会作用到
    错误的人身上。纯 ASCII 名字不受影响（不会丢信息，也就不会冲突），
    保持原样以免已保存的 id 变动。
    """
    raw = str(name or "")
    s = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    lossy = bool(re.search(r"[^\x00-\x7f]", raw))    # 含非 ASCII = 有信息丢失
    if len(s) < 3 or lossy:
        h = hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        s = f"{s}_{h}" if s else f"p_{h}"
    return s


def normalize(person: dict) -> dict:
    """补全人物记录的默认字段。"""
    pid = str(person.get("id") or _slug(person.get("name", "")))
    return {
        "id": pid,
        "name": person.get("name") or pid,
        "group": person.get("group", ""),
        "color": person.get("color") or _color_for(pid),
        "avatar": person.get("avatar", ""),
        "accounts": {k: [str(x) for x in (v or [])]
                     for k, v in (person.get("accounts") or {}).items()},
    }


def merge_key(name: str) -> str:
    """归并键：去掉团体后缀与空格，让同一个人在不同平台的写法能对上。

    例：「鈴木 瞳美（≠ME）」与 melink 房间名「鈴木 瞳美」→ 都归一为「鈴木瞳美」
    """
    s = re.sub(r"[（(\[【].*?[）)\]】]", "", str(name or ""))
    return re.sub(r"\s+", "", s).lower() or str(name or "").lower()


def auto_derive(config: dict, archive=None) -> list[dict]:
    """自动推断人物列表。

    两个来源合并：
      1. 各平台配置里的账号 + display_names（X / IG / TikTok / SHOWROOM / YouTube）
      2. 归档库里实际出现过的 (platform, account, author) —— 覆盖 melink 这类
         「房间是动态获取、配置里没有账号列表」的平台

    归并键用 merge_key()，因此「鈴木 瞳美（≠ME）」与「鈴木 瞳美」会合并成一个人。
    """
    by_key: dict[str, dict] = {}

    def _add(display: str, platform: str, acc: str):
        acc = str(acc or "").strip()
        if not acc:
            return
        k = merge_key(display)
        entry = by_key.setdefault(k, {
            "id": _slug(display), "name": display, "accounts": {}})
        # 名字更完整的（带团体后缀）优先作为展示名
        if len(display) > len(entry["name"]):
            entry["name"] = display
        entry["accounts"].setdefault(platform, [])
        if acc not in entry["accounts"][platform]:
            entry["accounts"][platform].append(acc)

    for platform, pcfg in (config.get("platforms") or {}).items():
        if not isinstance(pcfg, dict):
            continue
        field = ACCOUNT_FIELDS.get(platform)
        if not field:
            continue
        names = pcfg.get("display_names") or {}
        for acc in pcfg.get(field) or []:
            acc = str(acc)
            display = names.get(acc) or names.get(acc.lstrip("@")) or acc
            _add(display, platform, acc)

    # ME LINK 已订阅成员（来自 fetcher 每轮缓存的房间列表）——
    # 这样订阅新成员后，人物分类立刻就有条目，无需等第一条消息。
    # 仅当传入的配置确实包含 melink 平台时才读缓存，避免全局缓存
    # 泄漏进不相关的配置（例如测试用的合成配置）。
    if "melink" in (config.get("platforms") or {}):
        try:
            from src.melink.accounts import load_cached_rooms
            for info in load_cached_rooms(subscribed_only=True).values():
                name = info.get("name") or ""
                if name:
                    _add(name, "melink", name)
        except Exception:  # nosec B110
            pass

    # JOY LINK 已订阅成员（同 melink 逻辑）
    if "joylink" in (config.get("platforms") or {}):
        try:
            from src.joylink.accounts import load_cached_rooms as load_joylink_rooms
            for info in load_joylink_rooms(subscribed_only=True).values():
                name = info.get("name") or ""
                if name:
                    _add(name, "joylink", name)
        except Exception:  # nosec B110
            pass

    # 归档库里实际出现过的账号（其余平台的兜底）
    if archive is not None:
        try:
            for row in archive.accounts():
                _add(row.get("author") or row["account"],
                     row["platform"], row["account"])
        except Exception:  # nosec B110
            pass

    return [normalize(p) for p in by_key.values()]


def load_people(config: dict, archive=None) -> list[dict]:
    """读取人物列表：显式配置 **+ 自动发现的新账号**。

    关键设计：显式配置不会「冻结」分类。以后新增关注账号或订阅新成员时——

      * 如果新账号属于已有人物（按 merge_key 判定同一个人）→ 自动并入其名下
      * 否则 → 作为新人物追加，并标记 auto=True 供后台提示确认

    否则一旦在后台保存过人物分类，新成员就会静默消失（前台筛不到、
    机器人规则里也选不到），这是很容易踩的坑。
    """
    raw = config.get("people")
    if not (isinstance(raw, list) and raw):
        return auto_derive(config, archive)

    people = [normalize(p) for p in raw]

    # 已被显式分类覆盖的账号
    covered = set()
    for p in people:
        for platform, accs in (p.get("accounts") or {}).items():
            for a in accs:
                covered.add((platform, str(a)))
                covered.add((platform, str(a).lstrip("@")))

    by_key = {merge_key(p["name"]): p for p in people}

    for cand in auto_derive(config, archive):
        pending: dict[str, list] = {}
        for platform, accs in (cand.get("accounts") or {}).items():
            missing = [a for a in accs
                       if (platform, str(a)) not in covered
                       and (platform, str(a).lstrip("@")) not in covered]
            if missing:
                pending[platform] = missing
        if not pending:
            continue

        target = by_key.get(merge_key(cand["name"]))
        if target is not None:
            # 同一个人新增了平台账号 → 直接并入，并记录是自动加的
            for platform, accs in pending.items():
                target.setdefault("accounts", {}).setdefault(platform, [])
                for a in accs:
                    if a not in target["accounts"][platform]:
                        target["accounts"][platform].append(a)
                        target.setdefault("auto_accounts", []).append(
                            f"{platform}:{a}")
        else:
            # 全新的人（比如订阅了新成员）→ 追加为待确认条目
            entry = {**cand, "accounts": pending, "auto": True}
            people.append(entry)
            by_key[merge_key(entry["name"])] = entry

    return people


def unclassified(config: dict, archive=None) -> list[dict]:
    """返回自动发现、但尚未在显式配置里确认的账号（供后台提示）。"""
    out = []
    for p in load_people(config, archive):
        if p.get("auto"):
            out.append({"name": p["name"], "id": p["id"],
                        "accounts": p["accounts"], "reason": "新人物"})
        elif p.get("auto_accounts"):
            out.append({"name": p["name"], "id": p["id"],
                        "accounts": p["auto_accounts"], "reason": "新增账号"})
    return out


def build_index(people: list[dict]) -> dict:
    """构建 (platform, account) → person_id 的索引，供归档查询使用。"""
    idx = {}
    for p in people:
        for platform, accs in (p.get("accounts") or {}).items():
            for a in accs:
                idx[(platform, str(a))] = p["id"]
                idx[(platform, str(a).lstrip("@"))] = p["id"]
    return idx


def pairs_for(people: list[dict], person_id: str) -> list[tuple]:
    """返回某个人物在各平台的 (platform, account) 组合。"""
    for p in people:
        if p["id"] == person_id:
            out = []
            for platform, accs in (p.get("accounts") or {}).items():
                for a in accs:
                    out.append((platform, str(a)))
                    if str(a).startswith("@"):
                        out.append((platform, str(a).lstrip("@")))
            return out
    return []


def resolve(people: list[dict], platform: str, account: str) -> dict | None:
    """根据平台+账号找出所属人物。"""
    if not account:
        return None
    idx = build_index(people)
    pid = idx.get((platform, str(account))) or idx.get(
        (platform, str(account).lstrip("@")))
    if not pid:
        return None
    for p in people:
        if p["id"] == pid:
            return p
    return None
