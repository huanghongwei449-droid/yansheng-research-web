#!/usr/bin/env python3
"""Safety boundaries for the AlphaEngine production checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from daily_research_bus import git_checked, git_run


PRODUCTION_BRANCH = "main"
PRODUCTION_REMOTE = "origin"
PRODUCTION_REF = "origin/main"
PROJECT_TASK_PREFIX = "AlphaEngine"
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
FILE_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
FREEZE_SNAPSHOT_ENV = "ALPHAENGINE_PRODUCTION_FREEZE_SNAPSHOT"
FREEZE_SNAPSHOT_PARENT = "alphaengine-deployment-snapshots"
FREEZE_SNAPSHOT_PREFIX = "production-freeze-"
FREEZE_METADATA_PATH = Path("snapshot-metadata.json")
FREEZE_MANIFEST_PATH = Path("promotion") / "planned-deployment-manifest.json"
FREEZE_ROLLBACK_REQUIRED_FILES = (
    Path("rollback") / "README.txt",
    Path("git") / "status.porcelain-v2.txt",
    Path("git") / "tracked-diff.patch",
    Path("git") / "dirty-file-sha256.json",
)
WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n\s;,]+")
UNIX_PATH_RE = re.compile(r"(?<![:\w])/(?:[^\s/]+/)+[^\s]*")
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_ -]?key|token|cookie|secret|credential|password|authorization|bearer)\s*[:=]\s*[^\s,;]+"
)


class ProductionRuntimeError(RuntimeError):
    """Raised when a production-root safety invariant is not satisfied."""


def _sanitize_error(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_ASSIGNMENT_RE.sub("[redacted]", text)
    text = WINDOWS_PATH_RE.sub("[local-path]", text)
    text = UNIX_PATH_RE.sub("[local-path]", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def production_snapshot(root: Path) -> dict[str, Any]:
    """Return production identity without exposing the local root path."""

    root = Path(root).resolve()
    actual = Path(git_checked(root, "rev-parse", "--show-toplevel")).resolve()
    if actual != root:
        raise ProductionRuntimeError("root is not the repository top level")
    status = git_checked(root, "status", "--porcelain", "--untracked-files=all")
    branch = git_checked(root, "branch", "--show-current")
    head = git_checked(root, "rev-parse", "HEAD")
    origin_main = git_checked(root, "rev-parse", PRODUCTION_REF)
    return {
        "branch": branch,
        "head": head,
        "origin_main": origin_main,
        "dirty": bool(status),
    }


def assert_production_ready(root: Path) -> dict[str, Any]:
    """Require a clean production checkout already aligned with origin/main."""

    snapshot = production_snapshot(root)
    if snapshot["dirty"]:
        raise ProductionRuntimeError("production root is not clean; refusing structured task")
    if snapshot["branch"].lower() != PRODUCTION_BRANCH:
        raise ProductionRuntimeError("production root is not on main; refusing structured task")
    if snapshot["head"].lower() != snapshot["origin_main"].lower():
        raise ProductionRuntimeError("production root is not aligned with origin/main; refusing structured task")
    return snapshot


def assert_snapshot_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Fail closed if a task changed the production checkout identity."""

    for key in ("branch", "head", "dirty"):
        if before.get(key) != after.get(key):
            raise ProductionRuntimeError("production root changed during structured task")


def safe_align_production_root(
    root: Path,
    *,
    remote: str = PRODUCTION_REMOTE,
    branch: str = PRODUCTION_BRANCH,
) -> dict[str, Any]:
    """Align a clean production checkout to main using fast-forward only."""

    if remote != PRODUCTION_REMOTE or branch != PRODUCTION_BRANCH:
        raise ProductionRuntimeError("only origin/main may be used for production alignment")
    before = production_snapshot(root)
    if before["dirty"]:
        raise ProductionRuntimeError("production root is not clean; manual review required")
    if before["branch"].lower() != PRODUCTION_BRANCH:
        git_checked(root, "switch", PRODUCTION_BRANCH)
    git_checked(root, "pull", "--ff-only", remote, branch)
    after = production_snapshot(root)
    if after["dirty"]:
        raise ProductionRuntimeError("production root became dirty during alignment")
    if after["branch"].lower() != PRODUCTION_BRANCH:
        raise ProductionRuntimeError("production root alignment did not leave main checked out")
    if after["head"].lower() != after["origin_main"].lower():
        raise ProductionRuntimeError("production root alignment did not reach origin/main")
    return after


def bootstrap_production_root(
    root: Path,
    *,
    remote: str = PRODUCTION_REMOTE,
    branch: str = PRODUCTION_BRANCH,
) -> dict[str, Any]:
    """Fetch and safely bootstrap a production root before a long-running task.

    The bootstrap is intentionally stricter than the legacy manual alignment
    helper: it never changes branches.  A clean ``main`` checkout may only be
    fast-forwarded to ``origin/main``; dirty, non-main, ahead, and diverged
    checkouts fail closed before any merge is attempted.
    """

    if remote != PRODUCTION_REMOTE or branch != PRODUCTION_BRANCH:
        raise ProductionRuntimeError("only origin/main may be used for production bootstrap")
    before = production_snapshot(root)
    if before["dirty"]:
        raise ProductionRuntimeError("production root is not clean; bootstrap refused")
    if before["branch"].lower() != PRODUCTION_BRANCH:
        raise ProductionRuntimeError("production root is not on main; bootstrap refused")

    git_checked(root, "fetch", "--no-tags", remote, f"refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    fetched = production_snapshot(root)
    if fetched["dirty"]:
        raise ProductionRuntimeError("production root became dirty during bootstrap")
    if fetched["branch"].lower() != PRODUCTION_BRANCH:
        raise ProductionRuntimeError("production branch changed during bootstrap")
    if fetched["head"].lower() == fetched["origin_main"].lower():
        return fetched

    behind_check = git_run(root, "merge-base", "--is-ancestor", "HEAD", PRODUCTION_REF)
    if behind_check.returncode != 0:
        raise ProductionRuntimeError("production main is ahead of or diverged from origin/main")
    git_checked(root, "merge", "--ff-only", PRODUCTION_REF)
    after = production_snapshot(root)
    if after["dirty"]:
        raise ProductionRuntimeError("production root became dirty during fast-forward")
    if after["branch"].lower() != PRODUCTION_BRANCH:
        raise ProductionRuntimeError("production bootstrap did not leave main checked out")
    if after["head"].lower() != after["origin_main"].lower():
        raise ProductionRuntimeError("production bootstrap did not reach origin/main")
    return after


def _path_is_within(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is contained by ``parent`` after resolution."""

    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_file(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProductionRuntimeError(f"approved production freeze {label} is missing") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionRuntimeError(f"approved production freeze {label} is invalid") from exc


def _resolve_freeze_snapshot(root: Path, snapshot_root: Path | None) -> Path:
    """Resolve exactly one approved freeze directory without guessing across roots."""

    approved_parent = (root.resolve().parent / FREEZE_SNAPSHOT_PARENT).resolve()
    configured = snapshot_root
    if configured is None:
        configured_value = os.environ.get(FREEZE_SNAPSHOT_ENV, "").strip()
        configured = Path(configured_value) if configured_value else None
    if configured is not None:
        candidate = configured.expanduser().resolve()
        if not _path_is_within(candidate, approved_parent) or not candidate.name.startswith(FREEZE_SNAPSHOT_PREFIX):
            raise ProductionRuntimeError("approved production freeze is outside the approved snapshot directory")
        if not candidate.is_dir():
            raise ProductionRuntimeError("approved production freeze snapshot is missing")
        return candidate

    candidates = sorted(
        (
            item.resolve()
            for item in approved_parent.glob(f"{FREEZE_SNAPSHOT_PREFIX}*")
            if item.is_dir()
        ),
        key=lambda item: item.name,
    )
    if not candidates:
        raise ProductionRuntimeError("approved production freeze snapshot is missing")
    if len(candidates) != 1:
        raise ProductionRuntimeError("multiple production freeze snapshots require explicit selection")
    return candidates[0]


def _validate_freeze_manifest(root: Path, freeze_root: Path) -> dict[str, Any]:
    """Validate every target byte named by the immutable deployment manifest."""

    manifest_path = freeze_root / FREEZE_MANIFEST_PATH
    manifest = _load_json_file(manifest_path, "deployment manifest")
    if not isinstance(manifest, list) or not manifest:
        raise ProductionRuntimeError("approved production freeze deployment manifest is empty or invalid")

    checked_paths: set[str] = set()
    for entry in manifest:
        if not isinstance(entry, dict):
            raise ProductionRuntimeError("approved production freeze deployment manifest entry is invalid")
        raw_path = str(entry.get("path") or "").replace("\\", "/").strip()
        relative = Path(raw_path)
        if not raw_path or relative.is_absolute() or ".." in relative.parts:
            raise ProductionRuntimeError("approved production freeze deployment manifest contains an unsafe path")
        normalized = relative.as_posix()
        if normalized in checked_paths:
            raise ProductionRuntimeError("approved production freeze deployment manifest contains duplicate paths")
        checked_paths.add(normalized)
        if entry.get("after_exists") is not True:
            raise ProductionRuntimeError(f"approved production freeze target is not expected to exist: {normalized}")
        expected = str(entry.get("after_sha256") or "").lower()
        if not FILE_SHA256_RE.fullmatch(expected):
            raise ProductionRuntimeError(f"approved production freeze target hash is invalid: {normalized}")
        target = (root / relative).resolve()
        if not _path_is_within(target, root):
            raise ProductionRuntimeError("approved production freeze deployment target escapes production root")
        if not target.is_file() or _sha256_file(target).lower() != expected:
            raise ProductionRuntimeError(f"approved production freeze target hash mismatch: {normalized}")

    return {
        "path": str(FREEZE_MANIFEST_PATH).replace("\\", "/"),
        "file_count": len(checked_paths),
        "sha256": _sha256_file(manifest_path),
        "validated": True,
    }


def _validate_freeze_rollback(freeze_root: Path) -> dict[str, Any]:
    """Require the file-scoped rollback material captured with the freeze."""

    for relative in FREEZE_ROLLBACK_REQUIRED_FILES:
        if not (freeze_root / relative).is_file():
            raise ProductionRuntimeError(f"approved production freeze rollback material is missing: {relative.as_posix()}")
    dirty_inventory = _load_json_file(freeze_root / "git" / "dirty-file-sha256.json", "dirty-file inventory")
    if not isinstance(dirty_inventory, list):
        raise ProductionRuntimeError("approved production freeze dirty-file inventory is invalid")
    rollback_dir = freeze_root / "dirty-files"
    if not rollback_dir.is_dir():
        raise ProductionRuntimeError("approved production freeze dirty-file rollback directory is missing")
    for entry in dirty_inventory:
        if not isinstance(entry, dict):
            raise ProductionRuntimeError("approved production freeze dirty-file inventory entry is invalid")
        raw_path = str(entry.get("path") or "").replace("\\", "/").strip()
        relative = Path(raw_path)
        expected = str(entry.get("sha256") or "").lower()
        if not raw_path or relative.is_absolute() or ".." in relative.parts or not FILE_SHA256_RE.fullmatch(expected):
            raise ProductionRuntimeError("approved production freeze dirty-file inventory is unsafe")
        rollback_copy = (rollback_dir / relative).resolve()
        if not _path_is_within(rollback_copy, rollback_dir) or not rollback_copy.is_file():
            raise ProductionRuntimeError(f"approved production freeze rollback copy is missing: {raw_path}")
        if _sha256_file(rollback_copy).lower() != expected:
            raise ProductionRuntimeError(f"approved production freeze rollback copy hash mismatch: {raw_path}")
    return {"validated": True, "dirty_file_count": len(dirty_inventory)}


def verify_approved_frozen_production(root: Path, *, snapshot_root: Path | None = None) -> dict[str, Any]:
    """Allow the watcher to run only against an explicitly verifiable frozen root.

    This is intentionally separate from ``bootstrap_production_root``.  A frozen
    deployment may retain approved dirty files, but it may not skip identity,
    deployment-manifest, or rollback verification.  Ordinary structured tasks
    continue to require a clean checkout aligned with ``origin/main``.
    """

    root = Path(root).resolve()
    current = production_snapshot(root)
    if current["branch"].lower() != PRODUCTION_BRANCH:
        raise ProductionRuntimeError("approved frozen production must be on main")
    if not GIT_SHA_RE.fullmatch(str(current["head"])):
        raise ProductionRuntimeError("approved frozen production HEAD is invalid")

    freeze_root = _resolve_freeze_snapshot(root, snapshot_root)
    metadata = _load_json_file(freeze_root / FREEZE_METADATA_PATH, "metadata")
    if not isinstance(metadata, dict) or metadata.get("snapshot_type") != "production-deployment-freeze":
        raise ProductionRuntimeError("approved production freeze metadata is invalid")
    metadata_root_value = metadata.get("production_root")
    if not isinstance(metadata_root_value, str) or not metadata_root_value.strip():
        raise ProductionRuntimeError("approved production freeze root is missing")
    metadata_root = Path(metadata_root_value).resolve()
    if metadata_root != root:
        raise ProductionRuntimeError("approved production freeze root does not match production root")
    if str(metadata.get("production_branch") or "").lower() != PRODUCTION_BRANCH:
        raise ProductionRuntimeError("approved production freeze branch is invalid")
    if str(metadata.get("production_head") or "").lower() != str(current["head"]).lower():
        raise ProductionRuntimeError("approved production freeze HEAD does not match production HEAD")
    if metadata.get("snapshot_readonly_pending") is not False:
        raise ProductionRuntimeError("approved production freeze has not been finalized")
    if metadata.get("scheduler_modified") is not False:
        raise ProductionRuntimeError("approved production freeze scheduler flag is not clean")

    manifest = _validate_freeze_manifest(root, freeze_root)
    rollback = _validate_freeze_rollback(freeze_root)
    return {
        "mode": "approved_frozen_production",
        "branch": current["branch"],
        "head": current["head"],
        "origin_main": current["origin_main"],
        "dirty": current["dirty"],
        "freeze_snapshot": freeze_root.name,
        "freeze_head": metadata["production_head"],
        "deployment_manifest": manifest,
        "rollback_baseline": rollback,
        "dirty_root_allowed": True,
    }


def _worktree_registered(root: Path, worktree: Path) -> bool:
    expected = str(worktree.resolve()).replace("\\", "/").rstrip("/").lower()
    listing = git_checked(root, "worktree", "list", "--porcelain")
    for line in listing.splitlines():
        if line.startswith("worktree ") and line[9:].replace("\\", "/").rstrip("/").lower() == expected:
            return True
    return False


def _remove_task_worktree(root: Path, worktree: Path) -> None:
    root = root.resolve()
    worktree = worktree.resolve()
    if worktree == root:
        raise ProductionRuntimeError("refusing to remove the production root")
    if not _worktree_registered(root, worktree) and not worktree.exists():
        return
    result = git_run(root, "worktree", "remove", "--force", str(worktree))
    if result.returncode and _worktree_registered(root, worktree):
        raise ProductionRuntimeError("temporary task worktree cleanup failed")


def _remove_task_directory(parent: Path) -> str:
    """Best-effort removal of generated temp files without failing a task.

    On Windows, Codex child processes can briefly retain a handle after the
    Git worktree registration has already been removed.  The production safety
    invariant is the repository checkout, not successful deletion of a
    disposable temp directory.  Read-only/locked cleanup is therefore left
    for the next maintenance pass and never turns a successful task into a
    false task failure.
    """

    if not parent.exists():
        return ""

    def onerror(function: Any, path: str, _exc_info: Any) -> None:
        try:
            os.chmod(path, stat.S_IWRITE)
            function(path)
        except OSError:
            raise

    try:
        shutil.rmtree(parent, onerror=onerror)
    except OSError as exc:
        return _sanitize_error(exc)
    return ""


@contextmanager
def isolated_task_worktree(
    root: Path,
    task_id: str,
    *,
    base_ref: str = PRODUCTION_REF,
    branch_name: str = "",
) -> Iterator[Path]:
    """Run one structured task in an isolated worktree without touching production."""

    root = Path(root).resolve()
    before = assert_production_ready(root)
    if base_ref != PRODUCTION_REF and not GIT_SHA_RE.fullmatch(str(base_ref)):
        raise ProductionRuntimeError("isolated worktree base must be origin/main or a full commit SHA")
    if branch_name:
        if branch_name.lower() in {"main", "master"}:
            raise ProductionRuntimeError("isolated task branch may not be a default branch")
        try:
            git_checked(root, "check-ref-format", "--branch", branch_name)
        except Exception as exc:
            raise ProductionRuntimeError("isolated task branch is invalid") from exc
    try:
        base_sha = git_checked(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    except Exception as exc:
        raise ProductionRuntimeError("isolated worktree base commit is unavailable") from exc
    if base_ref != PRODUCTION_REF and base_sha.lower() != str(base_ref).lower():
        raise ProductionRuntimeError("isolated worktree base does not match the validated commit SHA")
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(task_id))[:40] or "task"
    parent = Path(tempfile.mkdtemp(prefix=f"alphaengine-codex-{safe_id}-"))
    worktree = parent / "checkout"
    created = False
    try:
        git_checked(root, "worktree", "add", "--detach", str(worktree), base_ref)
        created = True
        if branch_name:
            local_ref = f"refs/heads/{branch_name}"
            local_branch = git_run(root, "show-ref", "--verify", "--quiet", local_ref)
            if local_branch.returncode not in {0, 1}:
                raise ProductionRuntimeError("could not inspect the isolated task branch")
            if local_branch.returncode == 0:
                local_sha = git_checked(root, "rev-parse", local_ref)
                if local_sha.lower() != base_sha.lower():
                    raise ProductionRuntimeError("existing task branch does not match the validated PR head")
                git_checked(worktree, "switch", branch_name)
            else:
                git_checked(worktree, "switch", "-c", branch_name, base_sha)
            attached_branch = git_checked(worktree, "branch", "--show-current")
            attached_sha = git_checked(worktree, "rev-parse", "HEAD")
            if attached_branch != branch_name or attached_sha.lower() != base_sha.lower():
                raise ProductionRuntimeError("isolated worktree did not attach to the validated PR head")
        yield worktree
    finally:
        cleanup_error: Exception | None = None
        worktree_removed = not created
        try:
            if created:
                _remove_task_worktree(root, worktree)
                worktree_removed = True
        except Exception as exc:  # pragma: no cover - platform cleanup failure
            cleanup_error = exc
        if worktree_removed:
            _remove_task_directory(parent)
        after = production_snapshot(root)
        try:
            assert_snapshot_unchanged(before, after)
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is not None:
            if isinstance(cleanup_error, ProductionRuntimeError):
                raise cleanup_error
            raise ProductionRuntimeError(_sanitize_error(cleanup_error)) from cleanup_error


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _contains_root(value: Any, root: Path) -> bool:
    token = str(root.resolve()).replace("\\", "/").rstrip("/").lower()
    text = str(value or "").replace("\\", "/").lower()
    return bool(token) and token in text


def _powershell_scheduler_probe() -> dict[str, Any]:
    if os.name != "nt":
        return {"status": "not_applicable", "tasks": [], "watcher_processes": []}
    script = r"""
$tasks = @(Get-ScheduledTask | Where-Object { $_.TaskName -like 'AlphaEngine*' -or $_.TaskPath -match '研枢' })
$task_rows = @($tasks | ForEach-Object {
  $task = $_
  $actions = @($task.Actions | ForEach-Object {
    [pscustomobject]@{
      execute = [string]$_.Execute
      arguments = [string]$_.Arguments
      working_directory = [string]$_.WorkingDirectory
    }
  })
  [pscustomobject]@{
    task_name = [string]$task.TaskName
    enabled = [bool]$task.Settings.Enabled
    actions = $actions
  }
})
$process_rows = @(Get-CimInstance Win32_Process | Where-Object { [string]$_.CommandLine -match 'github_handoff_watcher\.py' } | ForEach-Object {
  [pscustomobject]@{ command_line = [string]$_.CommandLine }
})
[pscustomobject]@{ status = 'succeeded'; tasks = $task_rows; watcher_processes = $process_rows } | ConvertTo-Json -Depth 6 -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
    )
    if result.returncode:
        return {"status": "failed", "tasks": [], "watcher_processes": []}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "failed", "tasks": [], "watcher_processes": []}
    return payload if isinstance(payload, dict) else {"status": "failed", "tasks": [], "watcher_processes": []}


def evaluate_runtime_references(root: Path, probe: dict[str, Any]) -> dict[str, Any]:
    """Evaluate scheduler/process references while keeping paths private."""

    tasks = [item for item in _as_list(probe.get("tasks")) if isinstance(item, dict)]
    project_tasks = [
        item for item in tasks if str(item.get("task_name") or "").lower().startswith(PROJECT_TASK_PREFIX.lower())
    ]
    task_matches: list[bool] = []
    watcher_task_match = False
    for task in project_tasks:
        actions = [item for item in _as_list(task.get("actions")) if isinstance(item, dict)]
        references_root = bool(actions) and all(
            _contains_root(
                " ".join(str(action.get(field) or "") for field in ("execute", "arguments", "working_directory")),
                root,
            )
            for action in actions
        )
        task_matches.append(references_root)
        if "watcher" in str(task.get("task_name") or "").lower():
            watcher_task_match = references_root
    process_matches = [
        _contains_root(item.get("command_line"), root)
        for item in _as_list(probe.get("watcher_processes"))
        if isinstance(item, dict)
    ]
    return {
        "scheduler_probe_status": str(probe.get("status") or "failed"),
        "scheduler_task_count": len(project_tasks),
        "scheduler_paths_match": bool(project_tasks) and all(task_matches),
        "watcher_task_root_match": watcher_task_match,
        "watcher_process_root_match": bool(process_matches) and all(process_matches),
        "watcher_root_match": watcher_task_match and bool(process_matches) and all(process_matches),
    }


def build_health_report(root: Path, *, scheduler_probe: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    snapshot = production_snapshot(root)
    references = evaluate_runtime_references(root, (scheduler_probe or _powershell_scheduler_probe)())
    errors: list[str] = []
    if snapshot["dirty"]:
        errors.append("production root is not clean")
    if snapshot["branch"].lower() != PRODUCTION_BRANCH:
        errors.append("production branch is not main")
    if snapshot["head"].lower() != snapshot["origin_main"].lower():
        errors.append("production HEAD does not match origin/main")
    if not references["scheduler_paths_match"]:
        errors.append("scheduler paths do not match production root")
    if not references["watcher_root_match"]:
        errors.append("watcher does not match production root")
    return {
        "production_branch": snapshot["branch"],
        "production_head": snapshot["head"],
        "origin_main": snapshot["origin_main"],
        "production_clean": not snapshot["dirty"],
        **references,
        "ok": not errors,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check or safely align the AlphaEngine production checkout")
    parser.add_argument("--root", default=".")
    parser.add_argument("--align-main", action="store_true", help="switch clean root to main and pull --ff-only")
    args = parser.parse_args()
    try:
        if args.align_main:
            snapshot = safe_align_production_root(Path(args.root))
            payload = {
                "status": "succeeded",
                "production_branch": snapshot["branch"],
                "production_head": snapshot["head"],
                "origin_main": snapshot["origin_main"],
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        payload = build_health_report(Path(args.root))
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if payload["ok"] else 1
    except (ProductionRuntimeError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": _sanitize_error(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
