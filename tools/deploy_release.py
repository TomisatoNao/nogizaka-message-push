#!/usr/bin/env python3
"""通过 SSH 将经过 CI 验证的不可变镜像部署到 NAS。

脚本只接受 SSH Key/Agent 认证，不读取或保存 NAS 密码。远端部署失败时会用
部署前正在运行的镜像创建本地回滚标签，并恢复该镜像。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys


_TAG_RE = re.compile(r"^(?:sha-[0-9a-f]{7,40}|v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9._-]+)?)$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._/:=-]+$")


REMOTE_DEPLOY_SCRIPT = r'''set -u
REMOTE_DIR=$1
TARGET_TAG=$2
DOCKER_BIN=$3
COMPOSE_FILE=$4
HEALTH_URL=$5
ATTEMPTS=$6
INTERVAL=$7
IMAGE_REPO=$8
SERVICE=sakamichi-push
CONTAINER=sakamichi-push
ENV_FILE=.env
STATE_FILE=.deploy-state

cd "$REMOTE_DIR" || exit 20
if [ ! -f "$COMPOSE_FILE" ]; then
  echo "ERROR: compose file not found: $REMOTE_DIR/$COMPOSE_FILE" >&2
  exit 21
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $REMOTE_DIR/$ENV_FILE is required so mounted secrets remain intact" >&2
  exit 22
fi

umask 077
BACKUP_FILE=".env.release-backup.$$"
cp "$ENV_FILE" "$BACKUP_FILE" || exit 23
PREVIOUS_TAG=$(awk -F= '/^APP_IMAGE_TAG=/{print substr($0, index($0,"=")+1); exit}' "$ENV_FILE")
[ -n "$PREVIOUS_TAG" ] || PREVIOUS_TAG=latest
CURRENT_IMAGE_ID=$("$DOCKER_BIN" inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)
ROLLBACK_TAG="rollback-local-$(date +%s)"
ROLLBACK_REF="$IMAGE_REPO:$ROLLBACK_TAG"

set_tag() {
  new_tag=$1
  temp_file=".env.release-new.$$"
  awk -v tag="$new_tag" '
    BEGIN { found=0 }
    /^APP_IMAGE_TAG=/ { if (!found) { print "APP_IMAGE_TAG=" tag; found=1 }; next }
    { print }
    END { if (!found) print "APP_IMAGE_TAG=" tag }
  ' "$ENV_FILE" > "$temp_file" && mv "$temp_file" "$ENV_FILE"
}

compose() {
  if "$DOCKER_BIN" compose version >/dev/null 2>&1; then
    "$DOCKER_BIN" compose -f "$COMPOSE_FILE" "$@"
  elif [ -x "${DOCKER_BIN}-compose" ]; then
    "${DOCKER_BIN}-compose" -f "$COMPOSE_FILE" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -f "$COMPOSE_FILE" "$@"
  else
    "$DOCKER_BIN" compose -f "$COMPOSE_FILE" "$@"
  fi
}

probe() {
  expected_version=$1
  require_version=$2
  attempt=1
  while [ "$attempt" -le "$ATTEMPTS" ]; do
    state=$("$DOCKER_BIN" inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER" 2>/dev/null || true)
    body=$(curl -fsS --max-time 4 "$HEALTH_URL" 2>/dev/null || true)
    if printf '%s' "$state" | grep -Eq '^running (healthy|starting|none)$' && [ -n "$body" ]; then
      version_ok=0
      if [ "$require_version" = 0 ]; then
        version_ok=1
      elif printf '%s' "$body" | grep -Eq '"version"[[:space:]]*:[[:space:]]*"'"$expected_version"'"'; then
        case "$expected_version" in
          sha-*)
            expected_sha=${expected_version#sha-}
            if printf '%s' "$body" | grep -Eq '"git_sha"[[:space:]]*:[[:space:]]*"'"$expected_sha"'[0-9a-f]*"'; then
              version_ok=1
            fi
            ;;
          *) version_ok=1 ;;
        esac
      fi
      if [ "$version_ok" = 1 ]; then
        return 0
      fi
    fi
    echo "Waiting for $CONTAINER readiness ($attempt/$ATTEMPTS)..."
    attempt=$((attempt + 1))
    sleep "$INTERVAL"
  done
  return 1
}

record_state() {
  outcome=$1
  current=$2
  previous=$3
  digest=$4
  temp_state=".deploy-state.$$"
  {
    echo "DEPLOY_OUTCOME=$outcome"
    echo "CURRENT_IMAGE_TAG=$current"
    echo "PREVIOUS_IMAGE_TAG=$previous"
    echo "IMAGE_DIGEST=$digest"
    echo "UPDATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$temp_state" && mv "$temp_state" "$STATE_FILE"
}

rollback() {
  reason=$1
  echo "Deployment failed: $reason; restoring previous container image..." >&2
  if [ -n "$CURRENT_IMAGE_ID" ] && "$DOCKER_BIN" image tag "$CURRENT_IMAGE_ID" "$ROLLBACK_REF"; then
    cp "$BACKUP_FILE" "$ENV_FILE"
    set_tag "$ROLLBACK_TAG"
    if compose up -d --no-deps "$SERVICE" && probe "$ROLLBACK_TAG" 0; then
      record_state rollback "$ROLLBACK_TAG" "$PREVIOUS_TAG" "$CURRENT_IMAGE_ID"
      rm -f "$BACKUP_FILE"
      echo "Rollback succeeded with local image $ROLLBACK_REF" >&2
      return 0
    fi
  fi
  cp "$BACKUP_FILE" "$ENV_FILE" 2>/dev/null || true
  rm -f "$BACKUP_FILE"
  record_state rollback_failed unknown "$PREVIOUS_TAG" "$CURRENT_IMAGE_ID"
  echo "ERROR: automatic rollback failed; manual recovery is required" >&2
  return 1
}

if [ -n "$CURRENT_IMAGE_ID" ]; then
  "$DOCKER_BIN" image tag "$CURRENT_IMAGE_ID" "$ROLLBACK_REF" || {
    rm -f "$BACKUP_FILE"
    echo "ERROR: unable to preserve current image for rollback" >&2
    exit 24
  }
fi

if ! set_tag "$TARGET_TAG"; then
  cp "$BACKUP_FILE" "$ENV_FILE" 2>/dev/null || true
  rm -f "$BACKUP_FILE"
  exit 25
fi
if ! compose pull "$SERVICE"; then
  cp "$BACKUP_FILE" "$ENV_FILE" 2>/dev/null || true
  rm -f "$BACKUP_FILE"
  if [ -n "$CURRENT_IMAGE_ID" ]; then
    "$DOCKER_BIN" image rm "$ROLLBACK_REF" >/dev/null 2>&1 || true
  fi
  record_state failed_no_change "$PREVIOUS_TAG" "$PREVIOUS_TAG" "$CURRENT_IMAGE_ID"
  echo "Deployment failed: image pull failed; the running container was not changed" >&2
  exit 30
fi
if ! compose up -d --no-deps "$SERVICE"; then
  rollback "container replacement failed"
  exit 31
fi
if ! probe "$TARGET_TAG" 1; then
  rollback "health/version verification timed out"
  exit 32
fi

TARGET_REF="$IMAGE_REPO:$TARGET_TAG"
DIGEST=$("$DOCKER_BIN" image inspect --format '{{index .RepoDigests 0}}' "$TARGET_REF" 2>/dev/null || true)
record_state success "$TARGET_TAG" "$PREVIOUS_TAG" "$DIGEST"
rm -f "$BACKUP_FILE"
if [ -n "$CURRENT_IMAGE_ID" ]; then
  "$DOCKER_BIN" image rm "$ROLLBACK_REF" >/dev/null 2>&1 || true
fi
case "$PREVIOUS_TAG" in
  rollback-local-*) "$DOCKER_BIN" image rm "$IMAGE_REPO:$PREVIOUS_TAG" >/dev/null 2>&1 || true ;;
esac
echo "Deployment succeeded: $TARGET_REF"
echo "Digest: ${DIGEST:-unavailable}"
'''


def _validated(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"{label} contains unsupported characters: {value!r}")
    return value


def _default_tag() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return f"sha-{result.stdout.strip()}"


def build_ssh_command(args: argparse.Namespace) -> list[str]:
    host = _validated(args.host, _HOST_RE, "NAS host")
    user = _validated(args.user, _USER_RE, "NAS user")
    remote_dir = _validated(args.remote_dir, _REMOTE_PATH_RE, "remote directory")
    tag = _validated(args.tag, _TAG_RE, "image tag")
    docker_bin = _validated(args.docker_bin, _SAFE_TOKEN_RE, "Docker binary")
    compose_file = _validated(args.compose_file, _SAFE_TOKEN_RE, "Compose file")
    health_url = _validated(args.health_url, _SAFE_TOKEN_RE, "health URL")
    image_repo = _validated(args.image_repo, _SAFE_TOKEN_RE, "image repository")

    command = [
        "ssh", "-p", str(args.port),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
    ]
    if args.identity:
        command.extend(["-i", str(Path(args.identity).expanduser())])
    command.extend([
        f"{user}@{host}",
        "sh", "-s", "--",
        remote_dir, tag, docker_bin, compose_file, health_url,
        str(args.attempts), str(args.interval), image_repo,
    ])
    return command


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy a verified immutable image to a NAS with rollback")
    parser.add_argument("--tag", default=os.getenv("APP_IMAGE_TAG") or None,
                        help="immutable image tag, normally sha-xxxxxxx (default: current Git commit)")
    parser.add_argument("--host", default=os.getenv("NAS_HOST"), help="NAS host (or NAS_HOST)")
    parser.add_argument("--user", default=os.getenv("NAS_USER"), help="SSH user (or NAS_USER)")
    parser.add_argument("--port", type=int, default=int(os.getenv("NAS_PORT", "22")))
    parser.add_argument("--identity", default=os.getenv("NAS_SSH_KEY"), help="SSH private key path")
    parser.add_argument("--remote-dir", default=os.getenv("NAS_REMOTE_DIR", "/volume1/docker/nogizaka-push"))
    parser.add_argument("--docker-bin", default=os.getenv("NAS_DOCKER_BIN", "docker"))
    parser.add_argument("--compose-file", default=os.getenv("NAS_COMPOSE_FILE", "docker-compose.yml"))
    parser.add_argument("--health-url", default=os.getenv(
        "NAS_HEALTH_URL", "http://127.0.0.1:46046/api/health/status"))
    parser.add_argument("--image-repo", default="ghcr.io/tomisatonao/nogizaka-message-push")
    parser.add_argument("--attempts", type=int, default=18)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="print the target without connecting")
    args = parser.parse_args(argv)
    if not args.host or not args.user:
        parser.error("--host/NAS_HOST and --user/NAS_USER are required")
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")
    if args.attempts < 1 or not (1 <= args.interval <= 60):
        parser.error("attempts must be positive and interval must be 1..60 seconds")
    if not args.tag:
        args.tag = _default_tag()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        command = build_ssh_command(args)
    except ValueError as exc:
        print(f"Invalid deployment argument: {exc}", file=sys.stderr)
        return 2

    destination = f"{args.user}@{args.host}:{args.remote_dir}"
    print(f"Deploying {args.image_repo}:{args.tag} to {destination}")
    if args.dry_run:
        print("Dry run complete; no SSH connection was made.")
        return 0

    result = subprocess.run(command, input=REMOTE_DEPLOY_SCRIPT, text=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
