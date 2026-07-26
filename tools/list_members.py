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
    get_mobile_api_base,
    get_mobile_headers,
    get_web_headers,
    load_all_accounts,
    proactive_refresh_if_expiring,
    validate_account_cred,
)
from src.logger import init_loggers, log_all

BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[90m"
RESET = "\033[0m"


def _is_mobile(acc_cfg: dict) -> bool:
    return acc_cfg.get("auth_method") == "mobile"


def _api_base(account_id: str, acc_cfg: dict) -> str:
    """该账号列成员时应该打的 API 根地址。"""
    if _is_mobile(acc_cfg):
        return get_mobile_api_base(account_id)
    if acc_cfg.get("api_base"):
        return acc_cfg["api_base"]
    return f"https://api.message.{acc_cfg.get('group_type', '')}.com"


def _headers(account_id: str, acc_cfg: dict) -> dict[str, str]:
    """按账号认证方式构造请求头（与 fetcher 保持一致）。"""
    if _is_mobile(acc_cfg):
        return get_mobile_headers(account_id)

    from config.credentials import ACCOUNT_CREDS

    cred = ACCOUNT_CREDS.get(account_id) or {}
    headers = get_web_headers(
        acc_cfg.get("group_type", ""),
        cred.get("token", ""),
        app_tag=acc_cfg.get("app_tag"),
        api_base=acc_cfg.get("api_base"),
        web_origin=acc_cfg.get("web_origin"),
    )
    cookies = cred.get("cookies") or {}
    if cookies:
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return headers


def _normalize(payload) -> list[dict] | None:
    """/v2/groups 返回的可能是裸数组，也可能包一层 groups/items。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("groups", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


async def _fetch_groups(client: httpx.AsyncClient, account_id: str,
                        acc_cfg: dict) -> list[dict] | None:
    url = f"{_api_base(account_id, acc_cfg).rstrip('/')}/v2/groups"
    try:
        resp = await client.get(url, headers=_headers(account_id, acc_cfg))
    except Exception as e:
        print(f"  {YELLOW}✗ 请求失败{RESET}: {type(e).__name__}: {e}")
        return None

    if resp.status_code == 401:
        print(f"  {YELLOW}✗ HTTP 401{RESET} 凭证已失效，请更新该账号的凭证后重试")
        print(f"    {DIM}Web 账号需重新抓 TOKEN + COOKIE，mobile 账号需更新 REFRESH_TOKEN；"
              f"改完 .env 后记得删掉 data/web_credentials/{account_id}.json{RESET}")
        return None
    if resp.status_code == 404:
        print(f"  {YELLOW}✗ HTTP 404{RESET} 该地址没有成员列表接口: {url}")
        print(f"    {DIM}毕业生专用域名（如 yodel）可能不提供此接口，"
              f"可改用同团的其他账号查询{RESET}")
        return None
    if resp.status_code != 200:
        print(f"  {YELLOW}✗ HTTP {resp.status_code}{RESET}: {resp.text[:200]}")
        return None

    try:
        groups = _normalize(resp.json())
    except ValueError:
        print(f"  {YELLOW}✗ 响应不是合法 JSON{RESET}: {resp.text[:200]}")
        return None

    if groups is None:
        print(f"  {YELLOW}✗ 响应结构不认识{RESET}: {str(resp.json())[:200]}")
        return None
    return groups


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
            auth = "mobile" if _is_mobile(acc_cfg) else "web"
            print(f"\n{BOLD}▸ {CYAN}{account_id}{RESET} "
                  f"{DIM}({acc_cfg.get('group_type', '?')} · {auth} · "
                  f"{_api_base(account_id, acc_cfg)}){RESET}")

            ok, reason = validate_account_cred(account_id)
            if not ok:
                print(f"  {YELLOW}✗ 凭证不可用{RESET}: {reason}")
                continue

            # 与主程序同一套续期逻辑；target_group 传 0，告警不会走 QQ 群
            await proactive_refresh_if_expiring(account_id, 0)

            groups = await _fetch_groups(client, account_id, acc_cfg)
            if groups is not None:
                _print_members(account_id, groups)
    finally:
        await client.aclose()

    print(f"\n{BOLD}═══ 完成 ═══{RESET}")
    log_all("成员列表查询完毕", is_debug=True)


if __name__ == "__main__":
    asyncio.run(main())
