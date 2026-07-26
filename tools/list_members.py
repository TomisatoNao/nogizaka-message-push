"""列出账号可监控的成员及其 m_id（填 config.json 的 monitor 用）。

直接复用本项目的账号池和凭证：`config.json` 里配了哪些账号、`.env`/
`data/web_credentials/` 里有什么凭证，这里就用什么，不需要额外配置。

用法:
    python tools/list_members.py                  # 列出所有账号能看到的成员
    python tools/list_members.py nogizaka_main    # 只列指定账号（可传多个）

说明:
    - Token 剩余时间不足时会自动续期（与主程序同一套逻辑），续期结果会写回
      data/web_credentials/，所以跑完这个工具不会让主程序的凭证失效。
    - 续期失败时主程序会发系统告警，而这里故意不初始化任何推送通道，
      因此失败只会打印在本地日志里，不会往 QQ 群 / TG 频道发消息。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import config.config as cfg
from config.credentials import (
    load_all_accounts,
    proactive_refresh_if_expiring,
    validate_account_cred,
)
from src.logger import init_loggers, log_all
from src.member_directory import api_base, fetch_member_directory, is_mobile

BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[90m"
RESET = "\033[0m"


def _sort_key(member: dict):
    """按 id 数值排序，非数字 id 排在最后。"""
    raw = str(member.get("id", ""))
    return (0, int(raw)) if raw.isdigit() else (1, 0)


def _print_members(account_id: str, groups: list[dict]) -> None:
    if not groups:
        print(f"  {DIM}(该账号看不到任何成员){RESET}")
        return

    by_tag: dict[str, list[dict]] = {}
    for member in groups:
        tags = member.get("tags") or ["其他"]
        by_tag.setdefault(" · ".join(str(t) for t in tags), []).append(member)

    open_members: list[dict] = []
    for tag, members in by_tag.items():
        print(f"\n  ── {tag} ──")
        for member in sorted(members, key=_sort_key):
            state = member.get("state", "?")
            is_open = state == "open"
            if is_open:
                open_members.append(member)
            icon = f"{GREEN}🟢{RESET}" if is_open else f"{DIM}⚫{RESET}"
            sub = member.get("subscription") or {}
            sub_info = f" {DIM}[{sub.get('type', '?')}]{RESET}" if sub else ""
            name = member.get("name") or "(无名)"
            print(f"    [{str(member.get('id', '?')):>3}] {icon} {name}{sub_info}")

    total = len(groups)
    print(f"\n  共 {total} 项，其中 {len(open_members)} 个在籍（🟢 open）")

    if open_members:
        sample = open_members[0]
        print(f"  {DIM}config.json 的 monitor 里这样写：{RESET}")
        print(f'    {{ "id": "{sample.get("id")}", "name": "{sample.get("name")}", '
              f'"account": "{account_id}", "groups": [你的QQ群号] }}')


async def main() -> None:
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    if any(a in {"-h", "--help"} for a in sys.argv[1:]):
        print(__doc__)
        return

    init_loggers()
    load_all_accounts()

    accounts = dict(cfg.ACCOUNTS)
    if argv:
        unknown = [a for a in argv if a not in accounts]
        if unknown:
            print(f"{YELLOW}未在 config.json 的 accounts 里找到：{', '.join(unknown)}{RESET}")
            print(f"可用账号：{', '.join(accounts)}")
            return
        accounts = {k: accounts[k] for k in argv}

    if not accounts:
        print(f"{YELLOW}config.json 的 accounts 是空的，先配一个账号再来{RESET}")
        return

    print(f"\n{BOLD}═══ 成员列表（{len(accounts)} 个账号）═══{RESET}")

    client = httpx.AsyncClient(timeout=20)
    try:
        for account_id, acc_cfg in accounts.items():
            auth = "mobile" if is_mobile(acc_cfg) else "web"
            print(f"\n{BOLD}▸ {CYAN}{account_id}{RESET} "
                  f"{DIM}({acc_cfg.get('group_type', '?')} · {auth} · "
                  f"{api_base(account_id, acc_cfg)}){RESET}")

            ok, reason = validate_account_cred(account_id)
            if not ok:
                print(f"  {YELLOW}✗ 凭证不可用{RESET}: {reason}")
                continue

            # 与主程序同一套续期逻辑；target_group 传 0，告警不会走 QQ 群
            await proactive_refresh_if_expiring(account_id, 0)

            groups, err = await fetch_member_directory(client, account_id)
            if err:
                print(f"  {YELLOW}✗ {err}{RESET}")
            else:
                _print_members(account_id, groups)
    finally:
        await client.aclose()

    print(f"\n{BOLD}═══ 完成 ═══{RESET}")
    log_all("成员列表查询完毕", is_debug=True)


if __name__ == "__main__":
    asyncio.run(main())
