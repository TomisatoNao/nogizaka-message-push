"""
fetchers/social_base.py — 社交平台 fetcher 公共基类

在既有 BaseFetcher 之上补充社交平台共性能力（纯新增，不改动 BaseFetcher）：

  * 账号列表 / 展示名读取
  * 独立轮询间隔 —— 覆写 get_interval()，使用配置的固定间隔 + 轻微抖动，
    不套用 melink 的 Pareto 活跃度模型（社交平台需求是「X 60秒」这类确定值）
  * SQLite 去重 —— fetch 阶段就跳过已推送项，避免重复下载媒体
  * 首次运行只记录不推送（first_run_skip），避免历史内容刷屏
  * 统一媒体落地目录与 MediaItem 构造
  * mark_synced() —— 成功推送后写入 SQLite

子类只需实现 _fetch_account(account) 返回该账号的新 Post 列表。
"""

import logging
import os
import random
from datetime import datetime, timezone, timedelta

from src.social.fetchers.base import BaseFetcher
from src.social.models import MediaItem, Post
from src.social.downloader import MediaDownloader, classify_media
from src.social.settings import platform_settings
from src.social.store import SocialStore

log = logging.getLogger("collink")

_CST = timezone(timedelta(hours=8))
_NIGHT_START = 0
_NIGHT_END = 6


class SocialFetcher(BaseFetcher):
    """社交平台 fetcher 基类。"""

    #: 子类可声明本平台支持的内容形态（仅用于日志）
    kinds: tuple[str, ...] = ("post",)

    def __init__(self, config: dict,
                 store: SocialStore | None = None,
                 downloader: MediaDownloader | None = None):
        super().__init__(config)
        # store / downloader 由 SyncManager 注入并全平台共用；
        # 缺省时自行创建，保证单独实例化也能工作（便于脚本与测试）
        self._store = store or SocialStore()
        self._dl = downloader or MediaDownloader(config)

    # ── 配置 ─────────────────────────────────────────────

    @property
    def cfg(self) -> dict:
        """当前平台配置（每次读取，配置热更新即时生效）。"""
        return platform_settings(self._config, self.platform_name)

    @property
    def is_enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    @property
    def accounts(self) -> list[str]:
        """监控账号列表（结合全局平台配置与成员绑定配置，统一去掉前导 @）。"""
        accounts_set: list[str] = []
        raw = self.cfg.get("accounts") or []
        for a in raw:
            s = str(a).lstrip("@").strip()
            if s and s not in accounts_set:
                accounts_set.append(s)

        # 自动聚合 monitor 列表中的成员社交账号绑定
        for m in self._config.get("monitor", []):
            soc = m.get("social", {})
            if isinstance(soc, dict):
                p_accs = soc.get(self.platform_name)
                if isinstance(p_accs, str):
                    p_accs = [p_accs]
                if isinstance(p_accs, list):
                    for a in p_accs:
                        s = str(a).lstrip("@").strip()
                        if s and s not in accounts_set:
                            accounts_set.append(s)
        return accounts_set

    def member_name(self, account: str) -> str | None:
        """根据社媒账号反查其归属的 monitor 成员名。若为公共账号则返回 None。"""
        acc_clean = account.lstrip("@").strip()
        for m in self._config.get("monitor", []):
            soc = m.get("social", {})
            if isinstance(soc, dict):
                p_accs = soc.get(self.platform_name)
                if isinstance(p_accs, str):
                    p_accs = [p_accs]
                if isinstance(p_accs, list):
                    cleaned = [str(x).lstrip("@").strip() for x in p_accs]
                    if acc_clean in cleaned:
                        return m.get("name") or acc_clean
        return None

    def display_name(self, account: str) -> str:
        names = self.cfg.get("display_names") or {}
        if account in names:
            return names[account]
        if f"@{account}" in names:
            return names[f"@{account}"]

        # 检查是否绑定了 monitor 成员名称
        m_name = self.member_name(account)
        if m_name and m_name != account:
            return f"{m_name} ({account})"

        return account

    @property
    def media_root(self) -> str:
        return self.cfg.get("download_dir") or os.path.join(
            "messages", f"{self.platform_name}_media")

    @property
    def max_items_per_poll(self) -> int:
        return max(1, int(self.cfg.get("max_items_per_poll", 5)))

    def get_interval(self) -> int:
        """计算下一次轮询的等待时间。

        两种配置形态：

          1. `interval_range_seconds: [1800, 3600]` —— **每轮在区间内重新随机取值**。
             推荐给 Instagram 这类风控敏感的平台：固定间隔（哪怕带小抖动）在
             服务端看来依然是规律性访问，而真人的间隔是散乱的。
          2. `interval_seconds` + 抖动系数 —— 原有形态，其它平台继续用。

        夜间可分别用 `night_interval_range_seconds` / `night_interval_seconds`。
        """
        cfg = self.cfg
        is_night = _NIGHT_START <= datetime.now(_CST).hour < _NIGHT_END

        # 形态 1：区间随机（优先）
        rng = (cfg.get("night_interval_range_seconds") if is_night else None) \
            or cfg.get("interval_range_seconds")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            try:
                lo, hi = int(rng[0]), int(rng[1])
                if lo > hi:
                    lo, hi = hi, lo
                if lo > 0:
                    return max(5, random.randint(lo, hi))
            except (TypeError, ValueError):
                pass

        # 形态 2：基准值 + 抖动
        base = int(cfg.get("interval_seconds", 60) or 60)
        night = int(cfg.get("night_interval_seconds", 0) or 0)
        if night > 0 and is_night:
            base = night
        jmin = float(cfg.get("interval_jitter_min", 0.9) or 0.9)
        jmax = float(cfg.get("interval_jitter_max", 1.1) or 1.1)
        if jmax < jmin:
            jmin, jmax = jmax, jmin
        return max(5, int(base * random.uniform(jmin, jmax)))

    # ── 去重 ─────────────────────────────────────────────

    def is_sent(self, item_id: str) -> bool:
        return self._store.is_sent(self.platform_name, item_id)

    def mark_seen(self, item_id: str, account: str = "", kind: str = "") -> None:
        self._store.mark_seen(self.platform_name, item_id, account, kind)

    def mark_synced(self, synced_posts: list[Post]) -> None:
        """成功推送后写入 SQLite —— 失败项不打标，下轮自动重试。"""
        for p in synced_posts:
            if p.platform != self.platform_name:
                continue
            self._store.mark_sent(
                self.platform_name, p.post_id,
                account=p.extra.get("account", ""),
                kind=p.extra.get("kind", ""),
            )

    def _bootstrap_guard(self, account: str, ids: list[str], kind: str = "") -> bool:
        """首次运行守卫。

        返回 True 表示「本次只记录不推送」—— 把当前全部已存在内容标记为已发送，
        避免首次启动时把账号历史内容全量推送到 QQ（与 youtube_fetcher 的
        「首轮跳过全部已有视频」行为一致）。
        """
        if not self.cfg.get("first_run_skip", True):
            return False
        if self._store.is_bootstrapped(self.platform_name, account, kind):
            return False
        for i in ids:
            self._store.mark_sent(self.platform_name, i, account, kind)
        self._store.mark_bootstrapped(self.platform_name, account, kind)
        log.info("[%s] 🆕 首次监控 %s%s，已记录 %s 条现有内容（不推送）",
                 self.platform_name, account,
                 f"/{kind}" if kind else "", len(ids))
        return True

    def _filter_before_bootstrap(self, items: list[dict], account: str,
                                  kind: str = "", ts_key: str = "timestamp") -> list[dict]:
        """过滤掉时间早于首次监控时刻的内容。

        TikTok embed 接口返回的内容列表不稳定（每次 ~10 条，但集合可能不同），
        导致 bootstrap 时看到的只是子集，后续轮次会冒出未标记过的老视频。
        本方法利用 bootstrap 时间戳做一刀切：比首次监控还早的内容直接丢弃。
        """
        boot_time = self._store.get_bootstrap_time(
            self.platform_name, account, kind)
        if not boot_time:
            return items
        kept = [i for i in items if i.get(ts_key, 0) >= boot_time]
        dropped = len(items) - len(kept)
        if dropped:
            log.debug("[%s] @%s 丢弃 %s 条 bootstrap 前的内容（时间戳 < %s）",
                      self.platform_name, account, dropped,
                      datetime.fromtimestamp(boot_time, tz=_CST).strftime("%Y-%m-%d %H:%M"))
        return kept

    # ── 媒体 ─────────────────────────────────────────────

    def item_dir(self, account: str, item_id: str, kind: str = "") -> str:
        """单条内容的媒体目录：{download_dir}/{account}/{kind_}{item_id}/"""
        from src.social.live_recorder import safe_name
        sub = f"{kind}_{item_id}" if kind and kind not in ("post", "feed") else str(item_id)
        d = os.path.join(self.media_root, safe_name(account), safe_name(sub))
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def build_media_items(files: list[str], urls: list[str] | None = None,
                          alt_texts: list[str] | None = None) -> list[MediaItem]:
        """本地文件列表 → MediaItem 列表（类型按扩展名判定）。"""
        items: list[MediaItem] = []
        for i, fp in enumerate(files):
            if not fp or not os.path.exists(fp):
                continue
            items.append(MediaItem(
                type=classify_media(fp),
                url=(urls[i] if urls and i < len(urls) else ""),
                local_path=os.path.abspath(fp),
                alt_text=(alt_texts[i] if alt_texts and i < len(alt_texts) else ""),
            ))
        return items

    # ── 主入口 ───────────────────────────────────────────

    def fetch(self) -> list[Post]:
        """遍历所有账号；单账号异常被隔离，不影响其它账号。"""
        posts: list[Post] = []
        accounts = self.accounts
        if not accounts:
            log.debug("[%s] 未配置监控账号，跳过", self.platform_name)
            return posts

        for account in accounts:
            log.debug("[%s] 🔎 开始检查账号 @%s", self.platform_name, account)
            try:
                got = self._fetch_account(account)
            except Exception as e:
                log.warning("[%s] @%s 检查失败: %s", self.platform_name, account,
                            str(e).replace("\n", " ")[:200])
                continue
            if got:
                m_name = self.member_name(account)
                for p in got:
                    if m_name:
                        p.extra["member_name"] = m_name
                    p.extra["account"] = account
                log.info("[%s] 🆕 @%s 发现 %s 条新内容",
                         self.platform_name, account, len(got))
                posts.extend(got)
            else:
                log.debug("[%s] ✅ @%s 无新内容", self.platform_name, account)
        return posts

    def _fetch_account(self, account: str) -> list[Post]:
        raise NotImplementedError

    # ── 向后兼容别名 ─────────────────────────────────────
    # scripts/integration_test.py 是按早期 X/IG/TikTok fetcher 的内部命名写的
    # （user_ids / usernames / _fetch_user）。保留这些别名让它继续可用，
    # 新代码请直接用 accounts / _fetch_account。

    @property
    def usernames(self) -> list[str]:
        """[兼容] 等价于 accounts。"""
        return self.accounts

    @property
    def user_ids(self) -> list[str]:
        """[兼容] 等价于 accounts。"""
        return self.accounts

    def _fetch_user(self, account: str) -> list[Post]:
        """[兼容] 等价于 _fetch_account()。"""
        return self._fetch_account(account)
