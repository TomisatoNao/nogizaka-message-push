"""历史消息回填归档：从指定日期开始分页拉取全部历史消息并归档。

主程序的实时归档只覆盖启用之后的新消息；这个工具补齐历史，
并会重试之前媒体下载失败（_download_failed）的消息。

用法:
    python tools/backfill_archive.py                          # 回填所有监控成员（断点续传）
    python tools/backfill_archive.py "冨里 奈央"               # 只回填指定成员（名字或 id 均可，可多个）
    python tools/backfill_archive.py "佐藤 優羽" --reset       # 重置断点，从最早期全量回填（换号后推荐）
    python tools/backfill_archive.py --from 2023-01-01        # 指定起始日期（默认 2013-01-01）
    python tools/backfill_archive.py --force                  # 跳过"主程序正在运行"检查（不建议）

说明:
    - ⚠️ 请先停止主程序：两个进程同时刷新同一账号的 Token 会让先刷的那个凭证作废。
      工具会自动检测主程序是否在跑（探测网页管理端端口），在跑就拒绝启动。
    - 复用项目的账号池与凭证（web / mobile 都支持），与主程序同一套续期逻辑。
    - 断点续传：进度存 data/archive_progress.json，中断后重跑从上次位置继续。
    - 已归档的消息自动跳过（除非其媒体下载失败）。
    - 成员串行处理，避免同账号并发请求；媒体下载内部限流 3 并发。
    - 可与主程序同时运行（写入按月加锁 + 原子替换），但为减小 API 压力，
      建议在主程序休眠时段或停机时执行。
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx

import config.config as cfg
from config.credentials import (
    load_all_accounts,
    proactive_refresh_if_expiring,
    validate_account_cred,
)
from src import archive, tagger
from src.logger import init_loggers, log_all
from src.member_directory import api_base, build_headers

PROGRESS_PATH = Path(cfg.TIME_RECORD_DIR).parent / "archive_progress.json"
PAGE_COUNT = 200
EMPTY_PAGES_TO_STOP = 3
DEFAULT_START = "2013-01-01T00:00:00Z"


class AdaptivePacer:
    """自适应分页限速：顺畅时逐步提速，出错/被限流时指数退避。"""

    def __init__(self, base: float = 1.5, floor: float = 0.8, ceil: float = 90.0):
        self.delay = base
        self.floor = floor
        self.ceil = ceil

    def on_success(self) -> None:
        self.delay = max(self.floor, self.delay * 0.85)

    def on_error(self, rate_limited: bool = False) -> None:
        self.delay = min(self.ceil, max(self.delay, 1.5) * (3.0 if rate_limited else 2.0))

    async def wait(self) -> None:
        await asyncio.sleep(self.delay)


def main_program_running() -> bool:
    """探测主程序是否在跑（网页管理端端口被监听即视为在跑）。

    两个进程同时用同一账号刷新 Token 时，后刷的会让先刷的 Token 失效，
    导致主程序推送中断——这类事故靠人自觉很难避免，这里做成硬检查。
    """
    import socket
    if not getattr(cfg, "WEB_ADMIN_ENABLED", False):
        return False
    host = getattr(cfg, "WEB_ADMIN_HOST", "127.0.0.1")
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with socket.create_connection((probe_host, int(cfg.WEB_ADMIN_PORT)), timeout=1.5):
            return True
    except OSError:
        return False


def _load_progress() -> dict:
    if PROGRESS_PATH.exists():
        try:
            return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def _save_progress(progress: dict) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, PROGRESS_PATH)


async def backfill_member(client: httpx.AsyncClient, member: dict,
                          start_from: str, progress: dict,
                          reset_cursor: bool = False,
                          user_specified_start: bool = False) -> None:
    m_name = member["m_name"]
    account_id = member["account_id"]
    acc_cfg = cfg.ACCOUNTS.get(account_id, {})
    # 游标键名绑定账号 ID 与成员 ID，当用户为成员切换不同账号时自动互不干扰
    key = f"{account_id}_{member['group_type']}_{member['m_id']}"
    legacy_key = f"{member['group_type']}_{member['m_id']}"

    ok, reason = validate_account_cred(account_id)
    if not ok:
        print(f"  ✗ 跳过 {m_name}：账号 {account_id} 凭证不可用（{reason}）")
        return

    archived_ids, failed_ids = archive.load_archived_ids(m_name)
    saved_cursor = None if reset_cursor else (progress.get(key) or progress.get(legacy_key))
    if reset_cursor or user_specified_start:
        cursor = start_from
    else:
        cursor = saved_cursor or start_from
    print(f"▸ {m_name}（账号 {account_id}）从 {cursor} 开始，已归档 {len(archived_ids)} 条"
          + (f"，待重试媒体 {len(failed_ids)} 条" if failed_ids else ""))

    base = acc_cfg.get("api_base") or api_base(account_id, acc_cfg)
    empty_pages = 0
    total_new = 0
    pacer = AdaptivePacer()

    # ── 1. 优先拉取新订阅成员过去 24 小时历史消息 (/past_messages) ──
    past_url = f"{base.rstrip('/')}/v2/groups/{member['m_id']}/past_messages"
    try:
        past_resp = await client.get(past_url, headers=build_headers(account_id, acc_cfg))
        if past_resp.status_code == 200:
            past_msgs = past_resp.json().get("messages", [])
            if past_msgs:
                past_new = await archive.archive_messages_batch(
                    member, past_msgs, archived_ids=archived_ids, failed_ids=failed_ids
                )
                if past_new:
                    print(f"  📥 [past_messages] 成功归档 {past_new} 条过去 24h 消息")
                    total_new += past_new
    except Exception as e:
        print(f"  ⚠️ 抓取 past_messages 失败: {e}")

    # ── 2. 分页回填时间线 (timeline) ──
    while True:
        await proactive_refresh_if_expiring(account_id, 0)   # target_group=0：告警只打日志
        url = (f"{base.rstrip('/')}/v2/groups/{member['m_id']}/timeline"
               f"?updated_from={quote(cursor)}&count={PAGE_COUNT}&order=asc")
        try:
            resp = await client.get(url, headers=build_headers(account_id, acc_cfg))
        except Exception as e:
            pacer.on_error()
            print(f"  ✗ 请求失败: {type(e).__name__}: {e}，{pacer.delay:.0f}s 后重试")
            await pacer.wait()
            continue

        if resp.status_code == 401:
            print("  🔄 凭证过期，续期后重试…")
            await proactive_refresh_if_expiring(account_id, 0)
            await asyncio.sleep(3)
            continue
        if resp.status_code == 429:
            pacer.on_error(rate_limited=True)
            print(f"  ⏳ 被限流 (429)，退避 {pacer.delay:.0f}s…")
            await pacer.wait()
            continue
        if resp.status_code != 200:
            pacer.on_error()
            print(f"  ✗ HTTP {resp.status_code}: {resp.text[:150]}，{pacer.delay:.0f}s 后重试")
            await pacer.wait()
            continue
        pacer.on_success()

        msgs = resp.json().get("messages", [])
        if not msgs:
            empty_pages += 1
            if empty_pages >= EMPTY_PAGES_TO_STOP:
                break
            await asyncio.sleep(2)
            continue
        empty_pages = 0

        # 高性能批量归档：多媒体并发下载 + 月度 JSON 单次合并 + SQLite 批量提交
        page_new = await archive.archive_messages_batch(
            member, msgs, archived_ids=archived_ids, failed_ids=failed_ids
        )
        total_new += page_new
        new_cursor = msgs[-1].get("updated_at", cursor)
        if new_cursor == cursor:
            break   # 游标不再前进，防止死循环
        cursor = new_cursor
        progress[key] = cursor
        if legacy_key in progress and legacy_key != key:
            progress.pop(legacy_key, None)
        _save_progress(progress)
        print(f"  … 游标 {cursor}（本页新归档 {page_new} 条，累计 {total_new}，间隔 {pacer.delay:.1f}s）")
        await pacer.wait()   # 自适应分页间隔

    progress[key] = cursor
    if legacy_key in progress and legacy_key != key:
        progress.pop(legacy_key, None)
    _save_progress(progress)
    print(f"  ✅ {m_name} 完成，本次新归档 {total_new} 条")


async def main() -> None:
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return

    force = "--force" in args
    args = [a for a in args if a != "--force"]

    reset = "--reset" in args
    args = [a for a in args if a != "--reset"]

    user_specified_start = False
    start_from = DEFAULT_START
    if "--from" in args:
        i = args.index("--from")
        start_from = f"{args[i + 1]}T00:00:00Z"
        user_specified_start = True
        args = args[:i] + args[i + 2:]

    if main_program_running() and not force:
        print("🚫 检测到主程序正在运行（网页管理端端口已被监听）。")
        print("   两个进程同时刷新同一账号的 Token 会让其中一个凭证作废，导致推送中断。")
        print("   请先停止主程序再回填；确认无冲突可加 --force 跳过此检查。")
        return

    init_loggers()
    load_all_accounts()

    targets = list(cfg.MONITOR_LIST)
    if args:
        wanted_norm = {a.replace(" ", "").replace("　", "").replace("_", "") for a in args}
        wanted_raw = set(args)
        targets = [
            m for m in targets
            if m["m_name"] in wanted_raw
            or m["m_id"] in wanted_raw
            or m["m_name"].replace(" ", "").replace("　", "").replace("_", "") in wanted_norm
        ]
        if not targets:
            print(f"未在 monitor 里找到：{'、'.join(args)}")
            print(f"可选成员：{'、'.join(m['m_name'] for m in cfg.MONITOR_LIST)}")
            return

    if not cfg.ARCHIVE_ENABLED:
        print("⚠️ config.json 的 archive.enabled 是关闭的——回填仍会写入归档目录，"
              "但主程序不会实时归档新消息。")

    print(f"═══ 历史回填（{len(targets)} 个成员，起始 {start_from}"
          + ("，--reset 强制从头扫描" if reset else "") + "）═══")
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=30)
    client = httpx.AsyncClient(timeout=30, limits=limits)
    archive.initialize(client)
    tagger.initialize(client)
    progress = _load_progress()
    try:
        for member in targets:      # 串行：避免同账号并发
            await backfill_member(
                client,
                member,
                start_from,
                progress,
                reset_cursor=reset,
                user_specified_start=user_specified_start,
            )
        await tagger.wait_pending(timeout=10)
    finally:
        await client.aclose()
    log_all("历史回填完成", is_debug=True)
    print("═══ 全部完成 ═══")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏸ 已中断，进度已保存，重跑将从断点继续")
