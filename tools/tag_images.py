# ============================================================
# tools/tag_images.py — 批量图片打标签回填
# ============================================================
# 扫描归档目录，找出 type=picture/image 且 _tags 为空的图片，
# 逐张调 Gemini 打标签并写回归档。
#
# 幂等：已拥有 _tags 的图片跳过，随时可重跑。
#
# 用法：
#   python tools/tag_images.py --dry-run                      # 预览所有需处理的图片
#   python tools/tag_images.py --member "冨里 奈央" --dry-run  # 预览指定成员
#   python tools/tag_images.py --member "冨里 奈央" --year 2026 --month 07  # 指定月份
#   python tools/tag_images.py --member "冨里 奈央"            # 回填指定成员全部
#   python tools/tag_images.py                                # 回填所有成员全部
# ============================================================
import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 添加项目根到 sys.path
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.archive import archive_root, list_members, load_month, member_dir_name, _merge_write  # noqa: E402
from src.tagger import tag_image, initialize as init_tagger  # noqa: E402


async def main():
    parser = argparse.ArgumentParser(description="批量图片打标签回填")
    parser.add_argument("--member", help="成员名（默认全部）")
    parser.add_argument("--year", type=int, help="年份（默认全部）")
    parser.add_argument("--month", type=int, help="月份（默认全部）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际调用 API")
    args = parser.parse_args()

    # 初始化 tagger
    init_tagger()

    members = [m.strip() for m in args.member.split(",")] if args.member else list_members()
    if not members:
        print("❌ 没有找到任何归档成员")
        return

    # 收集待处理图片
    pending = []  # [(member, year, month, msg)]
    for m_name in members:
        dir_name = member_dir_name(m_name)
        root = archive_root() / dir_name
        if not root.is_dir():
            print(f"⚠ 成员归档不存在: {m_name}")
            continue

        year_dirs = sorted(
            (d for d in root.iterdir() if d.is_dir() and d.name.isdigit()),
            reverse=True,
        )
        for yd in year_dirs:
            year = int(yd.name)
            if args.year and year != args.year:
                continue

            month_dirs = sorted(
                (d for d in yd.iterdir() if d.is_dir() and d.name.isdigit()),
                reverse=True,
            )
            for md in month_dirs:
                month = int(md.name)
                if args.month and month != args.month:
                    continue

                msgs = load_month(m_name, year, month)
                for msg in msgs:
                    if msg.get("type") not in ("picture", "image"):
                        continue
                    if msg.get("_tags"):
                        continue  # 已有标签，跳过
                    if not msg.get("_local_file"):
                        continue  # 没有本地文件，跳过
                    pending.append((m_name, year, month, msg))

    if not pending:
        print("✅ 所有图片都已打标签，无需处理")
        return

    print(f"📊 待处理图片: {len(pending)} 张")
    if args.dry_run:
        print()
        for m_name, year, month, msg in pending:
            print(f"   [{m_name}] {year}/{month:02d}  {msg['_local_file']}  "
                  f"{msg.get('text', '')[:40]}")
        print()
        print(f"共 {len(pending)} 张（--dry-run 模式，未实际调用 API）")
        return

    # 逐个打标签
    ok = 0
    fail = 0
    skip = 0
    t0 = time.time()

    for i, (m_name, year, month, msg) in enumerate(pending, 1):
        local_file = msg.get("_local_file", "")
        print(f"[{i}/{len(pending)}] [{m_name}] {local_file} ... ", end="", flush=True)

        tags = await tag_image(m_name, local_file)
        if not tags:
            print("❌ 打标签失败")
            fail += 1
            continue

        print(f"✅ {tags}")

        # 写回归档
        utc_str = msg.get("updated_at") or msg.get("published_at", "")
        try:
            dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            print(f"   ⚠ 时间戳解析失败: {utc_str}")
            skip += 1
            continue

        delta = {"id": msg["id"], "updated_at": utc_str, "_tags": tags}
        await _merge_write(m_name, dt, delta)
        ok += 1

        # 进度：每 10 张输出一次
        if ok % 10 == 0:
            elapsed = time.time() - t0
            print(f"\n  📊 进度: {ok}/{len(pending)}，耗时 {elapsed:.0f}s")

    elapsed = time.time() - t0
    print(f"\n{'='*40}")
    print(f"✅ 完成: 成功 {ok} 张")
    if fail:
        print(f"❌ 失败: {fail} 张")
    if skip:
        print(f"⏭️  跳过: {skip} 张（时间戳问题）")
    print(f"⏱️  总耗时: {elapsed:.0f}s（平均 {elapsed/max(ok,1):.1f}s/张）")
    print(f"{'='*40}")


if __name__ == "__main__":
    asyncio.run(main())
