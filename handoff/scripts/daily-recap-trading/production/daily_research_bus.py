#!/usr/bin/env python3
"""Private GitHub transport for the Daily Pipeline v1 handoff.

The bus carries only two dated, privacy-filtered JSON files on the dedicated
``daily-bus`` branch.  It never uploads local holdings or raw source bundles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from production_task_logging import redact
from trading_context_v1 import context_path_for_root, resolve_trading_context

from run_daily_pipeline_v1 import (
    HANDOFF_SCHEMA,
    default_handoff,
    merge_handoff,
    normalize_date,
    now,
    read_json,
    run_id_for_date,
    update_handoff,
    write_json,
)


BUS_BRANCH = "daily-bus"
RESEARCH_SCHEMA = "daily-research-input-v1"
DECISION_SCHEMA = "daily-chatgpt-decision-v1"
CLOSE_READINESS_SCHEMA = "daily-close-readiness-v1"
PREPARED_ARTIFACT_SCHEMA = "daily-research-prepared-artifact-v1"
MAX_ZSXQ_CARDS = 50
BUS_ROOT = Path("daily-bus")
SHANGHAI = timezone(timedelta(hours=8))
PUSH_MAX_ATTEMPTS = 3
PUSH_RETRY_DELAYS_SECONDS = (5.0, 15.0)
PUSH_RETRYABLE_ERROR_MARKERS = (
    "schannel: failed to receive handshake",
    "ssl/tls connection failed",
    "connection reset",
    "connection timed out",
    "timed out",
    "temporary failure",
    "temporarily unavailable",
    "remote end hung up unexpectedly",
    "connection closed",
    "could not resolve host",
    "failed to connect",
)
PUSH_NON_RETRYABLE_ERROR_MARKERS = (
    "authentication failed",
    "authentication is required",
    "could not authenticate",
    "invalid username or password",
    "could not read username",
    "permission denied",
    "access denied",
    "non-fast-forward",
    "fetch first",
    "rejected",
    "remote rejected",
    "failed to push some refs",
    "invalid ref",
    "pre-receive hook declined",
)
# Privacy is a data-boundary rule, not a vocabulary blacklist.  Structured
# private fields remain a hard failure.  Free text is checked by deterministic
# combinations of ownership/account context and private portfolio or trade
# facts, so public research can still discuss risk budgets and market
# positioning.
PRIVATE_FIELD_NAMES = frozenset({
    "private",
    "private_holdings",
    "holdings",
    "holdings_mapping",
    "positions",
    "position",
    "shares",
    "quantity",
    "cost_basis",
    "user_cost_basis",
    "account_balance",
    "account_assets",
    "account_equity",
    "pnl",
    "profit_loss",
    "financing",
    "margin_liability",
    "private_trade_actions",
    "private_transactions",
    "portfolio",
})
PRIVATE_BOOLEAN_FIELD_NAMES = frozenset({"private_related", "contains_private_data"})
PRIVATE_ARTIFACT_PATTERNS = (
    ("private_artifact", re.compile(
        r"\b(?:holdings[_ -]?mapping|private[_ -]?holdings|cost[_ -]?basis|"
        r"account[_ -]?(?:balance|assets|equity)|margin[_ -]?liability|"
        r"private[_ -]?(?:trade|transaction|plan)|pnl)\b",
        re.I,
    )),
    ("credential", re.compile(r"\b(?:api[_ -]?key|credential|cookie|token|secret)\b", re.I)),
)
PRIVATE_OWNER_DOMAIN_RE = re.compile(
    r"(?:我的|本人|个人|私人|我(?!们|方)(?:目前|现在|当前|今天|今日)?|"
    r"my(?:\s+current)?|personal|private|broker(?:age)?)"
    r"[\s:：,，、]{0,2}.{0,8}?"
    r"(?:账户|持仓|仓位|持有|成本|盈亏|损益|余额|资产|融资|负债|"
    r"holdings?|positions?|cost(?:\s+basis)?|pnl|profit.?loss|balance|assets?|"
    r"margin|liabilit(?:y|ies)|shares?|quantity|trade|order|execution)",
    re.I,
)
PRIVATE_ACCOUNT_PATTERNS = (
    ("private_account", re.compile(
        r"(?:账户(?:可用|当前|总)?(?:余额|资产|资金|权益|净值|成本|盈亏|持仓|仓位)|"
        r"可用余额|账户资金|account\s+(?:balance|assets?|equity|pnl|holdings?|"
        r"positions?|cost|shares?|quantity|margin)|broker(?:age)?\s+(?:account|balance|position))",
        re.I,
    )),
    ("private_pnl", re.compile(
        r"(?:(?:累计|浮动|实现|账户|个人|我的)(?:盈亏|损益)|"
        r"(?:account\s+)?(?:pnl|profit.?loss))",
        re.I,
    )),
    ("private_financing", re.compile(
        r"(?:融资(?:负债|债务|欠款|账户)|融券负债|margin\s+(?:liability|debt))",
        re.I,
    )),
)
PRIVATE_POSITION_PATTERNS = (
    ("private_position_owner", re.compile(
        r"(?:我的|本人|我(?!们|方)(?:目前|现在|当前)?|my(?:\s+current)?)\s*"
        r"(?:账户|持仓|仓位|持有|holdings?|positions?)",
        re.I,
    )),
    ("private_position_numeric", re.compile(
        r"(?:持仓|仓位)\s*[:：=]?\s*[-+]?\d+(?:\.\d+)?\s*%?",
        re.I,
    )),
    ("private_position_cost", re.compile(
        r"(?:持仓|仓位)\s*(?:的)?成本\s*[-+]?\d+(?:\.\d+)?",
        re.I,
    )),
    ("private_quantity", re.compile(
        r"(?:当前|目前|现在)?\s*持有\s*"
        r"(?!单位|机构|公司|行业|基金|北向)[\u4e00-\u9fffA-Za-z]{2,16}"
        r"\s*\d[\d,]*(?:\.\d+)?\s*(?:股|股数|数量|shares?|quantity)",
        re.I,
    )),
    ("private_security_cost", re.compile(
        r"(?!单位|生产|原材料|服务器|AI|产品|行业|公司|项目)"
        r"[\u4e00-\u9fff]{2,12}\s*(?:的)?成本\s*[-+]?\d+(?:\.\d+)?",
        re.I,
    )),
)
PRIVATE_TRADE_PATTERNS = (
    ("private_trade_action", re.compile(
        r"(?:今天|今日|目前|现在)\s*(?:我|本人)?\s*(?:主动)?"
        r"(?:加仓|减仓|买入|卖出|建仓|平仓|清仓)\s*"
        r"(?:[\u4e00-\u9fff]{2,12}|[A-Z]{0,3}\d{6})",
        re.I,
    )),
    ("private_trade_owner", re.compile(
        r"(?:我的|本人|私人|我(?!们|方)(?:今天|今日|目前|现在)?|my)\s*"
        r"(?:交易|交易计划|私密计划|私人交易|交易动作|买入|卖出|加仓|减仓|"
        r"buy|sell|trade|order|execution)",
        re.I,
    )),
    ("private_execution", re.compile(
        r"(?:成交明细|个人成交|账户成交|成交记录|成交股数|成交数量|"
        r"买入成交价|卖出成交价|买入成交股数|卖出成交股数|买入成交数量|卖出成交数量|"
        r"(?:my|personal|account)\s+(?:sell|buy|trade|execution|order|shares?|quantity))",
        re.I,
    )),
)


class BusError(RuntimeError):
    """A transport, validation, merge, or privacy failure."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details if isinstance(details, dict) else {}


def compact_text(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def content_summary(value: Any, limit: int = 160) -> str:
    """Keep informative clauses without copying an entire topic body."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return "正文未提供可压缩内容"
    clauses = [part.strip(" ，,；;。！？!?\t") for part in re.split(r"(?<=[。！？!?；;])", text) if part.strip()]
    summary = "；".join(clauses[:2]) or text
    if len(summary) > limit:
        summary = summary[: limit - 1].rstrip(" ，,；;") + "…"
    elif summary == text:
        cut = max(1, min(len(summary) - 1, int(len(summary) * 0.8)))
        summary = summary[:cut].rstrip(" ，,；;") + "…"
    return summary


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BusError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _shanghai_zone() -> ZoneInfo | timezone:
    """Return the production business clock without requiring tzdata on Windows."""

    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:  # pragma: no cover - Windows images may omit IANA tzdata
        return SHANGHAI


def _context_timestamp(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise BusError("premarket cutoff timestamp is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BusError("premarket timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_shanghai_zone())
    return parsed.astimezone(_shanghai_zone())


def build_premarket_context(
    root: Path,
    date: str,
    *,
    trading_context_path: Path | None = None,
    trading_context: dict[str, Any] | None = None,
    cutoff_at: str = "",
) -> dict[str, str]:
    """Build the premarket time boundary from validated trading_context_v1."""

    _, target_date = normalize_date(date)
    context = resolve_trading_context(
        root,
        target_date,
        context_path=trading_context_path,
        context=trading_context,
        validated_at=cutoff_at or now(),
    )
    if context.get("validation_status") != "validated":
        raise BusError(f"{context.get('error_code', 'TRADING_CALENDAR_MISMATCH')}:{context.get('error', 'context unavailable')}")
    if context.get("is_trading_day") is not True:
        raise BusError("TRADING_CONTEXT_NOT_TRADING_DAY")
    as_of = str(context["previous_trading_day"])
    zone = _shanghai_zone()
    cutoff = _context_timestamp(cutoff_at) if cutoff_at else datetime.now(zone)
    window_start = datetime.fromisoformat(f"{as_of}T15:00:00+08:00").astimezone(zone)
    if cutoff < window_start:
        raise BusError("premarket cutoff precedes overnight window start")
    return {
        "session": "premarket",
        "target_date": target_date,
        "as_of_trading_date": as_of,
        "market_snapshot_as_of": as_of,
        "overnight_window_start": window_start.isoformat(timespec="seconds"),
        "overnight_window_end": cutoff.isoformat(timespec="seconds"),
        "public_only": True,
        "trading_context": context,
    }


def _window_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_shanghai_zone())
    return parsed.astimezone(_shanghai_zone())


def _public_collection_status(status: dict[str, Any]) -> dict[str, Any]:
    """Keep the bus status useful without carrying private-run artifacts."""

    allowed = (
        "schema_version",
        "pipeline_version",
        "date",
        "run_id",
        "created_at",
        "updated_at",
        "status",
        "session",
        "public_only",
        "attempt_id",
        "attempt_started_at",
        "attempt_finished_at",
        "attempt_status",
    )
    result = {key: status.get(key) for key in allowed if key in status}
    result["steps"] = [
        step for step in status.get("steps", [])
        if isinstance(step, dict) and not private_hits({"name": step.get("name"), "detail": step.get("detail")})
    ]
    result["blockers"] = [item for item in status.get("blockers", []) if not private_hits(item)]
    result["warnings"] = [item for item in status.get("warnings", []) if not private_hits(item)]
    return result


def _normalized_field_name(value: Any) -> str:
    raw = str(value or "").strip()
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    return re.sub(r"[^a-zA-Z0-9]+", "_", raw).strip("_").lower()


def _free_text_private_hits(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return []
    hits: list[str] = []
    for name, pattern in PRIVATE_ARTIFACT_PATTERNS:
        if pattern.search(normalized):
            hits.append(name)
    for name, pattern in PRIVATE_ACCOUNT_PATTERNS:
        if pattern.search(normalized):
            hits.append(name)
    for name, pattern in PRIVATE_POSITION_PATTERNS:
        if pattern.search(normalized):
            hits.append(name)
    for name, pattern in PRIVATE_TRADE_PATTERNS:
        if pattern.search(normalized):
            hits.append(name)

    # Ownership markers only become private when paired with a portfolio or
    # execution term.  This deliberately lets public wording such as
    # "机构持仓比例" and "风险预算维持 1/3" through the gate.
    if PRIVATE_OWNER_DOMAIN_RE.search(normalized):
        hits.append("private_owner_context")
    return hits


def private_hits(value: Any) -> list[str]:
    """Return deterministic privacy violations without blacklisting research vocabulary."""

    hits: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                field = _normalized_field_name(key)
                if field in PRIVATE_FIELD_NAMES:
                    hits.append(f"private_field:{field}")
                elif field in PRIVATE_BOOLEAN_FIELD_NAMES and child is True:
                    hits.append(f"private_field:{field}")
                elif str(key).strip() in {"私人持仓", "账户余额", "账户资产", "融资负债"}:
                    hits.append(f"private_field:{key}")
                walk(child)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
            return
        if isinstance(item, str):
            hits.extend(_free_text_private_hits(item))

    walk(value)
    return list(dict.fromkeys(hits))


def assert_bus_safe(payload: dict[str, Any]) -> None:
    hits = private_hits(payload)
    if hits:
        raise BusError("privacy gate rejected bus payload: " + ", ".join(hits))


def git_run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def git_checked(cwd: Path, *args: str) -> str:
    result = git_run(cwd, *args)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise BusError(f"git {' '.join(args)} failed ({result.returncode}): {detail}")
    return result.stdout.strip()


def repo_root(root: Path) -> Path:
    actual = Path(git_checked(root, "rev-parse", "--show-toplevel")).resolve()
    if actual != root.resolve():
        raise BusError(f"--root must be the repository root: {actual}")
    git_checked(root, "remote", "get-url", "origin")
    return actual


def remote_branch_exists(root: Path, branch: str) -> bool:
    result = git_run(root, "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}")
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    detail = (result.stderr or result.stdout or "").strip()
    raise BusError(f"git ls-remote origin {branch} failed ({result.returncode}): {detail}")


def fetch_bus(root: Path, branch: str) -> bool:
    exists = remote_branch_exists(root, branch)
    if exists:
        git_checked(root, "fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}")
    return exists


@contextmanager
def bus_worktree(root: Path, branch: str, *, branch_exists: bool | None = None) -> Iterator[Path]:
    """Create a detached bus worktree without touching the caller's checkout."""

    exists = fetch_bus(root, branch) if branch_exists is None else branch_exists
    base = f"origin/{branch}" if exists else "HEAD"
    with tempfile.TemporaryDirectory(prefix="alphaengine-daily-bus-") as temp:
        worktree = Path(temp) / "checkout"
        git_checked(root, "worktree", "add", "--detach", str(worktree), base)
        try:
            yield worktree
        finally:
            result = git_run(root, "worktree", "remove", "--force", str(worktree))
            if result.returncode:
                detail = (result.stderr or result.stdout or "").strip()
                raise BusError(f"git worktree cleanup failed ({result.returncode}): {detail}")


def classify_push_error(result: subprocess.CompletedProcess[str]) -> str:
    """Classify only known transient network failures as retryable."""

    detail = str(result.stderr or result.stdout or "").lower()
    if any(marker in detail for marker in PUSH_NON_RETRYABLE_ERROR_MARKERS):
        return "non_retryable"
    if any(marker in detail for marker in PUSH_RETRYABLE_ERROR_MARKERS):
        return "transient_network"
    return "non_retryable"


def _push_error_detail(result: subprocess.CompletedProcess[str]) -> str:
    return compact_text(redact(result.stderr or result.stdout or f"exit code {result.returncode}"))


def _is_stale_branch_error(result: subprocess.CompletedProcess[str]) -> bool:
    detail = str(result.stderr or result.stdout or "").lower()
    return any(marker in detail for marker in ("non-fast-forward", "fetch first", "[rejected]"))


def push_with_retry(
    worktree: Path,
    branch: str,
    *,
    publish_status: dict[str, Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = git_run,
    sleeper: Callable[[float], None] = time.sleep,
    max_attempts: int = PUSH_MAX_ATTEMPTS,
    stale_sync: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Push one commit with bounded transient retries and optional safe stale sync.

    Generic callers retain the historical fail-closed non-fast-forward
    behaviour.  The Daily Bus publisher supplies ``stale_sync`` only after it
    has re-fetched the branch and checked every target path for lineage
    conflicts; it never force-pushes or resets the user's working tree.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    state = publish_status if publish_status is not None else {}
    attempts: list[dict[str, Any]] = []
    state.update(
        {
            "publish_status": "running",
            "publish_attempt_count": 0,
            "publish_attempts": attempts,
            "publish_error": "",
            "publish_retry_entered": False,
        }
    )
    refspec = f"HEAD:refs/heads/{branch}"
    for attempt in range(1, max_attempts + 1):
        result = runner(worktree, "push", "origin", refspec)
        state["publish_attempt_count"] = attempt
        if result.returncode == 0:
            attempts.append(
                {
                    "attempt": attempt,
                    "classification": "succeeded",
                    "retry": False,
                    "error": "",
                }
            )
            state["publish_status"] = "succeeded"
            state["publish_retry_entered"] = attempt > 1
            return state

        error = _push_error_detail(result)
        classification = "stale_branch" if stale_sync is not None and _is_stale_branch_error(result) else classify_push_error(result)
        should_retry = classification in {"transient_network", "stale_branch"} and attempt < max_attempts
        row: dict[str, Any] = {
            "attempt": attempt,
            "classification": classification,
            "retry": should_retry,
            "error": error,
        }
        if should_retry:
            delay = PUSH_RETRY_DELAYS_SECONDS[min(attempt - 1, len(PUSH_RETRY_DELAYS_SECONDS) - 1)]
            row["retry_after_seconds"] = delay
            state["publish_retry_entered"] = True
            if classification == "stale_branch" and stale_sync is not None:
                try:
                    row["stale_sync"] = redact(stale_sync()) or {"status": "succeeded"}
                except Exception as exc:
                    row["stale_sync"] = {"status": "failed", "error": compact_text(redact(exc))}
                    attempts.append(row)
                    state["publish_status"] = "failed"
                    state["publish_error"] = row["stale_sync"]["error"]
                    raise BusError(
                        f"safe stale-branch synchronization failed; publish_status={json.dumps(state, ensure_ascii=False)}"
                    ) from exc
        attempts.append(row)
        if should_retry:
            sleeper(delay)
            continue

        state["publish_status"] = "failed"
        state["publish_error"] = error
        serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        raise BusError(
            f"git push origin {refspec} failed ({result.returncode}); "
            f"publish_status={serialized}"
        )

    # The loop always returns or raises; keep a defensive fail-closed branch.
    state["publish_status"] = "failed"
    state["publish_error"] = "push retry limit exhausted"
    raise BusError(f"git push origin {refspec} failed; publish_status={json.dumps(state, ensure_ascii=False)}")


def commit_and_push(
    worktree: Path,
    branch: str,
    relative_path: Path,
    message: str,
    *,
    publish_status: dict[str, Any] | None = None,
    push_runner: Callable[..., subprocess.CompletedProcess[str]] = git_run,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    relative = str(relative_path).replace("\\", "/")
    git_checked(worktree, "add", "--", relative)
    staged = git_run(worktree, "diff", "--cached", "--quiet", "--", relative)
    if staged.returncode == 0:
        return False
    if staged.returncode != 1:
        raise BusError(f"git diff --cached failed ({staged.returncode})")
    git_checked(worktree, "commit", "-m", message)
    push_with_retry(
        worktree,
        branch,
        publish_status=publish_status,
        runner=push_runner,
        sleeper=sleeper,
    )
    return True


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    """Serialize Bus JSON with stable UTF-8/LF bytes on every platform."""

    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _root_relative(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def _prepared_identity(attempt_id: str) -> tuple[str, str]:
    raw = str(attempt_id or "").strip() or f"publish-{uuid.uuid4().hex}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")[:64] or "attempt"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return raw, f"{slug}-{digest}"


def _prepared_root(root: Path, date: str, session: str) -> tuple[str, Path]:
    compact, dashed = normalize_date(date)
    if session not in {"premarket", "close"}:
        raise BusError("prepared artifact session must be premarket or close")
    return dashed, root / f"tmp_recap_{compact}" / "prepared_bus" / session


def _create_prepared_artifact(
    root: Path,
    date: str,
    session: str,
    payload: dict[str, Any],
    *,
    attempt_id: str,
    attempt_started_at: str,
) -> dict[str, Any]:
    dashed, session_root = _prepared_root(root, date, session)
    if payload.get("date") != dashed:
        raise BusError("prepared artifact date mismatch")
    if payload.get("public_only") is not True:
        raise BusError("prepared artifact must be public_only")
    context = payload.get("time_context") if isinstance(payload.get("time_context"), dict) else {}
    if context.get("session") != session or context.get("target_date") != dashed:
        raise BusError("prepared artifact session or target_date mismatch")
    effective_attempt_id, identity = _prepared_identity(attempt_id)
    artifact_dir = session_root / identity
    try:
        artifact_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise BusError("prepared artifact identity already exists; refusing overwrite") from exc
    artifact_path = artifact_dir / "research-input.json"
    artifact_bytes = _json_bytes(payload)
    artifact_path.write_bytes(artifact_bytes)
    manifest_path = artifact_dir / "manifest.json"
    manifest = {
        "schema_version": PREPARED_ARTIFACT_SCHEMA,
        "date": dashed,
        "session": session,
        "run_id": str(payload.get("run_id") or ""),
        "attempt_id": effective_attempt_id,
        "attempt_started_at": str(attempt_started_at or now()),
        "prepared_at": now(),
        "source_updated_at": str(payload.get("updated_at") or ""),
        "target_bus_path": str(BUS_ROOT / dashed / "research-input.json").replace("\\", "/"),
        "artifact_path": _root_relative(artifact_path, root),
        "artifact_sha256": _sha256_bytes(artifact_bytes),
        "artifact_size": len(artifact_bytes),
        "public_only": True,
        "status": "prepared",
    }
    write_json(manifest_path, manifest)
    return {
        "manifest_path": manifest_path,
        "artifact_path": artifact_path,
        "artifact_bytes": artifact_bytes,
        "manifest": manifest,
        "date": dashed,
        "session": session,
        "target_bus_path": manifest["target_bus_path"],
    }


def _prepared_observability(manifest: dict[str, Any], manifest_path: Path, root: Path) -> dict[str, Any]:
    return {
        "prepared_status": manifest.get("status", ""),
        "prepared_path": manifest.get("artifact_path", ""),
        "prepared_manifest_path": _root_relative(manifest_path, root),
        "prepared_sha256": manifest.get("artifact_sha256", ""),
        "prepared_size": manifest.get("artifact_size", 0),
        "prepared_session": manifest.get("session", ""),
        "prepared_attempt_id": manifest.get("attempt_id", ""),
        "publish_status": manifest.get("publish_status", "not_attempted"),
        "publish_attempt_count": manifest.get("publish_attempt_count", 0),
        "publish_error": manifest.get("publish_error", ""),
    }


def _update_prepared_manifest(
    manifest_path: Path,
    *,
    status: str,
    publish_status: dict[str, Any],
    error: str = "",
    bus_target_sha256: str = "",
    bus_target_size: int | None = None,
) -> dict[str, Any]:
    manifest = read_json(manifest_path, None)
    if not isinstance(manifest, dict):
        raise BusError("prepared artifact manifest is missing or invalid")
    manifest["status"] = status
    manifest["publish_status"] = str(publish_status.get("publish_status") or ("failed" if status == "publish_failed" else "succeeded"))
    manifest["publish_attempt_count"] = int(publish_status.get("publish_attempt_count") or 0)
    manifest["publish_attempts"] = publish_status.get("publish_attempts", [])
    manifest["publish_retry_entered"] = bool(publish_status.get("publish_retry_entered", False))
    manifest["publish_error"] = compact_text(error or publish_status.get("publish_error", ""), 1000)
    if bus_target_sha256:
        manifest["bus_target_sha256"] = bus_target_sha256
    if bus_target_size is not None:
        manifest["bus_target_size"] = bus_target_size
    if bus_target_sha256:
        manifest["content_match"] = bus_target_sha256 == manifest.get("artifact_sha256")
    write_json(manifest_path, manifest)
    return manifest


def _load_prepared_artifact(
    root: Path,
    prepared_path: Path,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    prepared_path = prepared_path.resolve()
    manifest_path = (manifest_path or prepared_path.parent / "manifest.json").resolve()
    if not prepared_path.is_file():
        raise BusError("prepared artifact does not exist")
    if manifest_path != prepared_path.parent / "manifest.json" or not manifest_path.is_file():
        raise BusError("prepared artifact manifest does not exist")
    manifest = read_json(manifest_path, None)
    if not isinstance(manifest, dict):
        raise BusError("prepared artifact manifest is invalid")
    if manifest.get("schema_version") != PREPARED_ARTIFACT_SCHEMA:
        raise BusError("prepared artifact manifest schema_version is invalid")
    try:
        dashed, session_root = _prepared_root(root, str(manifest.get("date") or ""), str(manifest.get("session") or ""))
    except (BusError, ValueError) as exc:
        raise BusError("prepared artifact date or session is invalid") from exc
    try:
        relative_to_session = prepared_path.relative_to(session_root.resolve())
    except ValueError as exc:
        raise BusError("prepared artifact path is outside the durable prepared root") from exc
    if len(relative_to_session.parts) != 2 or relative_to_session.parts[-1] != "research-input.json":
        raise BusError("prepared artifact path has an invalid identity layout")
    if manifest.get("artifact_path") != _root_relative(prepared_path, root):
        raise BusError("prepared artifact manifest path mismatch")
    expected_target = str(BUS_ROOT / dashed / "research-input.json").replace("\\", "/")
    if manifest.get("target_bus_path") != expected_target:
        raise BusError("prepared artifact target path is invalid")
    if manifest.get("public_only") is not True:
        raise BusError("prepared artifact public_only must be true")
    if not str(manifest.get("attempt_id") or ""):
        raise BusError("prepared artifact attempt_id is required")
    if manifest.get("status") not in {"prepared", "publish_failed", "publish_succeeded"}:
        raise BusError("prepared artifact status is invalid")
    try:
        manifest_size = int(manifest.get("artifact_size"))
    except (TypeError, ValueError) as exc:
        raise BusError("prepared artifact size is invalid") from exc
    content = prepared_path.read_bytes()
    digest = _sha256_bytes(content)
    if digest != manifest.get("artifact_sha256") or len(content) != manifest_size:
        raise BusError("prepared artifact SHA256 or size mismatch")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusError("prepared artifact is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise BusError("prepared artifact payload must be a JSON object")
    if payload.get("schema_version") != RESEARCH_SCHEMA:
        raise BusError("prepared artifact payload schema_version is invalid")
    if payload.get("date") != dashed or payload.get("public_only") is not True:
        raise BusError("prepared artifact payload date or public_only mismatch")
    context = payload.get("time_context") if isinstance(payload.get("time_context"), dict) else {}
    if context.get("session") != manifest.get("session") or context.get("target_date") != dashed:
        raise BusError("prepared artifact payload session or target_date mismatch")
    if manifest.get("run_id") and payload.get("run_id") != manifest.get("run_id"):
        raise BusError("prepared artifact run_id mismatch")
    if manifest.get("source_updated_at") and payload.get("updated_at") != manifest.get("source_updated_at"):
        raise BusError("prepared artifact source_updated_at mismatch")
    assert_bus_safe(payload)
    return {
        "manifest_path": manifest_path,
        "artifact_path": prepared_path,
        "artifact_bytes": content,
        "manifest": manifest,
        "date": dashed,
        "session": str(manifest["session"]),
        "target_bus_path": expected_target,
    }


def _publish_prepared_artifact(root: Path, branch: str, prepared: dict[str, Any]) -> dict[str, Any]:
    manifest_path = prepared["manifest_path"]
    manifest = prepared["manifest"]
    artifact_bytes = prepared["artifact_bytes"]
    publish_status: dict[str, Any] = {}
    changed = False
    try:
        with bus_worktree(root, branch) as worktree:
            target = worktree / Path(*PurePosixPath(prepared["target_bus_path"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(artifact_bytes)
            target_bytes = target.read_bytes()
            target_digest = _sha256_bytes(target_bytes)
            if target_digest != manifest.get("artifact_sha256") or len(target_bytes) != manifest.get("artifact_size"):
                raise BusError("prepared artifact and Bus target bytes differ")
            changed = commit_and_push(
                worktree,
                branch,
                Path(*PurePosixPath(prepared["target_bus_path"]).parts),
                f"bus: publish research input {prepared['date']}",
                publish_status=publish_status,
            )
    except (BusError, OSError) as exc:
        publish_status.setdefault("publish_status", "failed")
        publish_status.setdefault("publish_attempt_count", 0)
        publish_status.setdefault("publish_attempts", [])
        publish_status["publish_error"] = str(exc)
        try:
            updated_manifest = _update_prepared_manifest(
                manifest_path,
                status="publish_failed",
                publish_status=publish_status,
                error=str(exc),
            )
        except BusError:
            updated_manifest = dict(manifest)
            updated_manifest["status"] = "prepared"
        exc.details = _prepared_observability(updated_manifest, manifest_path, root)
        raise
    if not publish_status:
        publish_status = {"publish_status": "succeeded", "publish_attempt_count": 0, "publish_attempts": []}
    updated_manifest = _update_prepared_manifest(
        manifest_path,
        status="publish_succeeded",
        publish_status=publish_status,
        bus_target_sha256=manifest["artifact_sha256"],
        bus_target_size=int(manifest["artifact_size"]),
    )
    return {
        "ok": True,
        "changed": changed,
        "branch": branch,
        "path": prepared["target_bus_path"],
        "date": prepared["date"],
        "session": prepared["session"],
        **_prepared_observability(updated_manifest, manifest_path, root),
        "bus_target_sha256": updated_manifest.get("bus_target_sha256", ""),
        "bus_target_size": updated_manifest.get("bus_target_size", 0),
        "content_match": updated_manifest.get("content_match", False),
    }


def publish_prepared_research(
    root: Path,
    prepared_artifact: Path,
    branch: str = BUS_BRANCH,
    *,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    root = repo_root(root.resolve())
    prepared_artifact = prepared_artifact if prepared_artifact.is_absolute() else root / prepared_artifact
    if manifest_path is not None and not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    prepared = _load_prepared_artifact(root, prepared_artifact, manifest_path)
    return _publish_prepared_artifact(root, branch, prepared)


def source_ref(source: str, source_id: Any, source_time: Any, **extra: Any) -> dict[str, Any]:
    ref = {"source": source, "source_id": str(source_id or ""), "source_time": str(source_time or "")}
    ref.update({key: value for key, value in extra.items() if value not in (None, "")})
    return ref


def load_collection_status(folder: Path, date: str, attempt_id: str = "") -> tuple[dict[str, Any], list[str]]:
    status = read_json(folder / "daily_pipeline_status.json", {})
    gaps: list[str] = []
    if not isinstance(status, dict):
        return {"status": "missing", "date": date, "steps": []}, ["daily_pipeline_status.json missing"]
    current_attempt = str(attempt_id or status.get("attempt_id") or "")
    raw_steps = status.get("steps") if isinstance(status.get("steps"), list) else []
    if current_attempt:
        raw_steps = [
            step for step in raw_steps if isinstance(step, dict) and str(step.get("attempt_id") or "") == current_attempt
        ]
    step_summary = []
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        name = str(step.get("name", ""))
        if private_hits(name):
            continue
        step_summary.append({"name": name, "status": str(step.get("status", ""))})
    for blocker in status.get("blockers", []):
        text = compact_text(blocker)
        if text and not private_hits(text):
            gaps.append("pipeline blocker: " + text)
    for warning in status.get("warnings", []):
        text = compact_text(warning)
        if text and not private_hits(text):
            gaps.append("pipeline warning: " + text)
    return {
        "status": status.get("attempt_status") or status.get("status", "unknown"),
        "run_id": status.get("run_id", ""),
        "attempt_id": current_attempt,
        "attempt_started_at": status.get("attempt_started_at", ""),
        "attempt_finished_at": status.get("attempt_finished_at", ""),
        "updated_at": status.get("updated_at", ""),
        "steps": step_summary,
    }, gaps


def build_zsxq_summary(
    folder: Path,
    compact: str,
    *,
    overnight_window_start: str = "",
    overnight_window_end: str = "",
    session: str = "premarket",
    attempt_started_at: str = "",
) -> tuple[dict[str, Any], list[str]]:
    path = folder / f"zsxq_scan_{compact}.json"
    payload = read_json(path, None)
    gaps: list[str] = []
    window_start = _window_timestamp(overnight_window_start) if overnight_window_start else None
    window_end = _window_timestamp(overnight_window_end) if overnight_window_end else None
    overnight_topic_count = 0
    if not isinstance(payload, dict):
        result = {
            "same_day_topic_count": 0,
            "summary_count": 0,
            "summaries": [],
            "source_refs": [],
        }
        if overnight_window_start and overnight_window_end:
            result.update({
                "overnight_window_start": overnight_window_start,
                "overnight_window_end": overnight_window_end,
                "overnight_topic_count": 0,
            })
        return result, [f"{path.name} missing"]
    if session == "close" and attempt_started_at:
        scanned_at = _window_timestamp(payload.get("scanned_at"))
        attempt_started = _window_timestamp(attempt_started_at)
        if not scanned_at or not attempt_started or scanned_at < attempt_started:
            # Never reuse a morning/previous-run Knowledge Star scan in the
            # close Bus input.  The market snapshot remains the critical path;
            # incomplete ZSXQ coverage is truthfully carried as a gap.
            return {
                "same_day_topic_count": 0,
                "summary_count": 0,
                "summaries": [],
                "source_refs": [],
            }, ["zsxq close scan not refreshed in this attempt; previous scan excluded"]
    summaries: list[dict[str, Any]] = []
    source_refs: list[dict[str, Any]] = []
    groups = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    if not groups:
        gaps.append("zsxq source data not obtained; empty sources is not evidence of no events")
    topic_rows_seen = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_ref = source_ref(
            "zsxq_api",
            group.get("group_id"),
            payload.get("scanned_at"),
            group_name=compact_text(group.get("group_name"), 80),
        )
        source_refs.append(group_ref)
        topics = group.get("full_text_topics") if isinstance(group.get("full_text_topics"), list) else []
        topic_rows_seen += len(topics)
        for item in topics:
            if not isinstance(item, dict):
                continue
            topic_id = str(item.get("topic_id") or "")
            if not topic_id:
                continue
            title = compact_text(item.get("title"), 100)
            content = str(item.get("content") or "")
            created_at = str(item.get("created_at") or "")
            created_timestamp = _window_timestamp(created_at)
            if window_start and window_end:
                if created_timestamp is None:
                    gaps.append(f"zsxq topic {topic_id} has no valid created_at for overnight window")
                    continue
                if not (window_start <= created_timestamp <= window_end):
                    continue
                overnight_topic_count += 1
            raw = "；".join(filter(None, [title, content]))
            if private_hits(raw):
                gaps.append(f"zsxq topic {topic_id} excluded by privacy filter")
                continue
            if len(summaries) >= MAX_ZSXQ_CARDS:
                continue
            summaries.append({
                "topic_id": topic_id,
                "group_id": str(item.get("group_id") or group.get("group_id") or ""),
                "created_at": created_at,
                "title": title,
                "summary": f"{title}；{content_summary(content)}",
                "source": "zsxq_api",
            })
    total = int(payload.get("same_day_topic_count") or 0)
    if topic_rows_seen == 0:
        gaps.append("zsxq source returned an empty topic array; event coverage is unknown")
    if total > len(summaries):
        gaps.append(f"zsxq summaries compressed/capped: {total} topics, {len(summaries)} cards")
    result = {
        "same_day_topic_count": total,
        "summary_count": len(summaries),
        "summaries": summaries,
        "source_refs": source_refs,
    }
    if overnight_window_start and overnight_window_end:
        result.update({
            "overnight_window_start": overnight_window_start,
            "overnight_window_end": overnight_window_end,
            "overnight_topic_count": overnight_topic_count,
        })
    return result, gaps


def build_fact_cards(folder: Path) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[str]]:
    payload = read_json(folder / "evidence_index.json", {})
    buckets = {"announcements_financial_ir": [], "market_funds": [], "other_public_facts": []}
    refs: list[dict[str, Any]] = []
    gaps: list[str] = []
    evidence = payload.get("evidence") if isinstance(payload, dict) else []
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, dict) or item.get("public_safe") is not True or item.get("private_related") is True:
            continue
        text = "；".join(filter(None, [str(item.get("event") or ""), str(item.get("market_fact") or "")]))
        if private_hits(text):
            gaps.append(f"evidence {item.get('evidence_id', '')} excluded by privacy filter")
            continue
        card = {
            "evidence_id": str(item.get("evidence_id") or ""),
            "date": str(item.get("date") or ""),
            "theme": compact_text(item.get("theme"), 80),
            "summary": compact_text(text, 360),
            "source": compact_text(item.get("source"), 120),
            "source_time": str(item.get("source_time") or ""),
            "source_ref": compact_text(item.get("source_ref"), 220),
            "confidence": str(item.get("confidence") or ""),
        }
        category_text = " ".join(str(item.get(key) or "") for key in ("theme", "source", "layer", "event"))
        category = "other_public_facts"
        if re.search(r"公告|业绩|财报|互动|调研|IR|earnings|announcement", category_text, re.I):
            category = "announcements_financial_ir"
        elif re.search(r"资金|杠杆|行情|宽度|成交|涨停|跌停|炸板|打板|情绪|板块|指数|market|fund|margin", category_text, re.I):
            category = "market_funds"
        buckets[category].append(card)
        refs.append(source_ref("evidence_index", card["evidence_id"], card["source_time"], source_ref=card["source_ref"]))
    return buckets, refs, gaps


def build_health_summary(folder: Path) -> tuple[dict[str, Any], list[str]]:
    health = read_json(folder / "data_source_health.json", {})
    gaps: list[str] = []
    endpoints = health.get("endpoints") if isinstance(health, dict) else []
    compact_endpoints: list[dict[str, Any]] = []
    for item in endpoints if isinstance(endpoints, list) else []:
        if not isinstance(item, dict):
            continue
        row = {
            "endpoint": str(item.get("endpoint") or ""),
            "ok": bool(item.get("ok")),
            "row_count": int(item.get("row_count") or 0),
            "source": compact_text(item.get("source"), 120),
        }
        compact_endpoints.append(row)
        if not row["ok"]:
            gaps.append(f"data source unavailable: {row['endpoint']}")
    return {"date": health.get("date", ""), "updated_at": health.get("updated_at", ""), "endpoints": compact_endpoints}, gaps


def _validate_premarket_market_facts(
    facts: dict[str, list[dict[str, Any]]],
    fund_summary: dict[str, Any],
    *,
    as_of_trading_date: str,
) -> None:
    """Reject A-share market facts that have been relabeled to the target date."""

    _, as_of = normalize_date(as_of_trading_date)
    for card in facts.get("market_funds", []):
        fact_date = str(card.get("date") or "").strip()
        if fact_date:
            try:
                _, fact_date = normalize_date(fact_date)
            except ValueError as exc:
                raise BusError("TIME_SEMANTICS_INVALID: market fact date is invalid") from exc
            if fact_date > as_of:
                raise BusError("TIME_SEMANTICS_INVALID: future-dated A-share market fact")
        source_time = _window_timestamp(card.get("source_time"))
        if source_time and source_time.date().isoformat() > as_of:
            raise BusError("TIME_SEMANTICS_INVALID: future-dated market source time")
    fund_date = str(fund_summary.get("date") or "").strip()
    if fund_date:
        try:
            _, fund_date = normalize_date(fund_date)
        except ValueError as exc:
            raise BusError("TIME_SEMANTICS_INVALID: fund summary date is invalid") from exc
        if fund_date > as_of:
            raise BusError("TIME_SEMANTICS_INVALID: future-dated fund summary")


def build_research_input(
    root: Path,
    date: str,
    *,
    session: str = "premarket",
    attempt_id: str = "",
    attempt_started_at: str = "",
    trading_context_path: Path | None = None,
    trading_context: dict[str, Any] | None = None,
    cutoff_at: str = "",
) -> dict[str, Any]:
    if session not in {"premarket", "close"}:
        raise BusError("research input session must be premarket or close")
    compact, dashed = normalize_date(date)
    folder = root / f"tmp_recap_{compact}"
    status, gaps = load_collection_status(folder, dashed, attempt_id)
    status = _public_collection_status(status)
    context = (
        build_premarket_context(root, dashed, trading_context_path=trading_context_path, trading_context=trading_context, cutoff_at=cutoff_at)
        if session == "premarket"
        else {
            "session": "close",
            "target_date": dashed,
            "as_of_trading_date": dashed,
            "market_snapshot_as_of": dashed,
            "public_only": True,
            "trading_context": resolve_trading_context(root, dashed, context_path=trading_context_path, context=trading_context, validated_at=cutoff_at or now()),
        }
    )
    if context.get("trading_context", {}).get("validation_status") != "validated":
        raise BusError(f"{context['trading_context'].get('error_code', 'TRADING_CALENDAR_MISMATCH')}:{context['trading_context'].get('error', 'context unavailable')}")
    if context["trading_context"].get("is_trading_day") is not True:
        raise BusError("TRADING_CONTEXT_NOT_TRADING_DAY")
    zsxq, zsxq_gaps = build_zsxq_summary(
        folder,
        compact,
        overnight_window_start=context.get("overnight_window_start", ""),
        overnight_window_end=context.get("overnight_window_end", ""),
        session=session,
        attempt_started_at=attempt_started_at,
    )
    facts, refs, fact_gaps = build_fact_cards(folder)
    health, health_gaps = build_health_summary(folder)
    preflight = read_json(folder / "data_preflight.json", {})
    if not isinstance(preflight, dict):
        preflight = {}
        gaps.append("data_preflight.json missing")
    gaps.extend(zsxq_gaps + fact_gaps + health_gaps)
    gaps.extend(str(item) for item in preflight.get("missing_or_incomplete", []) if item)
    for bucket in ("announcements_financial_ir", "market_funds", "other_public_facts"):
        if not facts[bucket]:
            gaps.append(f"{bucket} source data not obtained; empty array is not evidence of no events")
    fund = read_json(folder / "market_fund_leverage.json", {})
    fund_summary: dict[str, Any] = {}
    if isinstance(fund, dict):
        trend = fund.get("main_fund_trend") if isinstance(fund.get("main_fund_trend"), dict) else {}
        margin = fund.get("margin_leverage") if isinstance(fund.get("margin_leverage"), dict) else {}
        fund_summary = {
            "date": fund.get("date", ""),
            "source_time": (trend.get("latest") or {}).get("date", "") if isinstance(trend.get("latest"), dict) else "",
            "main_fund_trend": {
                key: trend.get(key)
                for key in ("net_5d", "net_10d", "positive_days_5d", "current_vs_5d_average", "current_rank_last5")
                if key in trend
            },
            "margin_leverage": {
                key: margin.get(key)
                for key in ("financing_net_5d", "financing_net_10d")
                if key in margin
            },
            "stress": {
                key: (margin.get("stress") or {}).get(key)
                for key in ("level", "decline_streak_days")
                if isinstance(margin.get("stress"), dict) and key in margin.get("stress", {})
            },
        }
        if session == "premarket" and not fund_summary["date"]:
            gaps.append("market_fund_leverage date missing; market snapshot as_of is unverified")
    else:
        gaps.append("market_fund_leverage.json missing")
    if session == "premarket":
        _validate_premarket_market_facts(facts, fund_summary, as_of_trading_date=context["as_of_trading_date"])
    expected = ("zsxq_scan", "evidence_index", "data_source_health", "market_fund_leverage")
    for name in expected:
        if not (folder / f"{name}_{compact}.json").exists() and not (folder / f"{name}.json").exists():
            if name == "zsxq_scan":
                continue
            gaps.append(f"{name}.json missing")
    payload = {
        "schema_version": RESEARCH_SCHEMA,
        "pipeline_version": "daily-pipeline-v1",
        "date": dashed,
        "updated_at": now(),
        "time_context": context,
        "public_only": True,
        "run_id": status.get("run_id", ""),
        "public_safe_for_private_bus": True,
        "not_for_public_feed": True,
        "collection_status": status,
        "knowledge_star": zsxq,
        "announcements_financial_ir": facts["announcements_financial_ir"],
        "market_funds": {
            "fact_cards": facts["market_funds"],
            "summary": fund_summary,
        },
        "other_public_facts": facts["other_public_facts"],
        "source_refs": refs,
        "data_gaps": sorted(set(gaps)),
    }
    assert_bus_safe(payload)
    return payload


def publish_research(
    root: Path,
    date: str,
    branch: str = BUS_BRANCH,
    *,
    session: str = "premarket",
    attempt_id: str = "",
    attempt_started_at: str = "",
    trading_context_path: Path | None = None,
    trading_context: dict[str, Any] | None = None,
    cutoff_at: str = "",
) -> dict[str, Any]:
    root = repo_root(root.resolve())
    recorded_attempt_started_at = attempt_started_at or now()
    payload = build_research_input(
        root,
        date,
        session=session,
        attempt_id=attempt_id,
        attempt_started_at=attempt_started_at,
        trading_context_path=trading_context_path,
        trading_context=trading_context,
        cutoff_at=cutoff_at,
    )
    if attempt_started_at and _timestamp(payload["updated_at"]) < _timestamp(attempt_started_at):
        raise BusError("research input updated_at predates the current collection attempt")
    prepared = _create_prepared_artifact(
        root,
        date,
        session,
        payload,
        attempt_id=attempt_id,
        attempt_started_at=recorded_attempt_started_at,
    )
    result = _publish_prepared_artifact(root, branch, prepared)
    result["updated_at"] = payload["updated_at"]
    return result


def validate_close_readiness(payload: dict[str, Any], date: str) -> None:
    _, dashed = normalize_date(date)
    if payload.get("schema_version") != CLOSE_READINESS_SCHEMA:
        raise BusError(f"readiness schema_version must be {CLOSE_READINESS_SCHEMA}")
    if payload.get("date") != dashed:
        raise BusError("close readiness date mismatch")
    if payload.get("public_safe_for_private_bus") is not True or payload.get("not_for_public_feed") is not True:
        raise BusError("close readiness privacy boundary is invalid")
    semantics = payload.get("session_semantics")
    if not isinstance(semantics, dict) or semantics.get("session") != "close":
        raise BusError("close readiness must declare close session semantics")
    if semantics.get("target_date") != dashed:
        raise BusError("close readiness target_date mismatch")
    if not isinstance(payload.get("errors"), list):
        raise BusError("close readiness errors must be a list")
    if not isinstance(payload.get("updated_at"), str) or not payload["updated_at"]:
        raise BusError("close readiness updated_at is required")
    for key in ("scheduler_1505", "collect_status", "research_input_publish"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise BusError(f"close readiness {key} is required")
    assert_bus_safe(payload)


def publish_close_readiness(
    root: Path,
    date: str,
    readiness: dict[str, Any],
    branch: str = BUS_BRANCH,
) -> dict[str, Any]:
    root = repo_root(root.resolve())
    compact, dashed = normalize_date(date)
    validate_close_readiness(readiness, dashed)
    relative = BUS_ROOT / "_status" / f"close-readiness-{compact}.json"
    with bus_worktree(root, branch) as worktree:
        target = worktree / relative
        write_json(target, readiness)
        changed = commit_and_push(worktree, branch, relative, f"bus: publish close readiness {compact}")
    return {
        "ok": True,
        "changed": changed,
        "branch": branch,
        "path": str(relative).replace("\\", "/"),
        "date": dashed,
    }


def default_decision(date: str, run_id: str = "") -> dict[str, Any]:
    pending = {
        "status": "pending",
        "received_at": None,
        "summary": "",
        "evidence_refs": [],
        "unknowns": [],
        "next_checks": [],
    }
    return {
        "schema_version": DECISION_SCHEMA,
        "pipeline_version": "daily-pipeline-v1",
        "date": date,
        "run_id": run_id,
        "updated_at": now(),
        "public_safe_for_private_bus": True,
        "not_for_public_feed": True,
        "role_boundary": {
            "judgement_owner": "ChatGPT",
            "execution_owner": "Codex",
            "independent_market_view": False,
            "public_feed_excludes_private": True,
        },
        "premarket": dict(pending),
        "close": dict(pending),
    }


def validate_decision(payload: dict[str, Any], date: str) -> None:
    if payload.get("schema_version") != DECISION_SCHEMA:
        raise BusError(f"decision schema_version must be {DECISION_SCHEMA}")
    if payload.get("date") != date:
        raise BusError("decision date mismatch")
    boundary = payload.get("role_boundary") or {}
    required = {
        "judgement_owner": "ChatGPT",
        "execution_owner": "Codex",
        "independent_market_view": False,
        "public_feed_excludes_private": True,
    }
    for key, expected in required.items():
        if boundary.get(key) != expected:
            raise BusError(f"decision role_boundary.{key} must be {expected!r}")
    for stage in ("premarket", "close"):
        if not isinstance(payload.get(stage), dict):
            raise BusError(f"decision missing {stage} section")
    assert_bus_safe(payload)


def read_remote_decision(worktree: Path, date: str) -> dict[str, Any]:
    _, dashed = normalize_date(date)
    path = worktree / BUS_ROOT / dashed / "chatgpt-decision.json"
    payload = read_json(path, None)
    if not isinstance(payload, dict):
        raise BusError(f"missing or invalid bus decision: {path}")
    validate_decision(payload, dashed)
    return payload


def update_decision(root: Path, date: str, stage: str, source: Path, branch: str = BUS_BRANCH) -> dict[str, Any]:
    root = repo_root(root.resolve())
    compact, dashed = normalize_date(date)
    incoming = read_json(source.resolve(), None)
    if not isinstance(incoming, dict):
        raise BusError(f"decision input must be a JSON object: {source}")
    with bus_worktree(root, branch) as worktree:
        relative = BUS_ROOT / dashed / "chatgpt-decision.json"
        target = worktree / relative
        existing = read_json(target, {})
        decision = existing if isinstance(existing, dict) and existing else default_decision(dashed)
        if decision.get("schema_version") != DECISION_SCHEMA:
            raise BusError("existing bus decision has an incompatible schema")
        fragment = incoming.get(stage) if isinstance(incoming.get(stage), dict) else incoming
        if not isinstance(fragment, dict):
            raise BusError(f"decision {stage} input must be an object")
        decision[stage] = fragment
        if isinstance(incoming.get("role_boundary"), dict):
            decision["role_boundary"] = incoming["role_boundary"]
        if "run_id" in incoming and incoming.get("run_id"):
            decision["run_id"] = incoming["run_id"]
        decision["date"] = dashed
        decision["updated_at"] = now()
        validate_decision(decision, dashed)
        write_json(target, decision)
        changed = commit_and_push(worktree, branch, relative, f"bus: update {stage} decision {dashed}")
    return {"ok": True, "changed": changed, "branch": branch, "path": str(relative).replace("\\", "/"), "stage": stage, "date": dashed}


def pull_decision(root: Path, date: str, branch: str = BUS_BRANCH) -> dict[str, Any]:
    root = repo_root(root.resolve())
    compact, dashed = normalize_date(date)
    if not fetch_bus(root, branch):
        raise BusError(f"bus branch does not exist: {branch}")
    relative = BUS_ROOT / dashed / "chatgpt-decision.json"
    text = git_checked(root, "show", f"origin/{branch}:{str(relative).replace(chr(92), '/')}")
    try:
        decision = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusError(f"invalid decision JSON on bus: {exc}") from exc
    if not isinstance(decision, dict):
        raise BusError("decision on bus must be a JSON object")
    validate_decision(decision, dashed)
    folder = root / f"tmp_recap_{compact}"
    folder.mkdir(parents=True, exist_ok=True)
    status = read_json(folder / "daily_pipeline_status.json", {})
    existing = read_json(folder / "chatgpt_codex_handoff.json", {})
    run_id = run_id_for_date(status, dashed) or run_id_for_date(existing, dashed) or str(decision.get("run_id") or f"dpv1-{compact}-bus")
    payload = default_handoff(dashed, run_id)
    if isinstance(existing, dict):
        payload = merge_handoff(payload, existing)
    incoming = {
        key: value
        for key, value in decision.items()
        if key not in {"schema_version", "pipeline_version", "date", "run_id", "updated_at", "public_safe_for_private_bus", "not_for_public_feed"}
    }
    for stage in ("premarket", "close"):
        current = payload.get(stage) if isinstance(payload.get(stage), dict) else {}
        candidate = incoming.get(stage)
        if isinstance(candidate, dict) and current.get("status") == "received" and candidate.get("status") == "pending":
            incoming.pop(stage, None)
    payload = merge_handoff(payload, incoming)
    write_json(folder / "chatgpt_codex_handoff.json", payload)
    handoff_path, handoff, errors = update_handoff(folder, dashed, run_id, None, [])
    return {
        "ok": True,
        "branch": branch,
        "path": str(relative).replace("\\", "/"),
        "handoff": str(handoff_path),
        "handoff_status": handoff.get("handoff_status"),
        "errors": errors,
        "date": dashed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AlphaEngine Daily Research Bus")
    sub = parser.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publish-research")
    publish.add_argument("date")
    publish.add_argument("--root", default=".")
    publish.add_argument("--bus-branch", default=BUS_BRANCH)
    publish.add_argument("--session", choices=["premarket", "close"], default="premarket")
    publish.add_argument("--attempt-id", default="")
    publish.add_argument("--attempt-started-at", default="")
    publish.add_argument("--trading-context", default="", help="explicit validated local trading_context_v1 artifact")
    publish.add_argument("--cutoff-at", default="")
    prepared = sub.add_parser("publish-prepared-research")
    prepared.add_argument("prepared_artifact")
    prepared.add_argument("--root", default=".")
    prepared.add_argument("--bus-branch", default=BUS_BRANCH)
    prepared.add_argument("--manifest", default="")
    update = sub.add_parser("update-decision")
    update.add_argument("date")
    update.add_argument("--stage", choices=["premarket", "close"], required=True)
    update.add_argument("--input", required=True)
    update.add_argument("--root", default=".")
    update.add_argument("--bus-branch", default=BUS_BRANCH)
    pull = sub.add_parser("pull-decision")
    pull.add_argument("date")
    pull.add_argument("--root", default=".")
    pull.add_argument("--bus-branch", default=BUS_BRANCH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "publish-research":
            result = publish_research(
                Path(args.root),
                args.date,
                args.bus_branch,
                session=args.session,
                attempt_id=args.attempt_id,
                attempt_started_at=args.attempt_started_at,
                trading_context_path=context_path_for_root(Path(args.root), args.trading_context),
                cutoff_at=args.cutoff_at,
            )
        elif args.command == "publish-prepared-research":
            result = publish_prepared_research(
                Path(args.root),
                Path(args.prepared_artifact),
                args.bus_branch,
                manifest_path=Path(args.manifest).resolve() if args.manifest else None,
            )
        elif args.command == "update-decision":
            result = update_decision(Path(args.root), args.date, args.stage, Path(args.input), args.bus_branch)
        else:
            result = pull_decision(Path(args.root), args.date, args.bus_branch)
    except (BusError, FileNotFoundError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
