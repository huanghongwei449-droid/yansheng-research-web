#!/usr/bin/env python3
"""Versioned Daily Bus transport and deterministic canonical promotion."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from daily_research_bus import (
    BusError as LegacyBusError,
    assert_bus_safe,
    build_research_input,
    bus_worktree,
    fetch_bus,
    git_checked,
    git_run,
    normalize_date,
    now,
    push_with_retry,
    repo_root,
)
from transport_observability import (
    EXIT_BRANCH_WORKTREE_CONFLICT,
    EXIT_COLLECT_FAILURE,
    EXIT_COMMIT_FAILURE,
    EXIT_INTERNAL_FAILURE,
    EXIT_LOCAL_VALIDATION_FAILURE,
    EXIT_PUSH_FAILURE,
    EXIT_REMOTE_CONTENT_VALIDATION_FAILURE,
    EXIT_REMOTE_FETCH_FAILURE,
    EXIT_REMOTE_LINEAGE_CONFLICT,
    EXIT_REMOTE_READBACK_FAILURE,
    STAGE_COMMIT,
    STAGE_DAILY_BUS_FETCH,
    STAGE_FILE_COPY_STAGE,
    STAGE_LOCAL_ARTIFACT_GENERATED,
    STAGE_LOCAL_ARTIFACT_VALIDATION,
    STAGE_PUSH,
    STAGE_REMOTE_DIVERGENCE_CHECK,
    STAGE_REMOTE_FETCH_READBACK,
    STAGE_REMOTE_SHA_CONTENT_VALIDATION,
    STAGE_WORKTREE_BRANCH_SYNC,
    TRANSPORT_STAGES,
    TransportTrace,
    sanitize_error,
)
from trading_context_v1 import DEFAULT_CONTEXT_RELATIVE


BUS_BRANCH = "daily-bus"
PROTOCOL_VERSION = "bus/v1"
CANONICAL_SCHEMA = "daily-canonical-v1"
CANONICAL_MANIFEST_SCHEMA = "daily-canonical-manifest-v1"
CANONICAL_PROJECTION_VERSION = "daily-canonical-public-projection-v1"
INPUT_MANIFEST_SCHEMA = "daily-bus-v1-input-manifest"
DECISION_MANIFEST_SCHEMA = "daily-bus-v1-decision-manifest"
SUPPORTED_SESSIONS = ("premarket", "close")
SUPPORTED_MESSAGE_TYPES = ("research-input", "decision", "canonical", "delivery-receipt")
BUS_V1_ROOT = Path("bus") / "v1"
LEGACY_ROOT = Path("daily-bus")
PRIVATE_PRODUCERS = {"local-collector", "ChatGPT", "Codex", "migration"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BusV1Error(RuntimeError):
    """A fail-closed v1 transport or promotion error."""

    def __init__(
        self,
        message: str,
        *,
        stage: str = "",
        exit_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = str(stage or "")
        self.exit_code = int(exit_code) if exit_code is not None else None
        self.details = details if isinstance(details, dict) else {}


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_json_bytes(payload: Any) -> bytes:
    """Return the single verified byte representation used for payload hashes.

    The existing bus-v1 implementation deliberately uses readable, sorted
    JSON for all persisted objects.  The newline is part of that established
    byte contract and is therefore retained rather than silently changing
    historical hashes during the handoff.
    """
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def payload_hash(payload: Any) -> str:
    """Return SHA-256 over :func:`canonical_json_bytes(payload)`."""
    return sha256_bytes(canonical_json_bytes(payload))


def json_bytes(payload: Any) -> bytes:
    """Backward-compatible name for the bus-v1 canonical JSON bytes."""
    return canonical_json_bytes(payload)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:10]}")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _timestamp(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise BusV1Error("created_at is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BusV1Error("created_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise BusV1Error("created_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def session_root(date: str, session: str) -> Path:
    _, dashed = normalize_date(date)
    if session not in SUPPORTED_SESSIONS:
        raise BusV1Error(f"unsupported session: {session}")
    return BUS_V1_ROOT / dashed / session


def relative_path(date: str, session: str, kind: str) -> Path:
    root = session_root(date, session)
    paths = {
        "input": root / "input" / "research-input.json",
        "input_manifest": root / "input" / "manifest.json",
        "decision": root / "decision" / "decision.json",
        "decision_manifest": root / "decision" / "manifest.json",
        "canonical": root / "canonical" / "canonical.json",
        "canonical_sha": root / "canonical" / "canonical.sha256",
        "canonical_manifest": root / "canonical" / "manifest.json",
        "ima_latest": root / "delivery" / "ima" / "latest.json",
    }
    try:
        return paths[kind]
    except KeyError as exc:
        raise BusV1Error(f"unsupported bus object kind: {kind}") from exc


def legacy_relative_path(date: str, name: str) -> Path:
    _, dashed = normalize_date(date)
    allowed = {"research-input.json", "chatgpt-decision.json"}
    if name not in allowed:
        raise BusV1Error(f"unsupported legacy object: {name}")
    return LEGACY_ROOT / dashed / name


def make_envelope(
    payload: dict[str, Any],
    *,
    date: str,
    session: str,
    message_type: str,
    producer: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    _, dashed = normalize_date(date)
    if session not in SUPPORTED_SESSIONS:
        raise BusV1Error(f"unsupported session: {session}")
    if message_type not in SUPPORTED_MESSAGE_TYPES:
        raise BusV1Error(f"unsupported message_type: {message_type}")
    if not isinstance(payload, dict):
        raise BusV1Error("envelope payload must be an object")
    if not producer.strip():
        raise BusV1Error("producer is required")
    assert_bus_safe(payload)
    created = _timestamp(created_at or now())
    return {
        "protocol_version": PROTOCOL_VERSION,
        "trading_date": dashed,
        "session": session,
        "message_type": message_type,
        "producer": producer,
        "created_at": created,
        "payload_hash": payload_hash(payload),
        "payload": payload,
    }


def validate_envelope(
    envelope: dict[str, Any],
    *,
    date: str,
    session: str,
    message_type: str,
) -> dict[str, Any]:
    _, dashed = normalize_date(date)
    if not isinstance(envelope, dict):
        raise BusV1Error("message envelope must be an object")
    if envelope.get("protocol_version") != PROTOCOL_VERSION:
        raise BusV1Error("unsupported protocol_version; expected bus/v1")
    if envelope.get("trading_date") != dashed:
        raise BusV1Error("envelope trading_date mismatch")
    if envelope.get("session") != session:
        raise BusV1Error("envelope session mismatch")
    if envelope.get("message_type") != message_type:
        raise BusV1Error("envelope message_type mismatch")
    producer = str(envelope.get("producer") or "").strip()
    if not producer:
        raise BusV1Error("envelope producer is required")
    _timestamp(str(envelope.get("created_at") or ""))
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise BusV1Error("envelope payload must be an object")
    declared_payload_hash = str(envelope.get("payload_hash") or "")
    if not SHA256_RE.fullmatch(declared_payload_hash):
        raise BusV1Error("envelope payload_hash is invalid")
    if declared_payload_hash != payload_hash(payload):
        raise BusV1Error("envelope payload_hash does not match payload")
    assert_bus_safe(payload)
    return payload


def _git_object(root: Path, branch: str, path: Path) -> tuple[bytes | None, str]:
    root = repo_root(root.resolve())
    if not fetch_bus(root, branch):
        return None, ""
    remote_ref = f"origin/{branch}:{str(path).replace(chr(92), '/')}"
    content = _git_blob(root, remote_ref)
    if content is None:
        return None, ""
    return content, sha256_bytes(content)


def _git_blob(root: Path, object_ref: str) -> bytes | None:
    """Read a git blob as bytes; text-mode git output cannot prove byte identity."""

    try:
        result = subprocess.run(
            ["git", "show", object_ref],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise BusV1Error(
            f"git blob read failed: {sanitize_error(exc)}",
            stage=STAGE_REMOTE_FETCH_READBACK,
            exit_code=EXIT_REMOTE_READBACK_FAILURE,
        ) from exc
    if result.returncode:
        return None
    return bytes(result.stdout)


def _attach_transport_error(
    error: BaseException,
    trace: TransportTrace,
    *,
    stage: str,
    exit_code: int,
) -> BusV1Error:
    if isinstance(error, BusV1Error):
        if not error.stage:
            error.stage = trace.failed_stage() or stage
        if error.exit_code is None:
            for record in reversed(trace.records):
                if record.get("stage") == error.stage and record.get("status") == "FAIL" and record.get("exit_code") is not None:
                    error.exit_code = int(record["exit_code"])
                    break
            if error.exit_code is None:
                error.exit_code = exit_code
        details = dict(error.details)
        details["transport"] = trace.to_dict()
        error.details = details
        return error
    resolved_stage = trace.failed_stage() or stage
    resolved_exit_code = exit_code
    for record in reversed(trace.records):
        if record.get("stage") == resolved_stage and record.get("status") == "FAIL" and record.get("exit_code") is not None:
            resolved_exit_code = int(record["exit_code"])
            break
    return BusV1Error(
        f"{resolved_stage} failed: {sanitize_error(error)}",
        stage=resolved_stage,
        exit_code=resolved_exit_code,
        details={"transport": trace.to_dict()},
    )


def _run_transport_stage(
    trace: TransportTrace,
    stage: str,
    failure_exit_code: int,
    operation: Callable[[], Any],
    *,
    details: Any = None,
) -> Any:
    try:
        return trace.run(stage, operation, failure_exit_code=failure_exit_code, details=details)
    except Exception as exc:
        error = _attach_transport_error(exc, trace, stage=stage, exit_code=failure_exit_code)
        if error is exc:
            raise
        raise error from exc


def _ensure_trace_failure(
    trace: TransportTrace,
    error: BaseException,
    *,
    default_stage: str,
    default_exit_code: int,
) -> None:
    if trace.failed_stage():
        return
    stage = str(getattr(error, "stage", "") or default_stage)
    if stage not in TRANSPORT_STAGES:
        stage = default_stage
    index = trace.start(stage)
    trace.finish(
        index,
        status="FAIL",
        exit_code=int(getattr(error, "exit_code", default_exit_code) or default_exit_code),
        error_summary=error,
    )


def _publish_files(
    root: Path,
    branch: str,
    files: dict[Path, bytes],
    message: str,
    *,
    replace_paths: set[Path] | None = None,
    push_runner: Callable[..., subprocess.CompletedProcess[str]] = git_run,
    sleeper: Callable[[float], None] | None = None,
    trace: TransportTrace | None = None,
    verify_remote: bool = True,
) -> dict[str, Any]:
    if not files:
        raise BusV1Error("no bus files to publish")
    trace = trace or TransportTrace()
    publish_status: dict[str, Any] = {}
    replace_paths = replace_paths or set()
    root = root.resolve()
    branch_exists = False
    changed_paths: list[str] = []
    remote_commit = ""
    remote_same_paths: set[Path] = set()

    def fetch_current_bus() -> bool:
        nonlocal root
        root = repo_root(root)
        return fetch_bus(root, branch)

    try:
        branch_exists = bool(
            _run_transport_stage(
                trace,
                STAGE_DAILY_BUS_FETCH,
                EXIT_REMOTE_FETCH_FAILURE,
                fetch_current_bus,
                details={"branch": branch},
            )
        )

        def check_divergence() -> dict[str, Any]:
            conflicts: list[str] = []
            remote_same_paths.clear()
            if branch_exists:
                for relative, content in files.items():
                    object_ref = f"origin/{branch}:{str(relative).replace(chr(92), '/') }"
                    existing = _git_blob(root, object_ref)
                    if existing is not None and existing == content:
                        remote_same_paths.add(relative)
                    if existing is not None and existing != content and relative not in replace_paths:
                        conflicts.append(str(relative).replace("\\", "/"))
            if conflicts:
                raise BusV1Error(
                    "REMOTE_LINEAGE_CONFLICT: refusing to overwrite existing bus paths: " + ", ".join(conflicts),
                    stage=STAGE_REMOTE_DIVERGENCE_CHECK,
                    exit_code=EXIT_REMOTE_LINEAGE_CONFLICT,
                )
            return {"branch_exists": branch_exists, "conflicts": conflicts}

        worktree_index = trace.start(STAGE_WORKTREE_BRANCH_SYNC)
        try:
            with bus_worktree(root, branch, branch_exists=branch_exists) as worktree:
                trace.finish(
                    worktree_index,
                    status="PASS",
                    exit_code=0,
                    details={"branch": branch, "branch_exists": branch_exists, "base": f"origin/{branch}" if branch_exists else "HEAD"},
                )
                divergence = _run_transport_stage(
                    trace,
                    STAGE_REMOTE_DIVERGENCE_CHECK,
                    EXIT_REMOTE_LINEAGE_CONFLICT,
                    check_divergence,
                )

                def copy_and_stage() -> dict[str, Any]:
                    changed: list[str] = []
                    for relative, content in files.items():
                        target = worktree / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():
                            existing = target.read_bytes()
                            if existing != content and relative not in replace_paths:
                                if relative in remote_same_paths:
                                    continue
                                raise BusV1Error(
                                    "REMOTE_LINEAGE_CONFLICT: bus object conflict; refusing overwrite: "
                                    + relative.as_posix(),
                                    stage=STAGE_REMOTE_DIVERGENCE_CHECK,
                                    exit_code=EXIT_REMOTE_LINEAGE_CONFLICT,
                                )
                            if existing == content:
                                continue
                        target.write_bytes(content)
                        changed.append(str(relative).replace("\\", "/"))
                    if changed:
                        git_checked(worktree, "add", "--", *changed)
                        staged = git_run(worktree, "diff", "--cached", "--quiet")
                        if staged.returncode not in {0, 1}:
                            raise BusV1Error(
                                f"git diff --cached failed ({staged.returncode})",
                                stage=STAGE_FILE_COPY_STAGE,
                                exit_code=EXIT_COMMIT_FAILURE,
                            )
                        if staged.returncode == 0:
                            changed = []
                    return {"changed_paths": changed}

                copy_result = _run_transport_stage(
                    trace,
                    STAGE_FILE_COPY_STAGE,
                    EXIT_LOCAL_VALIDATION_FAILURE,
                    copy_and_stage,
                )
                changed_paths = list(copy_result["changed_paths"])
                if not changed_paths:
                    trace.skip(STAGE_COMMIT, reason="remote already contains identical bytes")
                    trace.skip(STAGE_PUSH, reason="remote already contains identical bytes")
                else:
                    _run_transport_stage(
                        trace,
                        STAGE_COMMIT,
                        EXIT_COMMIT_FAILURE,
                        lambda: git_checked(worktree, "commit", "-m", message),
                    )

                    def sync_stale_branch() -> dict[str, Any]:
                        latest_exists = fetch_bus(root, branch)
                        if not latest_exists:
                            raise BusV1Error(
                                "remote bus branch disappeared during safe stale-branch retry",
                                stage=STAGE_WORKTREE_BRANCH_SYNC,
                                exit_code=EXIT_BRANCH_WORKTREE_CONFLICT,
                            )
                        conflicts: list[str] = []
                        for relative, content in files.items():
                            object_ref = f"origin/{branch}:{str(relative).replace(chr(92), '/') }"
                            existing = _git_blob(root, object_ref)
                            if existing is not None and existing != content and relative not in replace_paths:
                                conflicts.append(str(relative).replace("\\", "/"))
                        if conflicts:
                            raise BusV1Error(
                                "REMOTE_LINEAGE_CONFLICT during stale-branch retry: " + ", ".join(conflicts),
                                stage=STAGE_REMOTE_DIVERGENCE_CHECK,
                                exit_code=EXIT_REMOTE_LINEAGE_CONFLICT,
                            )
                        rebased = git_run(worktree, "rebase", f"origin/{branch}")
                        if rebased.returncode:
                            git_run(worktree, "rebase", "--abort")
                            raise BusV1Error(
                                "safe stale-branch rebase failed: " + sanitize_error(rebased.stderr or rebased.stdout),
                                stage=STAGE_WORKTREE_BRANCH_SYNC,
                                exit_code=EXIT_BRANCH_WORKTREE_CONFLICT,
                            )
                        return {"status": "succeeded", "remote_commit": git_run(root, "rev-parse", f"origin/{branch}").stdout.strip()}

                    retry_kwargs: dict[str, Any] = {
                        "publish_status": publish_status,
                        "runner": push_runner,
                        "stale_sync": sync_stale_branch,
                    }
                    if sleeper is not None:
                        retry_kwargs["sleeper"] = sleeper
                    _run_transport_stage(
                        trace,
                        STAGE_PUSH,
                        EXIT_PUSH_FAILURE,
                        lambda: push_with_retry(worktree, branch, **retry_kwargs),
                        details=publish_status,
                    )

                if not verify_remote:
                    trace.skip(STAGE_REMOTE_FETCH_READBACK, reason="remote verification explicitly disabled")
                    trace.skip(STAGE_REMOTE_SHA_CONTENT_VALIDATION, reason="remote verification explicitly disabled")
                else:
                    def fetch_remote_for_readback() -> dict[str, Any]:
                        if not fetch_bus(root, branch):
                            raise BusV1Error(
                                "remote bus branch is missing after publish",
                                stage=STAGE_REMOTE_FETCH_READBACK,
                                exit_code=EXIT_REMOTE_READBACK_FAILURE,
                            )
                        ref_result = git_run(root, "rev-parse", f"origin/{branch}")
                        if ref_result.returncode:
                            raise BusV1Error(
                                "remote bus ref cannot be resolved after publish",
                                stage=STAGE_REMOTE_FETCH_READBACK,
                                exit_code=EXIT_REMOTE_READBACK_FAILURE,
                            )
                        return {"remote_commit": ref_result.stdout.strip()}

                    readback = _run_transport_stage(
                        trace,
                        STAGE_REMOTE_FETCH_READBACK,
                        EXIT_REMOTE_READBACK_FAILURE,
                        fetch_remote_for_readback,
                    )
                    remote_commit = str(readback.get("remote_commit") or "")

                    def validate_remote_content() -> dict[str, Any]:
                        observed: dict[str, str] = {}
                        for relative, expected in files.items():
                            object_ref = f"origin/{branch}:{str(relative).replace(chr(92), '/') }"
                            actual = _git_blob(root, object_ref)
                            if actual is None:
                                raise BusV1Error(
                                    "remote readback missing published path: " + str(relative).replace("\\", "/"),
                                    stage=STAGE_REMOTE_SHA_CONTENT_VALIDATION,
                                    exit_code=EXIT_REMOTE_CONTENT_VALIDATION_FAILURE,
                                )
                            expected_sha = sha256_bytes(expected)
                            actual_sha = sha256_bytes(actual)
                            observed[str(relative).replace("\\", "/")] = actual_sha
                            if actual != expected:
                                raise BusV1Error(
                                    "remote readback SHA/content mismatch for "
                                    + str(relative).replace("\\", "/")
                                    + f" (expected {expected_sha}, actual {actual_sha})",
                                    stage=STAGE_REMOTE_CONTENT_VALIDATION,
                                    exit_code=EXIT_REMOTE_CONTENT_VALIDATION_FAILURE,
                                )
                        return {"files": observed}

                    readback_content = _run_transport_stage(
                        trace,
                        STAGE_REMOTE_SHA_CONTENT_VALIDATION,
                        EXIT_REMOTE_CONTENT_VALIDATION_FAILURE,
                        validate_remote_content,
                    )
                    remote_files = readback_content.get("files", {})

                remote_publish_status = (
                    ("REMOTE_PUBLISH_PASS" if changed_paths else "REMOTE_PUBLISH_IDEMPOTENT")
                    if verify_remote
                    else "REMOTE_PUBLISH_UNVERIFIED"
                )
                trace.skip_unvisited(reason="not applicable for this publish path")
                return {
                    "ok": verify_remote,
                    "changed": bool(changed_paths),
                    "paths": changed_paths or [str(item).replace("\\", "/") for item in files],
                    "publish_status": publish_status,
                    "remote_publish_status": remote_publish_status,
                    "remote_commit": remote_commit,
                    "remote_files": remote_files if verify_remote else {},
                    "transport": trace.to_dict(),
                    "divergence": divergence,
                }
        except Exception as exc:
            if trace.records and trace.records[-1].get("stage") == STAGE_WORKTREE_BRANCH_SYNC and trace.records[-1].get("status") == "RUNNING":
                trace.finish(
                    len(trace.records) - 1,
                    status="FAIL",
                    exit_code=int(getattr(exc, "exit_code", EXIT_BRANCH_WORKTREE_CONFLICT) or EXIT_BRANCH_WORKTREE_CONFLICT),
                    error_summary=exc,
                )
            raise
    except Exception as exc:
        _ensure_trace_failure(
            trace,
            exc,
            default_stage=STAGE_DAILY_BUS_FETCH,
            default_exit_code=EXIT_INTERNAL_FAILURE,
        )
        trace.skip_unvisited(reason="not reached because an earlier transport stage failed")
        error = _attach_transport_error(
            exc,
            trace,
            stage=str(getattr(exc, "stage", "") or STAGE_DAILY_BUS_FETCH),
            exit_code=int(getattr(exc, "exit_code", EXIT_INTERNAL_FAILURE) or EXIT_INTERNAL_FAILURE),
        )
        if error is exc:
            raise
        raise error from exc


def _call_publisher(
    publisher: Callable[..., dict[str, Any]],
    root: Path,
    branch: str,
    files: dict[Path, bytes],
    message: str,
    *,
    trace: TransportTrace,
    replace_paths: set[Path] | None = None,
) -> dict[str, Any]:
    """Pass the trace to the production publisher while keeping test hooks compatible."""

    if publisher is _publish_files:
        kwargs: dict[str, Any] = {"trace": trace}
        if replace_paths:
            kwargs["replace_paths"] = replace_paths
        return publisher(root, branch, files, message, **kwargs)
    return publisher(root, branch, files, message)


def _canonical_metadata_refreeze_allowed(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Allow only a non-rendered NextCheck contract migration on same content.

    A canonical hash covers the customer title/body.  A formal contract patch
    may change only the source decision lineage and the machine-readable
    next_checks projection while leaving that rendered content byte-for-byte
    identical.  Any other difference remains fail-closed.
    """

    # A legacy canonical is immutable read-only compatibility data.  It must
    # never be upgraded in place merely because a later decision contains the
    # new structured projection; this protects historical frozen artifacts.
    if (
        existing.get("canonical_projection_version") != CANONICAL_PROJECTION_VERSION
        or candidate.get("canonical_projection_version") != CANONICAL_PROJECTION_VERSION
    ):
        return False
    if existing.get("next_checks") == candidate.get("next_checks"):
        return False
    existing_copy = copy.deepcopy(existing)
    candidate_copy = copy.deepcopy(candidate)
    # These are deterministic mirrors of the top-level next_checks field.
    # Normalize them only for the historical contract-migration exception.
    for container in ("opportunities", "public_projection"):
        existing_nested = existing_copy.get(container)
        candidate_nested = candidate_copy.get(container)
        if not isinstance(existing_nested, dict) or not isinstance(candidate_nested, dict):
            return False
        candidate_nested["next_checks"] = copy.deepcopy(existing_nested.get("next_checks"))
    candidate_copy["next_checks"] = copy.deepcopy(existing_copy["next_checks"])
    existing_copy.pop("source_decision_hash", None)
    candidate_copy.pop("source_decision_hash", None)
    # Direct unit fixtures may predate the optional in-canonical source path
    # fields; the canonical manifest remains the authoritative path lineage.
    for key in ("source_decision_path", "source_input_path"):
        existing_copy.pop(key, None)
        candidate_copy.pop(key, None)
    return existing_copy == candidate_copy


def _write_local_object(root: Path, date: str, session: str, kind: str, content: bytes) -> Path:
    _, dashed = normalize_date(date)
    path = root / f"tmp_recap_{dashed.replace('-', '')}" / "bus-v1" / session / kind
    path.mkdir(parents=True, exist_ok=True)
    target = path / ("research-input.json" if kind == "input" else "decision.json")
    _atomic_write(target, content)
    return target


def _parse_json(content: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusV1Error(f"{description} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise BusV1Error(f"{description} must be a JSON object")
    return value


def _legacy_input_envelope(
    content: bytes,
    *,
    date: str,
    session: str,
    source_hash: str,
    source_path: str | None = None,
) -> dict[str, Any]:
    payload = _parse_json(content, "legacy research input")
    _, dashed = normalize_date(date)
    if payload.get("date") != dashed:
        raise BusV1Error("legacy research input date mismatch")
    context = payload.get("time_context") if isinstance(payload.get("time_context"), dict) else {}
    if context.get("session") != session or context.get("target_date") != dashed:
        raise BusV1Error("cannot determine legacy research input session without an exact match")
    envelope = make_envelope(
        payload,
        date=dashed,
        session=session,
        message_type="research-input",
        producer="migration",
        created_at=str(payload.get("updated_at") or now()),
    )
    envelope["source_path"] = source_path or str(legacy_relative_path(dashed, "research-input.json")).replace("\\", "/")
    envelope["source_hash"] = source_hash
    return envelope


def _legacy_decision_envelope(content: bytes, *, date: str, session: str, source_hash: str) -> dict[str, Any]:
    payload = _parse_json(content, "legacy decision")
    _, dashed = normalize_date(date)
    if payload.get("date") != dashed:
        raise BusV1Error("legacy decision date mismatch")
    stage = payload.get(session)
    if not isinstance(stage, dict):
        raise BusV1Error("cannot determine legacy decision session without an exact stage")
    allowed_stage_fields = ("status", "title", "text", "evidence_refs", "unknowns", "next_checks")
    projected_stage = {key: stage[key] for key in allowed_stage_fields if key in stage}
    selected: dict[str, Any] = {
        "schema_version": payload.get("schema_version", "daily-chatgpt-decision-v1"),
        "pipeline_version": payload.get("pipeline_version", "daily-pipeline-v1"),
        "date": dashed,
        "run_id": payload.get("run_id", ""),
        "updated_at": payload.get("updated_at") or now(),
        "public_safe_for_private_bus": payload.get("public_safe_for_private_bus") is True,
        "not_for_public_feed": payload.get("not_for_public_feed") is True,
        "role_boundary": payload.get("role_boundary"),
        session: projected_stage,
    }
    envelope = make_envelope(
        selected,
        date=dashed,
        session=session,
        message_type="decision",
        producer="migration",
        created_at=str(selected["updated_at"]),
    )
    envelope["source_path"] = str(legacy_relative_path(dashed, "chatgpt-decision.json")).replace("\\", "/")
    envelope["source_hash"] = source_hash
    return envelope


def read_bus_envelope(
    root: Path,
    date: str,
    session: str,
    message_type: str,
    *,
    branch: str = BUS_BRANCH,
) -> tuple[dict[str, Any] | None, str, str]:
    path_kind = "input" if message_type == "research-input" else "decision"
    path = relative_path(date, session, path_kind)
    content, source_hash = _git_object(root, branch, path)
    if content is not None:
        envelope = _parse_json(content, f"bus/v1 {message_type}")
        validate_envelope(envelope, date=date, session=session, message_type=message_type)
        return envelope, str(path).replace("\\", "/"), source_hash

    legacy_name = "research-input.json" if message_type == "research-input" else "chatgpt-decision.json"
    legacy_path = legacy_relative_path(date, legacy_name)
    legacy_content, legacy_hash = _git_object(root, branch, legacy_path)
    if legacy_content is None:
        return None, "", ""
    if message_type == "research-input":
        envelope = _legacy_input_envelope(legacy_content, date=date, session=session, source_hash=legacy_hash)
    else:
        envelope = _legacy_decision_envelope(legacy_content, date=date, session=session, source_hash=legacy_hash)
    validate_envelope(envelope, date=date, session=session, message_type=message_type)
    return envelope, str(legacy_path).replace("\\", "/"), legacy_hash


def _validate_decision_payload(payload: dict[str, Any], date: str, session: str) -> tuple[str, dict[str, Any]]:
    _, dashed = normalize_date(date)
    if payload.get("date") != dashed:
        raise BusV1Error("decision date mismatch")
    schema = str(payload.get("schema_version") or "")
    if schema not in {"daily-chatgpt-decision-v1", "daily-decision-v1", "daily-decision-v2"}:
        raise BusV1Error("unsupported decision schema_version")
    boundary = payload.get("role_boundary")
    expected = {
        "judgement_owner": "Codex" if schema == "daily-decision-v2" else "ChatGPT",
        "execution_owner": "Codex",
        "independent_market_view": False,
        "public_feed_excludes_private": True,
    }
    if not isinstance(boundary, dict) or any(boundary.get(key) != value for key, value in expected.items()):
        raise BusV1Error("decision role_boundary is invalid")
    stage = payload.get(session)
    if not isinstance(stage, dict):
        raise BusV1Error(f"decision missing {session} stage")
    status = str(stage.get("status") or "")
    if status == "pending":
        return "WAITING_DECISION", stage
    if status != "received":
        raise BusV1Error(f"decision {session} status is invalid")
    body = stage.get("text")
    if not isinstance(body, str) or not body.strip():
        raise BusV1Error(f"decision {session} text is missing")
    for key in ("evidence_refs", "unknowns", "next_checks"):
        if not isinstance(stage.get(key), list):
            raise BusV1Error(f"decision {session} {key} must be a list")
    return "READY", stage


def _title_from_body(body: str, date: str, session: str) -> str:
    for line in body.splitlines():
        candidate = re.sub(r"^\s*#+\s*", "", line).strip()
        if candidate:
            return candidate[:240]
    return f"{date} {session} research"


def canonical_hash_for(title: str, body: str) -> str:
    material = f"{title.strip()}\n{body.replace(chr(13), '').strip()}".encode("utf-8")
    return sha256_bytes(material)


def _projection_slot(value: Any) -> dict[str, Any]:
    """Represent an available value or an explicit upstream data gap."""

    if value is None:
        return {"status": "notUpdated", "value": None}
    return {"status": "available", "value": copy.deepcopy(value)}


def _research_engine_node() -> str:
    discovered = shutil.which("node")
    if discovered:
        return discovered
    fallback = Path(
        r"C:\Users\15707\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    )
    if fallback.is_file():
        return str(fallback)
    raise BusV1Error("canonical opportunity projector node executable is missing")


def _project_formal_opportunity_changes(
    decision: dict[str, Any],
    stage: dict[str, Any],
    session: str,
) -> list[dict[str, Any]]:
    """Call the existing Research Engine projector at canonicalization time.

    The canonical materializer owns the timing and persistence of this
    projection, while the Research Engine remains the single owner of
    opportunity vocabulary and transition semantics.  No Python mapping table
    is maintained here.
    """

    raw = decision.get("opportunity_changes")
    if raw is None:
        raw = stage.get("opportunity_changes", [])
    if not isinstance(raw, list):
        raise BusV1Error("formal opportunity_changes must be a list or null")
    script = Path(__file__).resolve().parents[5] / "yanshu" / "services" / "research-engine" / "scripts" / "project-opportunity-changes-v1.mjs"
    if not script.is_file():
        raise BusV1Error("canonical opportunity projector is missing")
    request = {"decision": {session: {"opportunity_changes": raw}}, "phase": session}
    result = subprocess.run(
        [_research_engine_node(), str(script)],
        cwd=script.parent.parent,
        input=json.dumps(request, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Research Engine opportunity projector failed").strip()[-1200:]
        raise BusV1Error(f"canonical opportunity projection failed: {detail}")
    try:
        projected = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BusV1Error("canonical opportunity projector returned invalid JSON") from exc
    if not isinstance(projected, list) or not all(isinstance(item, dict) for item in projected):
        raise BusV1Error("canonical opportunity projector returned an invalid read model")
    return copy.deepcopy(projected)


def _public_risk_budget(value: Any, session: str) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, dict):
        return {
            "current": None,
            "scale": None,
            "previous": None,
            "change": None,
            "reason": "notUpdated",
            "asOfPhase": "postclose" if session == "close" else "premarket",
        }, False
    current = value.get("current")
    maximum = value.get("max")
    previous = value.get("previous")
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in (current, maximum, previous)):
        return {
            "current": None,
            "scale": None,
            "previous": None,
            "change": None,
            "reason": "notUpdated",
            "asOfPhase": "postclose" if session == "close" else "premarket",
        }, False
    change = str(value.get("change") or "")
    change = {"maintained": "unchanged", "unchanged": "unchanged", "increased": "raised", "decreased": "lowered"}.get(change, change or None)
    return {
        "current": current,
        "scale": maximum,
        "previous": previous,
        "change": change,
        "reason": str(value.get("stance") or "").strip() or "notUpdated",
        "asOfPhase": "postclose" if session == "close" else "premarket",
    }, True


def _canonical_structured_projection(
    decision: dict[str, Any],
    stage: dict[str, Any],
    session: str,
) -> dict[str, Any]:
    conclusion = stage.get("conclusion") if isinstance(stage.get("conclusion"), dict) else {}
    projected_opportunities = _project_formal_opportunity_changes(decision, stage, session)
    market = {
        # These keys are deliberately sourced only from the formal conclusion.
        # Missing upstream fields become explicit notUpdated slots; they are
        # never inferred from report prose or a prior Feed view.
        "regime": _projection_slot(conclusion.get("market_regime")),
        "breadth": _projection_slot(conclusion.get("breadth")),
        "turnover": _projection_slot(conclusion.get("turnover")),
        "index_moves": _projection_slot(conclusion.get("index_moves")),
        "sector_leadership": _projection_slot(conclusion.get("sector_leadership")),
        "temperature": _projection_slot(conclusion.get("market_temperature")),
        "focus_directions": _projection_slot(conclusion.get("focus_directions")),
    }
    risk_budget, risk_available = _public_risk_budget(conclusion.get("risk_budget"), session)
    market_available = risk_available or any(
        slot.get("status") == "available" for slot in market.values() if isinstance(slot, dict)
    )
    public_market = {
        "state": str(conclusion.get("market_state") or "notUpdated"),
        "temperature": str(conclusion.get("market_temperature") or "notUpdated"),
        "summary": str(conclusion.get("core_pricing_conflict") or "notUpdated"),
        "riskBudget": risk_budget,
        "coverage": "available" if market_available else "notUpdated",
    }
    opportunity_buckets = {
        "upgrades": [copy.deepcopy(item) for item in projected_opportunities if item.get("transitionType") == "upgraded"],
        "downgrades": [copy.deepcopy(item) for item in projected_opportunities if item.get("transitionType") == "downgraded"],
        "maintained": [copy.deepcopy(item) for item in projected_opportunities if item.get("transitionType") == "maintained"],
        "other": [copy.deepcopy(item) for item in projected_opportunities if item.get("transitionType") not in {"upgraded", "downgraded", "maintained"}],
        "next_checks": copy.deepcopy(stage.get("next_checks", [])),
    }
    public_projection = {
        "market": copy.deepcopy(public_market),
        "opportunity_changes": copy.deepcopy(projected_opportunities),
        "next_checks": copy.deepcopy(stage.get("next_checks", [])),
        "risk_budget": copy.deepcopy(risk_budget),
    }
    knowledge = decision.get("knowledge_sedimentation")
    if knowledge is None and "knowledge_sedimentation" not in decision:
        knowledge_projection = _projection_slot(None)
    else:
        knowledge_projection = _projection_slot(knowledge)
    return {
        "canonical_projection_version": CANONICAL_PROJECTION_VERSION,
        "decision_text": str(stage["text"]).replace("\r\n", "\n").replace("\r", "\n").strip(),
        "market": market,
        "opportunities": opportunity_buckets,
        "risk_budget": _projection_slot(conclusion.get("risk_budget")),
        "knowledge_sedimentation": knowledge_projection,
        "public_market": public_market,
        "opportunity_changes": projected_opportunities,
        "public_projection": public_projection,
    }


def build_canonical(
    decision: dict[str, Any],
    *,
    decision_source_hash: str,
    input_source_hash: str,
    date: str,
    session: str,
    decision_source_path: str | None = None,
    input_source_path: str | None = None,
) -> dict[str, Any]:
    status, stage = _validate_decision_payload(decision, date, session)
    if status != "READY":
        raise BusV1Error("cannot build canonical from a waiting decision")
    body = str(stage["text"]).replace("\r\n", "\n").replace("\r", "\n").strip()
    title = str(stage.get("title") or _title_from_body(body, date, session)).strip()
    canonical_hash = canonical_hash_for(title, body)
    canonical = {
        "schema_version": CANONICAL_SCHEMA,
        "methodology_version": str(decision.get("methodology_version") or decision.get("pipeline_version") or "daily-pipeline-v1"),
        "handoff_protocol_version": PROTOCOL_VERSION,
        "canonical_hash": canonical_hash,
        "trading_date": normalize_date(date)[1],
        "session": session,
        "source_decision_hash": decision_source_hash,
        "source_input_hash": input_source_hash,
        "title": title,
        "body": body,
        "evidence_refs": stage.get("evidence_refs", []),
        "unknowns": stage.get("unknowns", []),
        "next_checks": stage.get("next_checks", []),
    }
    canonical.update(_canonical_structured_projection(decision, stage, session))
    if decision_source_path:
        canonical["source_decision_path"] = str(decision_source_path).replace("\\", "/")
    if input_source_path:
        canonical["source_input_path"] = str(input_source_path).replace("\\", "/")
    validate_canonical_completeness(canonical, date=date, session=session)
    assert_bus_safe(canonical)
    return canonical


def _require_projection_slot(value: Any, path: str) -> None:
    if not isinstance(value, dict) or value.get("status") not in {"available", "notUpdated"} or "value" not in value:
        raise BusV1Error(f"canonical completeness invalid at {path}")
    if value["status"] == "notUpdated" and value["value"] is not None:
        raise BusV1Error(f"canonical completeness invalid at {path}: notUpdated must be null")
    if value["status"] == "available" and value["value"] is None:
        raise BusV1Error(f"canonical completeness invalid at {path}: available must have a value")


def validate_canonical_completeness(canonical: dict[str, Any], *, date: str, session: str) -> dict[str, Any]:
    """Validate the future freeze-time structured public projection.

    Canonicals without this marker are historical daily-canonical-v1 objects
    and remain valid read-only compatibility artifacts.
    """

    if "canonical_projection_version" not in canonical:
        return {"status": "legacy", "date": normalize_date(date)[1], "session": session}
    if canonical.get("canonical_projection_version") != CANONICAL_PROJECTION_VERSION:
        raise BusV1Error("canonical completeness version is unsupported")
    if canonical.get("decision_text") != canonical.get("body"):
        raise BusV1Error("canonical completeness decision_text does not match body")
    market = canonical.get("market")
    if not isinstance(market, dict):
        raise BusV1Error("canonical completeness market is missing")
    for field in ("regime", "breadth", "turnover", "index_moves", "sector_leadership", "temperature", "focus_directions"):
        _require_projection_slot(market.get(field), f"market.{field}")
    opportunities = canonical.get("opportunities")
    if not isinstance(opportunities, dict):
        raise BusV1Error("canonical completeness opportunities is missing")
    for field in ("upgrades", "downgrades", "maintained", "other", "next_checks"):
        if not isinstance(opportunities.get(field), list):
            raise BusV1Error(f"canonical completeness opportunities.{field} must be a list")
    _require_projection_slot(canonical.get("risk_budget"), "risk_budget")
    _require_projection_slot(canonical.get("knowledge_sedimentation"), "knowledge_sedimentation")
    public_market = canonical.get("public_market")
    if not isinstance(public_market, dict) or public_market.get("coverage") not in {"available", "notUpdated"}:
        raise BusV1Error("canonical completeness public_market is invalid")
    if not isinstance(public_market.get("riskBudget"), dict):
        raise BusV1Error("canonical completeness public_market.riskBudget is invalid")
    if not isinstance(canonical.get("opportunity_changes"), list):
        raise BusV1Error("canonical completeness opportunity_changes must be a list")
    formal_opportunity_fields = {"id", "entityType", "entityId", "label", "transitionType", "from", "to", "reason", "nextCheck", "anchors"}
    for index, item in enumerate(canonical["opportunity_changes"]):
        if not isinstance(item, dict) or set(item) != formal_opportunity_fields:
            raise BusV1Error(f"canonical completeness opportunity_changes[{index}] is not formal")
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in ("id", "entityType", "entityId", "label", "transitionType", "to", "reason", "nextCheck")):
            raise BusV1Error(f"canonical completeness opportunity_changes[{index}] has invalid text")
        if item.get("from") is not None and (not isinstance(item.get("from"), str) or not item["from"].strip()):
            raise BusV1Error(f"canonical completeness opportunity_changes[{index}].from is invalid")
        if not isinstance(item.get("anchors"), list):
            raise BusV1Error(f"canonical completeness opportunity_changes[{index}].anchors is invalid")
    public_projection = canonical.get("public_projection")
    if not isinstance(public_projection, dict) or not isinstance(public_projection.get("market"), dict) \
        or not isinstance(public_projection.get("opportunity_changes"), list) \
        or not isinstance(public_projection.get("next_checks"), list) \
        or not isinstance(public_projection.get("risk_budget"), dict):
        raise BusV1Error("canonical completeness public_projection is invalid")
    if public_projection.get("opportunity_changes") != canonical.get("opportunity_changes"):
        raise BusV1Error("canonical completeness public opportunity projection diverges")
    if public_projection.get("next_checks") != canonical.get("next_checks"):
        raise BusV1Error("canonical completeness public next_checks diverge")
    if public_projection.get("market") != public_market:
        raise BusV1Error("canonical completeness public market projection diverges")
    if public_projection.get("risk_budget") != public_market.get("riskBudget"):
        raise BusV1Error("canonical completeness public risk projection diverges")
    return {"status": "complete", "date": normalize_date(date)[1], "session": session}


def validate_canonical(canonical: dict[str, Any], *, date: str, session: str) -> dict[str, Any]:
    _, dashed = normalize_date(date)
    required = (
        "schema_version",
        "methodology_version",
        "handoff_protocol_version",
        "canonical_hash",
        "trading_date",
        "session",
        "source_decision_hash",
        "title",
        "body",
    )
    if any(key not in canonical for key in required):
        raise BusV1Error("canonical is missing a required field")
    if canonical.get("schema_version") != CANONICAL_SCHEMA:
        raise BusV1Error("canonical schema_version is unsupported")
    if canonical.get("handoff_protocol_version") != PROTOCOL_VERSION:
        raise BusV1Error("canonical handoff_protocol_version is unsupported")
    if canonical.get("trading_date") != dashed or canonical.get("session") != session:
        raise BusV1Error("canonical date/session mismatch")
    if not isinstance(canonical.get("title"), str) or not canonical["title"].strip():
        raise BusV1Error("canonical title is empty")
    if not isinstance(canonical.get("body"), str) or not canonical["body"].strip():
        raise BusV1Error("canonical body is empty")
    canonical_hash = str(canonical.get("canonical_hash") or "")
    if not SHA256_RE.fullmatch(canonical_hash):
        raise BusV1Error("canonical_hash is invalid")
    if canonical_hash != canonical_hash_for(canonical["title"], canonical["body"]):
        raise BusV1Error("canonical_hash does not match title/body")
    for key in ("source_decision_hash", "source_input_hash"):
        value = str(canonical.get(key) or "")
        if value and not SHA256_RE.fullmatch(value):
            raise BusV1Error(f"{key} is invalid")
    for key in ("source_decision_path", "source_input_path"):
        if key in canonical and (not isinstance(canonical.get(key), str) or not canonical[key].strip()):
            raise BusV1Error(f"{key} is invalid")
    validate_canonical_completeness(canonical, date=date, session=session)
    assert_bus_safe(canonical)
    return canonical


def _canonical_bundle(canonical: dict[str, Any], *, decision_path: str, decision_hash: str, input_path: str, input_hash: str) -> dict[Path, bytes]:
    date = str(canonical["trading_date"])
    session = str(canonical["session"])
    canonical_content = json_bytes(canonical)
    manifest = {
        "schema_version": CANONICAL_MANIFEST_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "trading_date": date,
        "session": session,
        "canonical_hash": canonical["canonical_hash"],
        "canonical_json_sha256": sha256_bytes(canonical_content),
        "source_decision_path": decision_path,
        "source_decision_hash": decision_hash,
        "source_input_path": input_path,
        "source_input_hash": input_hash,
        "created_at": now(),
        "immutable": True,
    }
    root = session_root(date, session) / "canonical"
    return {
        root / "canonical.json": canonical_content,
        root / "canonical.sha256": (str(sha256_bytes(canonical_content)) + "\n").encode("ascii"),
        root / "manifest.json": json_bytes(manifest),
    }


def promote_canonical(
    root: Path,
    date: str,
    session: str,
    *,
    branch: str = BUS_BRANCH,
    decision_reader: Callable[..., tuple[dict[str, Any] | None, str, str]] = read_bus_envelope,
    publisher: Callable[..., dict[str, Any]] = _publish_files,
    trace: TransportTrace | None = None,
) -> dict[str, Any]:
    decision_envelope, decision_path, decision_hash = decision_reader(
        root, date, session, "decision", branch=branch
    )
    if decision_envelope is None:
        return {"ok": True, "status": "WAITING_DECISION", "date": normalize_date(date)[1], "session": session}
    input_envelope, input_path, input_hash = decision_reader(
        root, date, session, "research-input", branch=branch
    )
    if input_envelope is None:
        return {"ok": False, "status": "FAILED_INPUT_MISSING", "error": "research input is missing"}
    decision_payload = validate_envelope(
        decision_envelope, date=date, session=session, message_type="decision"
    )
    input_payload = validate_envelope(
        input_envelope, date=date, session=session, message_type="research-input"
    )
    status, _ = _validate_decision_payload(decision_payload, date, session)
    if status == "WAITING_DECISION":
        return {
            "ok": True,
            "status": "WAITING_DECISION",
            "date": normalize_date(date)[1],
            "session": session,
            "decision_path": decision_path,
            "decision_hash": decision_hash,
        }
    canonical = build_canonical(
        decision_payload,
        decision_source_hash=decision_hash,
        input_source_hash=input_hash,
        date=date,
        session=session,
        decision_source_path=decision_path,
        input_source_path=input_path,
    )
    canonical_path = relative_path(date, session, "canonical")
    existing_content, _ = _git_object(root, branch, canonical_path)
    if existing_content is not None:
        existing = _parse_json(existing_content, "existing canonical")
        validate_canonical(existing, date=date, session=session)
        if existing["canonical_hash"] == canonical["canonical_hash"]:
            if _canonical_metadata_refreeze_allowed(existing, canonical):
                files = _canonical_bundle(
                    canonical,
                    decision_path=decision_path,
                    decision_hash=decision_hash,
                    input_path=input_path,
                    input_hash=input_hash,
                )
                transport = trace or TransportTrace()
                published = _call_publisher(
                    publisher,
                    root,
                    branch,
                    files,
                    f"bus/v1: refreeze canonical contract metadata {date} {session}",
                    trace=transport,
                    replace_paths=set(files),
                )
                return {
                    "ok": True,
                    "status": "CANONICAL_FROZEN",
                    "idempotent": False,
                    "refrozen": True,
                    "canonical_hash": canonical["canonical_hash"],
                    "canonical_path": str(canonical_path).replace("\\", "/"),
                    "canonical_json_sha256": sha256_bytes(json_bytes(canonical)),
                    "transport": transport.to_dict(),
                    "publish": published,
                }
            return {
                "ok": True,
                "status": "CANONICAL_IDEMPOTENT",
                "idempotent": True,
                "canonical_hash": canonical["canonical_hash"],
                "canonical_path": str(canonical_path).replace("\\", "/"),
            }
        return {
            "ok": False,
            "status": "CANONICAL_CONFLICT",
            "error": "existing canonical has a different canonical_hash",
            "existing_canonical_hash": existing["canonical_hash"],
            "candidate_canonical_hash": canonical["canonical_hash"],
        }
    files = _canonical_bundle(
        canonical,
        decision_path=decision_path,
        decision_hash=decision_hash,
        input_path=input_path,
        input_hash=input_hash,
    )
    transport = trace or TransportTrace()
    published = _call_publisher(
        publisher,
        root,
        branch,
        files,
        f"bus/v1: promote canonical {date} {session}",
        trace=transport,
    )
    return {
        "ok": True,
        "status": "CANONICAL_FROZEN",
        "idempotent": False,
        "canonical_hash": canonical["canonical_hash"],
        "canonical_path": str(canonical_path).replace("\\", "/"),
        "canonical_json_sha256": sha256_bytes(json_bytes(canonical)),
        "transport": transport.to_dict(),
        "publish": published,
    }


def collect(
    root: Path,
    date: str,
    session: str,
    *,
    branch: str = BUS_BRANCH,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    publisher: Callable[..., dict[str, Any]] = _publish_files,
) -> dict[str, Any]:
    trace = TransportTrace()
    try:
        root = repo_root(root.resolve())
        _, dashed = normalize_date(date)
        if session not in SUPPORTED_SESSIONS:
            raise BusV1Error(f"unsupported session: {session}", stage=STAGE_LOCAL_ARTIFACT_GENERATED, exit_code=EXIT_COLLECT_FAILURE)
        attempt_id = f"bus-v1-{session}-{dashed.replace('-', '')}-{uuid.uuid4().hex[:10]}"
        started_at = now()
        pipeline = Path(__file__).with_name("run_daily_pipeline_v1.py")
        command = [
            sys.executable,
            str(pipeline),
            dashed,
            "--root",
            str(root),
            "--stage",
            "prepare",
            "--session",
            session,
            "--fresh-attempt",
            "--attempt-id",
            attempt_id,
            "--attempt-started-at",
            started_at,
        ]
        command.append("--public-only" if session == "premarket" else "--data-only")
        command.extend(("--trading-context", str(root / DEFAULT_CONTEXT_RELATIVE)))

        generated_index = trace.start(STAGE_LOCAL_ARTIFACT_GENERATED)
        try:
            completed = runner(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode:
                raise BusV1Error(
                    f"collect pipeline failed with exit code {completed.returncode}",
                    stage=STAGE_LOCAL_ARTIFACT_GENERATED,
                    exit_code=EXIT_COLLECT_FAILURE,
                )

            payload = build_research_input(
                root,
                dashed,
                session=session,
                attempt_id=attempt_id,
                attempt_started_at=started_at,
                trading_context_path=root / DEFAULT_CONTEXT_RELATIVE,
            )
            envelope = make_envelope(
                payload,
                date=dashed,
                session=session,
                message_type="research-input",
                producer="local-collector",
                created_at=str(payload.get("updated_at") or now()),
            )
            envelope_content = json_bytes(envelope)
            local_path = _write_local_object(root, dashed, session, "input", envelope_content)
            manifest = {
                "schema_version": INPUT_MANIFEST_SCHEMA,
                "protocol_version": PROTOCOL_VERSION,
                "trading_date": dashed,
                "session": session,
                "message_type": "research-input",
                "producer": "local-collector",
                "attempt_id": attempt_id,
                "source_local_path": str(local_path).replace("\\", "/"),
                "source_hash": sha256_bytes(envelope_content),
                "payload_hash": envelope["payload_hash"],
                "created_at": now(),
                "pipeline_status": "completed",
            }
            files = {
                relative_path(dashed, session, "input"): envelope_content,
                relative_path(dashed, session, "input_manifest"): json_bytes(manifest),
            }
            trace.finish(
                generated_index,
                status="PASS",
                exit_code=0,
                details={
                    "artifact_path": str(local_path).replace("\\", "/"),
                    "artifact_sha256": sha256_bytes(envelope_content),
                    "pipeline_exit_code": int(completed.returncode),
                    "pipeline_stdout": sanitize_error(completed.stdout or ""),
                    "pipeline_stderr": sanitize_error(completed.stderr or ""),
                },
            )
        except Exception as exc:
            if trace.records[generated_index].get("status") == "RUNNING":
                trace.finish(
                    generated_index,
                    status="FAIL",
                    exit_code=int(getattr(exc, "exit_code", EXIT_COLLECT_FAILURE) or EXIT_COLLECT_FAILURE),
                    error_summary=exc,
                )
            raise

        def validate_local_artifact() -> dict[str, Any]:
            parsed = _parse_json(envelope_content, "local research input")
            validate_envelope(parsed, date=dashed, session=session, message_type="research-input")
            local_sha = sha256_bytes(envelope_content)
            if local_sha != sha256_bytes(local_path.read_bytes()):
                raise BusV1Error("local artifact bytes changed before publish", stage=STAGE_LOCAL_ARTIFACT_VALIDATION, exit_code=EXIT_LOCAL_VALIDATION_FAILURE)
            manifest_payload = _parse_json(files[relative_path(dashed, session, "input_manifest")], "local input manifest")
            if manifest_payload.get("source_hash") != local_sha or manifest_payload.get("payload_hash") != parsed.get("payload_hash"):
                raise BusV1Error("local input manifest does not match artifact", stage=STAGE_LOCAL_ARTIFACT_VALIDATION, exit_code=EXIT_LOCAL_VALIDATION_FAILURE)
            return {"artifact_sha256": local_sha, "payload_hash": parsed.get("payload_hash")}

        _run_transport_stage(
            trace,
            STAGE_LOCAL_ARTIFACT_VALIDATION,
            EXIT_LOCAL_VALIDATION_FAILURE,
            validate_local_artifact,
        )
        published = _call_publisher(
            publisher,
            root,
            branch,
            files,
            f"bus/v1: publish research input {dashed} {session}",
            trace=trace,
        )
        if publisher is _publish_files and published.get("remote_publish_status") not in {"REMOTE_PUBLISH_PASS", "REMOTE_PUBLISH_IDEMPOTENT"}:
            raise BusV1Error(
                "remote publish did not reach a verified terminal status",
                stage=STAGE_REMOTE_SHA_CONTENT_VALIDATION,
                exit_code=EXIT_REMOTE_CONTENT_VALIDATION_FAILURE,
            )
        trace.skip_unvisited(reason="custom publisher did not expose transport stages")
        return {
            "ok": True,
            "status": "PUBLISHED",
            "date": dashed,
            "session": session,
            "attempt_id": attempt_id,
            "path": str(relative_path(dashed, session, "input")).replace("\\", "/"),
            "payload_hash": envelope["payload_hash"],
            "message_hash": sha256_bytes(envelope_content),
            "remote_publish_status": published.get("remote_publish_status", "REMOTE_PUBLISH_PASS"),
            "transport": trace.to_dict(),
            "publish": published,
        }
    except Exception as exc:
        _ensure_trace_failure(
            trace,
            exc,
            default_stage=STAGE_LOCAL_ARTIFACT_GENERATED,
            default_exit_code=EXIT_COLLECT_FAILURE,
        )
        trace.skip_unvisited(reason="not reached because an earlier collect or transport stage failed")
        error = _attach_transport_error(
            exc,
            trace,
            stage=str(getattr(exc, "stage", "") or STAGE_LOCAL_ARTIFACT_GENERATED),
            exit_code=int(getattr(exc, "exit_code", EXIT_INTERNAL_FAILURE) or EXIT_INTERNAL_FAILURE),
        )
        if error is exc:
            raise
        raise error from exc


def bootstrap_legacy(
    root: Path,
    date: str,
    session: str,
    *,
    branch: str = BUS_BRANCH,
    publisher: Callable[..., dict[str, Any]] = _publish_files,
) -> dict[str, Any]:
    root = repo_root(root.resolve())
    _, dashed = normalize_date(date)
    legacy_input_path = legacy_relative_path(dashed, "research-input.json")
    legacy_input, input_hash = _git_object(root, branch, legacy_input_path)
    if legacy_input is None:
        raise BusV1Error(f"legacy input is missing: {legacy_input_path.as_posix()}")
    input_envelope = _legacy_input_envelope(
        legacy_input,
        date=dashed,
        session=session,
        source_hash=input_hash,
        source_path=str(legacy_input_path).replace("\\", "/"),
    )
    files: dict[Path, bytes] = {
        relative_path(dashed, session, "input"): json_bytes(input_envelope),
        relative_path(dashed, session, "input_manifest"): json_bytes(
            {
                "schema_version": INPUT_MANIFEST_SCHEMA,
                "protocol_version": PROTOCOL_VERSION,
                "trading_date": dashed,
                "session": session,
                "message_type": "research-input",
                "producer": "migration",
                "source_path": str(legacy_input_path).replace("\\", "/"),
                "source_hash": input_hash,
                "migrated_at": input_envelope["created_at"],
            }
        ),
    }
    legacy_decision_path = legacy_relative_path(dashed, "chatgpt-decision.json")
    legacy_decision, decision_hash = _git_object(root, branch, legacy_decision_path)
    decision_result = "not_available"
    if legacy_decision is not None:
        decision_envelope = _legacy_decision_envelope(
            legacy_decision, date=dashed, session=session, source_hash=decision_hash
        )
        files[relative_path(dashed, session, "decision")] = json_bytes(decision_envelope)
        files[relative_path(dashed, session, "decision_manifest")] = json_bytes(
            {
                "schema_version": DECISION_MANIFEST_SCHEMA,
                "protocol_version": PROTOCOL_VERSION,
                "trading_date": dashed,
                "session": session,
                "message_type": "decision",
                "producer": "migration",
                "source_path": str(legacy_decision_path).replace("\\", "/"),
                "source_hash": decision_hash,
                "migrated_at": decision_envelope["created_at"],
            }
        )
        decision_result = "migrated"
    published = publisher(root, branch, files, f"bus/v1: bootstrap legacy {dashed} {session}")
    return {
        "ok": True,
        "status": "BOOTSTRAPPED",
        "date": dashed,
        "session": session,
        "input_source_path": str(legacy_input_path).replace("\\", "/"),
        "input_source_hash": input_hash,
        "decision": decision_result,
        "publish": published,
    }


def bootstrap_existing_input(
    root: Path,
    date: str,
    session: str,
    source_path: Path,
    *,
    branch: str = BUS_BRANCH,
    publisher: Callable[..., dict[str, Any]] = _publish_files,
) -> dict[str, Any]:
    """Explicitly migrate one local prepared input; session is never inferred."""
    root = repo_root(root.resolve())
    _, dashed = normalize_date(date)
    source_path = source_path.resolve()
    if not source_path.exists() or not source_path.is_file():
        raise BusV1Error(f"input source is missing: {source_path}")
    content = source_path.read_bytes()
    source_hash = sha256_bytes(content)
    input_envelope = _legacy_input_envelope(
        content,
        date=dashed,
        session=session,
        source_hash=source_hash,
        source_path=str(source_path).replace("\\", "/"),
    )
    existing_content, _ = _git_object(root, branch, relative_path(dashed, session, "input"))
    if existing_content is not None:
        existing = _parse_json(existing_content, "existing bus/v1 input")
        validate_envelope(existing, date=dashed, session=session, message_type="research-input")
        if existing.get("source_hash") == source_hash:
            return {
                "ok": True,
                "status": "BOOTSTRAP_IDEMPOTENT",
                "date": dashed,
                "session": session,
                "input_source_path": str(source_path),
                "input_source_hash": source_hash,
            }
        raise BusV1Error("existing bus/v1 input has a different source_hash")
    files: dict[Path, bytes] = {
        relative_path(dashed, session, "input"): json_bytes(input_envelope),
        relative_path(dashed, session, "input_manifest"): json_bytes(
            {
                "schema_version": INPUT_MANIFEST_SCHEMA,
                "protocol_version": PROTOCOL_VERSION,
                "trading_date": dashed,
                "session": session,
                "message_type": "research-input",
                "producer": "migration",
                "source_path": str(source_path).replace("\\", "/"),
                "source_hash": source_hash,
                "migrated_at": input_envelope["created_at"],
            }
        ),
    }
    published = publisher(root, branch, files, f"bus/v1: bootstrap prepared input {dashed} {session}")
    return {
        "ok": True,
        "status": "BOOTSTRAPPED_INPUT",
        "date": dashed,
        "session": session,
        "input_source_path": str(source_path),
        "input_source_hash": source_hash,
        "publish": published,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily Bus v1 maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap",):
        command = sub.add_parser(name)
        command.add_argument("date")
        command.add_argument("--session", choices=SUPPORTED_SESSIONS, required=True)
        command.add_argument("--root", required=True)
        command.add_argument("--bus-branch", default=BUS_BRANCH)
        command.add_argument("--input", help="explicit local prepared input path for migration")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.input:
            result = bootstrap_existing_input(
                Path(args.root),
                args.date,
                args.session,
                Path(args.input),
                branch=args.bus_branch,
            )
        else:
            result = bootstrap_legacy(Path(args.root), args.date, args.session, branch=args.bus_branch)
    except (BusV1Error, LegacyBusError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
