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
COMPOSE_ENV_FILE=.deploy-compose-env.$$
COMPOSE_ENV_TEMP="$COMPOSE_ENV_FILE.tmp"
COMPOSE_TEMP=.deploy-compose.$$
COMPOSE_TEMP_TMP="$COMPOSE_TEMP.tmp"
PINNED_COMPOSE_TEMP=.deploy-compose-pinned.$$
PINNED_COMPOSE_TMP="$PINNED_COMPOSE_TEMP.tmp"
STATE_TEMP=".deploy-state.$$"
COMPOSE_BACKUP=.deploy-compose.previous
ACTIVE_COMPOSE_FILE=$COMPOSE_FILE

cd "$REMOTE_DIR" || exit 20
if [ ! -f "$COMPOSE_FILE" ]; then
  echo "ERROR: compose file not found: $REMOTE_DIR/$COMPOSE_FILE" >&2
  exit 21
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $REMOTE_DIR/$ENV_FILE is required as a mounted application secret file" >&2
  exit 22
fi

umask 077
cleanup() {
  rm -f "$COMPOSE_ENV_FILE" "$COMPOSE_ENV_TEMP" "$COMPOSE_TEMP" "$COMPOSE_TEMP_TMP" "$PINNED_COMPOSE_TEMP" "$PINNED_COMPOSE_TMP" "$STATE_TEMP" "$COMPOSE_BACKUP.tmp"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
unset APP_IMAGE_TAG WEB_ADMIN_BIND NAPCAT_EVENT_BIND

# Only the default app Compose file is supported. Secret-bearing variants need an
# explicit, separately reviewed deployment path rather than empty interpolation.
COMPOSE_VARS=$(sed 's/#.*//' "$COMPOSE_FILE" | grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*' | sed 's/.*{//' | sort -u || true)
if [ -n "$COMPOSE_VARS" ]; then
  UNSUPPORTED_VARS=$(printf '%s\n' "$COMPOSE_VARS" | grep -Ev '^(APP_IMAGE_TAG|WEB_ADMIN_BIND|NAPCAT_EVENT_BIND)$' || true)
  if [ -n "$UNSUPPORTED_VARS" ]; then
    echo "ERROR: unsupported Compose interpolation variables: $UNSUPPORTED_VARS" >&2
    exit 23
  fi
fi

COMPOSE_KIND=
if "$DOCKER_BIN" compose version >/dev/null 2>&1; then
  COMPOSE_KIND=docker
elif [ -x "${DOCKER_BIN}-compose" ]; then
  COMPOSE_KIND=adjacent
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_KIND=path
else
  echo "ERROR: Docker Compose is unavailable" >&2
  exit 24
fi
compose_help() {
  case "$COMPOSE_KIND" in
    docker) "$DOCKER_BIN" compose --help ;;
    adjacent) "${DOCKER_BIN}-compose" --help ;;
    path) docker-compose --help ;;
  esac
}
if ! compose_help 2>&1 | grep -q -- '--env-file'; then
  echo "ERROR: Compose does not support --env-file; refusing to read the protected .env file" >&2
  exit 25
fi

compose() {
  case "$COMPOSE_KIND" in
    docker) "$DOCKER_BIN" compose --env-file "$COMPOSE_ENV_FILE" -f "$ACTIVE_COMPOSE_FILE" "$@" ;;
    adjacent) "${DOCKER_BIN}-compose" --env-file "$COMPOSE_ENV_FILE" -f "$ACTIVE_COMPOSE_FILE" "$@" ;;
    path) docker-compose --env-file "$COMPOSE_ENV_FILE" -f "$ACTIVE_COMPOSE_FILE" "$@" ;;
  esac
}

write_compose_env() {
  temp_file=$COMPOSE_ENV_TEMP
  {
    printf 'APP_IMAGE_TAG=%s\n' "$1"
    printf 'WEB_ADMIN_BIND=%s\n' "$WEB_ADMIN_BIND"
    printf 'NAPCAT_EVENT_BIND=%s\n' "$NAPCAT_EVENT_BIND"
  } > "$temp_file" && mv "$temp_file" "$COMPOSE_ENV_FILE"
}

write_compose_tag() {
  new_tag=$1
  destination=$2
  image_lines=$(grep -Ec "^[[:space:]]*image:[[:space:]]*$IMAGE_REPO:" "$COMPOSE_FILE" || true)
  if [ "$image_lines" != 1 ]; then
    echo "ERROR: expected exactly one application image line in $COMPOSE_FILE" >&2
    return 1
  fi
  sed -E "s|^([[:space:]]*image:[[:space:]]*$IMAGE_REPO:).*|\1$new_tag|" "$COMPOSE_FILE" > "$destination.tmp" &&
    chmod 600 "$destination.tmp" &&
    mv "$destination.tmp" "$destination"
}

pin_main_compose() {
  pinned_tag=$1
  if ! write_compose_tag "$pinned_tag" "$PINNED_COMPOSE_TEMP"; then
    return 1
  fi
  if ! cp "$COMPOSE_FILE" "$COMPOSE_BACKUP.tmp" ||
     ! chmod 600 "$COMPOSE_BACKUP.tmp" ||
     ! mv "$COMPOSE_BACKUP.tmp" "$COMPOSE_BACKUP"; then
    echo "ERROR: could not preserve the small Compose config rollback copy" >&2
    return 1
  fi
  if ! mv "$PINNED_COMPOSE_TEMP" "$COMPOSE_FILE"; then
    echo "ERROR: could not atomically pin image tag in $COMPOSE_FILE" >&2
    return 1
  fi
  chmod 644 "$COMPOSE_FILE" || return 1
}

valid_ipv4() {
  case "$1" in
    ''|*[!0-9.]* ) return 1 ;;
  esac
  printf '%s\n' "$1" | awk -F. '{
    if (NF != 4) bad=1
    for (i=1; i<=NF; i++) if ($i == "" || $i+0 > 255) bad=1
  } END { if (NF != 4 || bad) exit 1; exit 0 }'
}

CONTAINER_STATUS=$("$DOCKER_BIN" inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || true)
if [ "$CONTAINER_STATUS" != running ]; then
  echo "ERROR: expected the current $CONTAINER container to be running; refusing deployment" >&2
  exit 26
fi
CURRENT_IMAGE_ID=$("$DOCKER_BIN" inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)
CURRENT_IMAGE_REF=$("$DOCKER_BIN" inspect --format '{{.Config.Image}}' "$CONTAINER" 2>/dev/null || true)
case "$CURRENT_IMAGE_REF" in
  "$IMAGE_REPO":*) PREVIOUS_TAG=${CURRENT_IMAGE_REF#"$IMAGE_REPO":} ;;
  *) echo "ERROR: current image reference is outside $IMAGE_REPO; refusing deployment" >&2; exit 27 ;;
esac
case "$PREVIOUS_TAG" in
  ''|*[!A-Za-z0-9_.-]*)
    echo "ERROR: current image reference has no safe immutable rollback tag" >&2
    exit 27
    ;;
esac
if ! printf '%s\n' "$PREVIOUS_TAG" | grep -Eq '^(sha-[0-9a-f]{7,40}|v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9._-]+)?)$'; then
  echo "ERROR: current image reference is not a supported immutable release tag" >&2
  exit 27
fi
if [ -z "$CURRENT_IMAGE_ID" ]; then
  echo "ERROR: unable to capture the current image ID for rollback" >&2
  exit 28
fi

ADMIN_BIND_COUNT=$("$DOCKER_BIN" inspect --format '{{with index .HostConfig.PortBindings "46046/tcp"}}{{len .}}{{end}}' "$CONTAINER" 2>/dev/null || true)
WEB_ADMIN_BIND=$("$DOCKER_BIN" inspect --format '{{with index .HostConfig.PortBindings "46046/tcp"}}{{(index . 0).HostIp}}{{end}}' "$CONTAINER" 2>/dev/null || true)
ADMIN_HOST_PORT=$("$DOCKER_BIN" inspect --format '{{with index .HostConfig.PortBindings "46046/tcp"}}{{(index . 0).HostPort}}{{end}}' "$CONTAINER" 2>/dev/null || true)
EVENT_BIND_COUNT=$("$DOCKER_BIN" inspect --format '{{with index .HostConfig.PortBindings "46047/tcp"}}{{len .}}{{end}}' "$CONTAINER" 2>/dev/null || true)
NAPCAT_EVENT_BIND=$("$DOCKER_BIN" inspect --format '{{with index .HostConfig.PortBindings "46047/tcp"}}{{(index . 0).HostIp}}{{end}}' "$CONTAINER" 2>/dev/null || true)
EVENT_HOST_PORT=$("$DOCKER_BIN" inspect --format '{{with index .HostConfig.PortBindings "46047/tcp"}}{{(index . 0).HostPort}}{{end}}' "$CONTAINER" 2>/dev/null || true)
if [ "$ADMIN_BIND_COUNT" != 1 ] || [ "$EVENT_BIND_COUNT" != 1 ] ||
   [ "$ADMIN_HOST_PORT" != 46046 ] || [ "$EVENT_HOST_PORT" != 46047 ] ||
   ! valid_ipv4 "$WEB_ADMIN_BIND" || ! valid_ipv4 "$NAPCAT_EVENT_BIND"; then
  echo "ERROR: expected exactly one IPv4 binding on host ports 46046 and 46047; refusing to alter exposure" >&2
  exit 29
fi

write_pinned_compose_for() {
  tag=$1
  if ! write_compose_tag "$tag" "$COMPOSE_TEMP"; then
    return 1
  fi
  ACTIVE_COMPOSE_FILE=$COMPOSE_TEMP
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
  {
    echo "DEPLOY_OUTCOME=$outcome"
    echo "CURRENT_IMAGE_TAG=$current"
    echo "PREVIOUS_IMAGE_TAG=$previous"
    echo "IMAGE_DIGEST=$digest"
    echo "UPDATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$STATE_TEMP" && mv "$STATE_TEMP" "$STATE_FILE"
}

ROLLBACK_TAG="rollback-local-$(date +%s)"
ROLLBACK_REF="$IMAGE_REPO:$ROLLBACK_TAG"
rollback() {
  reason=$1
  echo "Deployment failed: $reason; restoring previous container image..." >&2
  if "$DOCKER_BIN" image tag "$CURRENT_IMAGE_ID" "$ROLLBACK_REF" &&
     write_compose_env "$ROLLBACK_TAG" &&
     write_pinned_compose_for "$ROLLBACK_TAG" &&
     compose up -d --no-deps "$SERVICE" &&
     probe "$ROLLBACK_TAG" 0; then
    RESTORED_IMAGE_ID=$("$DOCKER_BIN" inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)
    if [ "$RESTORED_IMAGE_ID" = "$CURRENT_IMAGE_ID" ] && pin_main_compose "$PREVIOUS_TAG"; then
      record_state rollback "$PREVIOUS_TAG" "$PREVIOUS_TAG" "$CURRENT_IMAGE_ID" || true
      "$DOCKER_BIN" image rm "$ROLLBACK_REF" >/dev/null 2>&1 || true
      echo "Rollback succeeded; restored image ID $CURRENT_IMAGE_ID" >&2
      return 0
    fi
  fi
  record_state rollback_failed unknown "$PREVIOUS_TAG" "$CURRENT_IMAGE_ID" || true
  echo "ERROR: automatic rollback failed; manual recovery is required" >&2
  return 1
}

if ! write_compose_env "$TARGET_TAG" || ! write_pinned_compose_for "$TARGET_TAG"; then
  echo "ERROR: unable to prepare a pinned Compose release config" >&2
  exit 30
fi
"$DOCKER_BIN" image tag "$CURRENT_IMAGE_ID" "$ROLLBACK_REF" || {
  echo "ERROR: unable to preserve current image for rollback" >&2
  exit 31
}

if ! compose pull "$SERVICE"; then
  "$DOCKER_BIN" image rm "$ROLLBACK_REF" >/dev/null 2>&1 || true
  record_state failed_no_change "$PREVIOUS_TAG" "$PREVIOUS_TAG" "$CURRENT_IMAGE_ID" || true
  echo "Deployment failed: image pull failed; the running container was not changed" >&2
  exit 32
fi
if ! compose up -d --no-deps "$SERVICE"; then
  rollback "container replacement failed"
  exit 33
fi
if ! probe "$TARGET_TAG" 1; then
  rollback "health/version verification timed out"
  exit 34
fi
if ! pin_main_compose "$TARGET_TAG"; then
  rollback "unable to persist the verified image tag in Compose"
  exit 35
fi

TARGET_REF="$IMAGE_REPO:$TARGET_TAG"
DIGEST=$("$DOCKER_BIN" image inspect --format '{{index .RepoDigests 0}}' "$TARGET_REF" 2>/dev/null || true)
record_state success "$TARGET_TAG" "$PREVIOUS_TAG" "$DIGEST" || {
  echo "WARNING: release is healthy but deployment state could not be recorded" >&2
}
"$DOCKER_BIN" image rm "$ROLLBACK_REF" >/dev/null 2>&1 || true
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

    script_bytes = REMOTE_DEPLOY_SCRIPT.replace("\r\n", "\n").encode("utf-8")
    result = subprocess.run(command, input=script_bytes)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
