"""粉丝信件 (Fan Letters) 归档工具。

拉取已发送给成员的粉丝信件历史、下载高清信纸卡片并持久化到 SQLite 和本地归档目录。

用法:
    python tools/archive_letters.py              # 归档所有监控成员的信件
    python tools/archive_letters.py 冨里奈央      # 仅归档指定成员
    python tools/archive_letters.py 55           # 仅归档指定成员 ID
"""
import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
import config.config as cfg
from config.credentials import (
    load_all_accounts,
    ACCOUNT_CREDS,
    get_source_headers_for_account,
)
from src import archive
from src.logger import log_all
from src.member_directory import api_base


async def fetch_member_letters(member: dict, client: httpx.AsyncClient) -> list[dict]:
    """通过官方 API 获取指定成员的信件列表。"""
    m_name = member.get("m_name") or member.get("name", "")
    m_id = str(member.get("m_id") or member.get("id", ""))
    account_id = member.get("account_id") or member.get("account", "")

    if not m_id:
        norm_name = m_name.replace(" ", "").replace("　", "").replace("_", "").lower()
        for item in getattr(cfg, "MONITOR_LIST", []):
            item_name = (item.get("m_name") or item.get("name") or "").replace(" ", "").replace("　", "").replace("_", "").lower()
            if item_name == norm_name:
                m_id = str(item.get("m_id") or item.get("id") or "")
                if not account_id:
                    account_id = item.get("account_id") or item.get("account") or ""
                break

    if not m_id:
        return []

    # 尝试推导 account_id
    if not account_id:
        group_type = member.get("group_type", "") or archive.infer_member_group(m_name)
        for acc_name, acc_info in cfg.ACCOUNTS.items():
            if acc_info.get("group_type") == group_type:
                account_id = acc_name
                break
        if not account_id:
            account_id = list(cfg.ACCOUNTS.keys())[0] if cfg.ACCOUNTS else ""

    if not account_id or account_id not in ACCOUNT_CREDS:
        log_all(f"⚠️ 未找到可用账号凭证: {account_id}", is_error=True)
        return []

    acc_cfg = cfg.ACCOUNTS.get(account_id, {})
    group_type = acc_cfg.get("group_type", "nogizaka46")
    base = api_base(account_id, acc_cfg).rstrip('/')
    headers = get_source_headers_for_account(account_id, group_type)

    url = f"{base}/v2/groups/{m_id}/letters"
    try:
        resp = await client.get(url, headers=headers, timeout=20.0)
        if resp.status_code == 200:
            data = resp.json()
            letters = data.get("letters", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            return letters
        elif resp.status_code == 404:
            log_all(f"ℹ️ [{m_name}] 该成员无信件历史或接口不支持 (404)", is_debug=True)
            return []
        else:
            log_all(f"⚠️ [{m_name}] 获取信件失败: HTTP {resp.status_code} {resp.text[:100]}", is_error=True)
            return []
    except Exception as e:
        log_all(f"⚠️ [{m_name}] 请求信件异常: {e}", is_error=True)
        return []


async def sync_letters_for_member(member: dict, client: httpx.AsyncClient) -> tuple[int, int]:
    """同步成员信件：返回 (total_count, new_count)。"""
    m_name = member.get("m_name") or member.get("name", "")
    account_id = member.get("account_id") or member.get("account", "")
    acc_cfg = cfg.ACCOUNTS.get(account_id, {})
    group_type = acc_cfg.get("group_type", "nogizaka46")
    headers = get_source_headers_for_account(account_id, group_type)

    letters = await fetch_member_letters(member, client)
    if not letters:
        log_all(f"✉️ [信件同步] {m_name}: 未获取到信件或当前无信件", is_debug=True)
        return (0, 0)

    m_dir = archive.member_dir_name(m_name)
    existing_ids = archive.get_existing_letter_ids(m_dir)
    new_letters = [let_item for let_item in letters if let_item.get("id") not in existing_ids]

    if not new_letters:
        log_all(f"✉️ [信件同步] {m_name}: 信件已是最新（共 {len(letters)} 封，无新增）", is_debug=True)
        archived = await archive.archive_letters_batch(m_name, letters, headers=headers)
        return (len(archived), 0)

    log_all(f"✉️ [信件同步] {m_name}: 发现 {len(new_letters)} 封新信件（总计 {len(letters)} 封），正在下载信纸原图并入库归档...")
    archived = await archive.archive_letters_batch(m_name, letters, headers=headers)
    log_all(f"✅ [信件同步] {m_name}: 成功同步 {len(new_letters)} 封新信件（总计 {len(archived)} 封）！")
    return (len(archived), len(new_letters))


async def main():
    load_all_accounts()
    archive.init_db()

    target_arg = sys.argv[1].strip() if len(sys.argv) > 1 else None
    target_norm = target_arg.replace(" ", "").replace("　", "").replace("_", "").lower() if target_arg else ""
    members_to_sync = []

    for m in cfg.MONITOR_LIST:
        m_name = m.get("m_name") or m.get("name", "")
        m_id = str(m.get("m_id") or m.get("id", ""))
        m_norm = m_name.replace(" ", "").replace("　", "").replace("_", "").lower()
        if not target_arg:
            members_to_sync.append(m)
        elif target_arg == m_id or target_norm in m_norm or m_norm in target_norm:
            members_to_sync.append(m)

    if not members_to_sync:
        if target_arg:
            print(f"❌ 未匹配到成员: '{target_arg}'，请检查输入或确认已在监控列表中。")
        else:
            print("❌ 监控列表为空。")
        return

    print("=" * 60)
    print(f"✉️ 粉丝信件 (Fan Letters) 归档工具 | 目标成员数: {len(members_to_sync)}")
    print("=" * 60)

    total_synced = 0
    total_new = 0
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        archive.initialize(client)
        for member in members_to_sync:
            tot, nw = await sync_letters_for_member(member, client)
            total_synced += tot
            total_new += nw

    print("=" * 60)
    print(f"🎉 归档任务完成！共处理 {total_synced} 封信件（新增 {total_new} 封）。")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
