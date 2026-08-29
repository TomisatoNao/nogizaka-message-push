#!/usr/bin/env bash
# ============================================================
# install_systemd.sh — Linux 部署：systemd 服务（开机自启 + 崩溃自拉起）
# ============================================================
# 用法（在仓库根目录执行）:
#   bash tools/install_systemd.sh              # 安装（用户级，推荐，无需 root）
#   bash tools/install_systemd.sh --system     # 安装为系统级服务（需 sudo）
#   bash tools/install_systemd.sh --status     # 查看状态
#   bash tools/install_systemd.sh --logs       # 跟踪日志
#   bash tools/install_systemd.sh --uninstall  # 卸载
#
# 行为:
#   - 开机自动启动（用户级需 linger，脚本会自动开启）
#   - 进程异常退出后 60s 自动重启，无次数上限
#   - 停止时发 SIGTERM，主程序会走优雅停机（清理连接、收尾归档任务）
#   - 日志进 journald，同时照常写仓库的 logs/ 目录
# ============================================================
set -euo pipefail

SERVICE_NAME="sakamichi-push"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="user"
ACTION="install"

for arg in "$@"; do
  case "$arg" in
    --system)    MODE="system" ;;
    --status)    ACTION="status" ;;
    --logs)      ACTION="logs" ;;
    --uninstall) ACTION="uninstall" ;;
    -h|--help)   sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg"; exit 1 ;;
  esac
done

if [[ "$MODE" == "system" ]]; then
  SYSTEMCTL=(sudo systemctl)
  UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
  JOURNAL=(sudo journalctl -u "$SERVICE_NAME")
else
  SYSTEMCTL=(systemctl --user)
  UNIT_PATH="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"
  JOURNAL=(journalctl --user -u "$SERVICE_NAME")
fi

case "$ACTION" in
  status)
    "${SYSTEMCTL[@]}" status "$SERVICE_NAME" --no-pager || true
    exit 0 ;;
  logs)
    "${JOURNAL[@]}" -f
    exit 0 ;;
  uninstall)
    "${SYSTEMCTL[@]}" disable --now "$SERVICE_NAME" 2>/dev/null || true
    [[ "$MODE" == "system" ]] && sudo rm -f "$UNIT_PATH" || rm -f "$UNIT_PATH"
    "${SYSTEMCTL[@]}" daemon-reload
    echo "✅ 已卸载 ${SERVICE_NAME}（运行中的进程已停止）"
    exit 0 ;;
esac

# ── 安装 ────────────────────────────────────────────────
PYTHON="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON" ]]; then
  echo "✗ 找不到 python3，请先安装" >&2; exit 1
fi
if [[ ! -f "${REPO_DIR}/main.py" ]]; then
  echo "✗ ${REPO_DIR} 下没有 main.py" >&2; exit 1
fi
# 优先使用仓库内的虚拟环境
[[ -x "${REPO_DIR}/.venv/bin/python" ]] && PYTHON="${REPO_DIR}/.venv/bin/python"

UNIT_CONTENT="[Unit]
Description=Sakamichi Message Push (nogizaka-message-push)
Documentation=https://github.com/TomisatoNao/nogizaka-message-push
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
ExecStart=${PYTHON} main.py
# 崩溃 / 异常退出后自动拉起
Restart=always
RestartSec=60
# 主程序自身处理 SIGTERM，做优雅停机
KillSignal=SIGTERM
TimeoutStopSec=90
# 无缓冲输出，日志能实时进 journald
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=$([[ "$MODE" == "system" ]] && echo multi-user.target || echo default.target)
"

echo "▸ 服务名 : ${SERVICE_NAME}（${MODE} 级）"
echo "▸ 仓库   : ${REPO_DIR}"
echo "▸ Python : ${PYTHON}"

if [[ "$MODE" == "system" ]]; then
  # 系统级需指定运行用户，否则默认 root
  UNIT_CONTENT="${UNIT_CONTENT/\[Service\]/[Service]
User=$(id -un)
Group=$(id -gn)}"
  sudo mkdir -p "$(dirname "$UNIT_PATH")"
  printf '%s' "$UNIT_CONTENT" | sudo tee "$UNIT_PATH" >/dev/null
else
  mkdir -p "$(dirname "$UNIT_PATH")"
  printf '%s' "$UNIT_CONTENT" > "$UNIT_PATH"
  # 用户级服务默认在登出后停止，开启 linger 才能开机自启并常驻
  loginctl enable-linger "$(id -un)" 2>/dev/null \
    || echo "  ⚠️ 无法开启 linger，登出后服务会停止（可执行 sudo loginctl enable-linger $(id -un)）"
fi

"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" enable --now "$SERVICE_NAME"

echo
echo "✅ 已安装并启动 ${SERVICE_NAME}"
echo "   状态: bash tools/install_systemd.sh --status"
echo "   日志: bash tools/install_systemd.sh --logs"
echo "   管理端: http://127.0.0.1:46046/"
