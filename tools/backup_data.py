"""创建、校验和恢复项目持久化数据备份。

默认仅处理 config/ 与 data/，不会收集日志、源码或 .env。备份通常包含
凭据数据库，应妥善保存在受限位置。恢复操作必须显式传入 --apply。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = PROJECT_ROOT / "backups"
INCLUDED_DIRS = ("config", "data")
_EXCLUDED_PARTS = frozenset({"__pycache__", ".pytest_cache", "history"})
_EXCLUDED_FILES = frozenset({"app.pid"})
_MANIFEST_NAME = "manifest.json"


def _included_file(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(part in _EXCLUDED_PARTS for part in relative.parts)
        and path.name not in _EXCLUDED_FILES
        and not path.name.endswith((".pyc", ".tmp"))
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirname in INCLUDED_DIRS:
        base = root / dirname
        if base.is_dir():
            files.extend(path for path in base.rglob("*") if _included_file(root, path))
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _manifest(root: Path, files: list[Path]) -> dict:
    return {
        "format": "sakamichi-backup-v1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": [
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": _hash_file(path),
            }
            for path in files
        ],
    }


def _prune(destination: Path, keep: int) -> None:
    archives = sorted(destination.glob("sakamichi-backup-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in archives[max(1, keep):]:
        old.unlink(missing_ok=True)


def create_backup(root: Path = PROJECT_ROOT, destination: Path = BACKUP_DIR, keep: int = 7) -> tuple[Path, dict]:
    """创建备份并返回归档路径和不含敏感内容的清单。"""
    root = root.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    files = _collect_files(root)
    manifest = _manifest(root, files)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = destination / f"sakamichi-backup-{stamp}.tar.gz"
    suffix = 1
    while archive.exists():
        archive = destination / f"sakamichi-backup-{stamp}-{suffix}.tar.gz"
        suffix += 1

    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for path in files:
            tar.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
        raw_manifest = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo(_MANIFEST_NAME)
        info.size = len(raw_manifest)
        info.mtime = int(datetime.now().timestamp())
        tar.addfile(info, io.BytesIO(raw_manifest))

    try:
        os.chmod(archive, 0o600)
    except OSError:
        pass
    _prune(destination, keep)
    return archive, manifest


def _safe_archive_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) >= 2
        and path.parts[0] in INCLUDED_DIRS
    )


def verify_backup(archive: Path) -> tuple[bool, list[str], dict | None]:
    """校验归档路径、清单与每个文件哈希；不写入任何数据。"""
    errors: list[str] = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            manifest_member = tar.getmember(_MANIFEST_NAME)
            raw = tar.extractfile(manifest_member)
            manifest = json.loads(raw.read().decode("utf-8")) if raw else None
            if not isinstance(manifest, dict) or manifest.get("format") != "sakamichi-backup-v1":
                return False, ["备份清单无效"], None
            expected = {item.get("path"): item for item in manifest.get("files", []) if isinstance(item, dict)}
            if len(expected) != len(manifest.get("files", [])):
                errors.append("备份清单含重复或无效文件路径")
            for name, item in expected.items():
                if not isinstance(name, str) or not _safe_archive_path(name):
                    errors.append(f"非法归档路径: {name!r}")
                    continue
                try:
                    member = tar.getmember(name)
                    if not member.isfile():
                        errors.append(f"不是普通文件: {name}")
                        continue
                    data = tar.extractfile(member)
                    digest = hashlib.sha256(data.read() if data else b"").hexdigest()
                    if digest != item.get("sha256"):
                        errors.append(f"哈希不匹配: {name}")
                    if member.size != item.get("size"):
                        errors.append(f"大小不匹配: {name}")
                except KeyError:
                    errors.append(f"归档缺少文件: {name}")
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        return False, [f"无法读取备份: {exc}"], None
    return not errors, errors, manifest


def restore_backup(archive: Path, root: Path = PROJECT_ROOT, *, apply: bool = False) -> dict:
    """校验后恢复文件；默认只返回计划，传入 apply 才会覆盖现有文件。"""
    ok, errors, manifest = verify_backup(archive)
    if not ok or manifest is None:
        raise ValueError("备份校验失败：" + "; ".join(errors))
    files = manifest["files"]
    result = {"archive": str(archive), "files": len(files), "applied": apply}
    if not apply:
        return result

    root = root.resolve()
    with tempfile.TemporaryDirectory(prefix="sakamichi-restore-", dir=root) as temp_dir:
        staging = Path(temp_dir)
        with tarfile.open(archive, "r:gz") as tar:
            for item in files:
                rel = PurePosixPath(item["path"])
                source = tar.extractfile(item["path"])
                if source is None:
                    raise ValueError(f"归档读取失败: {item['path']}")
                destination = staging.joinpath(*rel.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as f:
                    shutil.copyfileobj(source, f)
        for dirname in INCLUDED_DIRS:
            staged_dir = staging / dirname
            if staged_dir.exists():
                shutil.copytree(staged_dir, root / dirname, dirs_exist_ok=True, copy_function=shutil.copy2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="创建、校验或恢复 config/ 和 data/ 的本地备份")
    parser.add_argument("command", choices=("create", "verify", "restore"))
    parser.add_argument("archive", nargs="?", type=Path, help="verify/restore 所需的 .tar.gz 文件")
    parser.add_argument("--output-dir", type=Path, default=BACKUP_DIR, help="备份输出目录")
    parser.add_argument("--keep", type=int, default=7, help="创建后保留的最近备份数量")
    parser.add_argument("--apply", action="store_true", help="实际执行恢复；恢复前应先停止服务")
    args = parser.parse_args()

    if args.command == "create":
        archive, manifest = create_backup(destination=args.output_dir, keep=args.keep)
        print(json.dumps({"ok": True, "archive": str(archive), "files": len(manifest["files"])}, ensure_ascii=False))
        return 0
    if args.archive is None:
        parser.error("verify 和 restore 必须提供备份文件路径")
    if args.command == "verify":
        ok, errors, manifest = verify_backup(args.archive)
        print(json.dumps({"ok": ok, "files": len((manifest or {}).get("files", [])), "errors": errors}, ensure_ascii=False))
        return 0 if ok else 1
    if not args.apply:
        print("恢复预演完成；未写入任何文件。确认已停止服务后，使用 restore <archive> --apply。")
        print(json.dumps(restore_backup(args.archive, apply=False), ensure_ascii=False))
        return 0
    result = restore_backup(args.archive, apply=True)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
