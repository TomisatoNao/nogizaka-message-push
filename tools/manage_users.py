"""管理网页端账号（管理员 / 归档查看者）。

密码支持直接参数传递或交互式安全输入；存储为 scrypt 加盐哈希。

用法:
    python tools/manage_users.py list                     # 列出所有用户
    python tools/manage_users.py add <用户名> [--viewer]   # 新增（默认 admin）
    python tools/manage_users.py passwd <用户名> [新密码]  # 修改/重置密码
    python tools/manage_users.py role <用户名> <admin|viewer>
    python tools/manage_users.py del <用户名>
    python tools/manage_users.py reset [--force]          # 清空用户并重新生成初始 admin 随机密码

角色:
    admin  —— 管理端全部功能（配置 / 凭证 / 日志 / 重启）+ 归档
    viewer —— 只能访问归档查看器 /archive
"""
import getpass
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.config as cfg  # noqa: E402
from src import auth  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _prompt_password() -> str | None:
    pw = getpass.getpass("请输入密码: ")
    if len(pw) < auth.MIN_PASSWORD_LEN:
        print(f"✗ 密码至少 {auth.MIN_PASSWORD_LEN} 位")
        return None
    if getpass.getpass("请再输入一次: ") != pw:
        print("✗ 两次输入不一致")
        return None
    return pw


def cmd_list() -> int:
    users = auth.load_users()
    if not users:
        print("（还没有任何用户，用 add 创建第一个 admin）")
        return 0
    print(f"{'用户名':<20} {'角色':<8} 创建时间")
    for name, u in sorted(users.items()):
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(u.get("created_at", 0)))
        print(f"{name:<20} {u.get('role', '?'):<8} {created}")
    return 0


def cmd_add(argv: list[str]) -> int:
    if not argv:
        print("用法: python tools/manage_users.py add <用户名> [--viewer]")
        return 1
    username = argv[0]
    role = "viewer" if "--viewer" in argv else "admin"
    pw = _prompt_password()
    if pw is None:
        return 1
    ok, msg = auth.add_user(username, pw, role)
    print(("✅ " if ok else "✗ ") + msg)
    return 0 if ok else 1


def cmd_passwd(argv: list[str]) -> int:
    if not argv:
        print("用法: python tools/manage_users.py passwd <用户名> [新密码]")
        return 1
    username = argv[0]
    if len(argv) >= 2:
        pw = argv[1]
    else:
        pw = _prompt_password()
    if pw is None:
        return 1
    ok, msg = auth.set_password(username, pw)
    print(("✅ " if ok else "✗ ") + msg + ("（该用户已登录的会话已失效）" if ok else ""))
    return 0 if ok else 1


def cmd_role(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法: python tools/manage_users.py role <用户名> <admin|viewer>")
        return 1
    ok, msg = auth.set_role(argv[0], argv[1])
    print(("✅ " if ok else "✗ ") + msg)
    return 0 if ok else 1


def cmd_del(argv: list[str]) -> int:
    if not argv:
        print("用法: python tools/manage_users.py del <用户名>")
        return 1
    if input(f"确定删除用户 {argv[0]}？(y/N) ").strip().lower() != "y":
        print("已取消")
        return 0
    ok, msg = auth.delete_user(argv[0])
    print(("✅ " if ok else "✗ ") + msg)
    return 0 if ok else 1


def cmd_reset(argv: list[str]) -> int:
    """重置系统用户库：清空所有用户并重新生成默认 admin 账号与随机初始密码。"""
    if "--force" not in argv:
        ans = input("⚠️ 警告：重置将清空所有现有用户，并重新生成初始 admin 账号！\n确定继续吗？(y/N) ").strip().lower()
        if ans != "y":
            print("已取消")
            return 0
    auth.save_users({})
    created, u, p = auth.ensure_initial_admin()
    if created:
        print("=" * 60)
        print("✅ 用户库已成功重置！")
        print(f"   • 用户名:   {u}")
        print(f"   • 初始密码: {p}")
        port = getattr(cfg, "WEB_ADMIN_PORT", 46046)
        print(f"   • 登录地址: http://127.0.0.1:{port}/")
        print("=" * 60)
        return 0
    else:
        print("✗ 重置用户库失败")
        return 1


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        return cmd_list()
    if cmd == "add":
        return cmd_add(rest)
    if cmd == "passwd":
        return cmd_passwd(rest)
    if cmd == "role":
        return cmd_role(rest)
    if cmd in ("del", "delete", "rm"):
        return cmd_del(rest)
    if cmd == "reset":
        return cmd_reset(rest)
    print(f"未知命令: {cmd}\n")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
