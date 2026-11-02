#!/usr/bin/env python3
"""Thin Daily Pipeline v1 coordinator.

This entrypoint coordinates the existing daily-recap-trading scripts.  It does
not write market conclusions, choose themes, or upload files to IMA itself.
ChatGPT owns research judgement; Codex owns collection, normalization,
holdings mapping, receipt checking, and the final public-feed handoff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from trading_context_v1 import DEFAULT_CONTEXT_RELATIVE, context_path_for_root


PIPELINE_VERSION = "daily-pipeline-v1"
HANDOFF_SCHEMA = "daily-chatgpt-codex-handoff-v1"
STATUS_SCHEMA = "daily-pipeline-status-v1"
SCRIPT_DIR = Path(__file__).resolve().parent.parent


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_date(raw: str) -> tuple[str, str]:
    compact = raw.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError("date must be YYYYMMDD or YYYY-MM-DD")
    return compact, f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path | None, default: Any = None) -> Any:
    if not path or not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def default_handoff(date: str, run_id: str) -> dict[str, Any]:
    pending = {
        "status": "pending",
        "received_at": None,
        "summary": "",
        "evidence_refs": [],
        "unknowns": [],
        "next_checks": [],
    }
    return {
        "schema_version": HANDOFF_SCHEMA,
        "pipeline_version": PIPELINE_VERSION,
        "date": date,
        "run_id": run_id,
        "created_at": now(),
        "updated_at": now(),
        "handoff_status": "template",
        "premarket": dict(pending),
        "close": dict(pending),
        "role_boundary": {
            "judgement_owner": "ChatGPT",
            "execution_owner": "Codex",
            "independent_market_view": False,
            "private_plan_uses_real_holdings": True,
            "public_feed_excludes_private": True,
        },
        "artifacts": {
            "public_report": "",
            "private_plan": "",
            "ima_receipt": "",
            "required_judgement_artifacts": [
                "daily_mainline_decision_state.json",
                "prior_day_signal_validation.json",
                "same_day_market_validation.json",
            ],
            "collected": [],
        },
        "operator_notes": [],
    }


def merge_handoff(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge a user-provided handoff without dropping unknown research fields."""

    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            child = dict(merged[key])
            child.update(value)
            merged[key] = child
        else:
            merged[key] = value
    return merged


def handoff_errors(payload: dict[str, Any], date: str) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != HANDOFF_SCHEMA:
        errors.append("handoff schema_version is not daily-chatgpt-codex-handoff-v1")
    if payload.get("date") != date:
        errors.append("handoff date mismatch")
    boundary = payload.get("role_boundary") or {}
    if boundary.get("judgement_owner") != "ChatGPT":
        errors.append("handoff judgement_owner must be ChatGPT")
    if boundary.get("execution_owner") != "Codex":
        errors.append("handoff execution_owner must be Codex")
    if boundary.get("independent_market_view") is not False:
        errors.append("handoff permits an independent market view")
    if boundary.get("public_feed_excludes_private") is not True:
        errors.append("handoff must exclude private data from the public feed")
    for name in ("premarket", "close"):
        section = payload.get(name)
        if not isinstance(section, dict):
            errors.append(f"handoff missing {name} section")
            continue
        status = section.get("status", "pending")
        if status not in {"pending", "received", "not_applicable"}:
            errors.append(f"handoff {name}.status is invalid")
        if status == "received" and not any(
            section.get(key) not in (None, "", [], {}) for key in ("summary", "conclusion", "text")
        ):
            errors.append(f"handoff {name} is received but has no conclusion text")
    return errors


def update_handoff(
    folder: Path,
    date: str,
    run_id: str,
    source: Path | None,
    collected: Iterable[str],
) -> tuple[Path, dict[str, Any], list[str]]:
    target = folder / "chatgpt_codex_handoff.json"
    payload = default_handoff(date, run_id)
    incoming_errors: list[str] = []
    existing = read_json(target, {})
    if isinstance(existing, dict):
        payload = merge_handoff(payload, existing)
    if source is not None:
        incoming = read_json(source, None)
        if not isinstance(incoming, dict):
            raise ValueError(f"handoff must be a JSON object: {source}")
        if incoming.get("date") not in (None, date):
            incoming_errors.append("imported handoff date mismatch")
        payload = merge_handoff(payload, incoming)
        payload["imported_from"] = str(source.resolve())
    payload["schema_version"] = HANDOFF_SCHEMA
    payload["pipeline_version"] = PIPELINE_VERSION
    payload["date"] = date
    payload["run_id"] = run_id
    payload["updated_at"] = now()
    artifacts = payload.setdefault("artifacts", {})
    old_collected = artifacts.get("collected") if isinstance(artifacts, dict) else []
    artifacts["collected"] = sorted({str(item) for item in (old_collected or [])} | set(collected))
    errors = incoming_errors + handoff_errors(payload, date)
    premarket = payload.get("premarket") or {}
    close = payload.get("close") or {}
    if errors:
        payload["handoff_status"] = "invalid"
    elif premarket.get("status") == "received" and close.get("status") == "received":
        payload["handoff_status"] = "ready_for_delivery"
    elif premarket.get("status") == "received" or close.get("status") == "received":
        payload["handoff_status"] = "partial"
    else:
        payload["handoff_status"] = "template"
    write_json(target, payload)
    return target, payload, errors


def find_holding_input(folder: Path, compact: str, explicit: Path | None = None) -> Path | None:
    candidates = [
        explicit,
        folder / f"holdings_input_{compact}.json",
        folder / "current_run" / "holdings_input.json",
    ]
    return next((path.resolve() for path in candidates if path and path.is_file()), None)


def find_latest(folder: Path, pattern: str) -> Path | None:
    matches = sorted(folder.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def artifact_map(folder: Path, compact: str, root: Path) -> dict[str, Any]:
    names = {
        "pipeline_status": folder / "daily_pipeline_status.json",
        "handoff": folder / "chatgpt_codex_handoff.json",
        "preflight": folder / "data_preflight.json",
        "market_health": folder / "data_source_health.json",
        "market_fund_leverage": folder / "market_fund_leverage.json",
        "zsxq": folder / f"zsxq_scan_{compact}.json",
        "evidence": folder / "evidence_index.json",
        "market_discovery": folder / "daily_market_discovery.json",
        "market_questions": folder / "daily_market_questions.json",
        "mainline_state": folder / "daily_mainline_decision_state.json",
        "holdings_review": folder / "holdings_event_review.json",
        "holdings_matrix": folder / "holdings_decision_matrix.json",
        "public_feed_receipt": folder / "public_feed_publish_status.json",
        "delivery_gate": folder / "market_intelligence_delivery_gate_v4.9.2.json",
    }
    ima = find_latest(folder, f"ima_upload_results_{compact}*.json")
    if ima:
        names["ima_receipt"] = ima
    reports = sorted(folder.glob("*.md"))
    public_report = next((path for path in reports if "每日市场与语料研判" in path.name), None)
    private_plan = next((path for path in reports if "交易计划" in path.name and "持仓事件审查" not in path.name), None)
    if public_report:
        names["public_report"] = public_report
    if private_plan:
        names["private_plan"] = private_plan
    return {
        key: (relative_to_root(path, root) if isinstance(path, Path) and path.exists() else None)
        for key, path in names.items()
    }


def make_status(root: Path, folder: Path, date: str, run_id: str, handoff_path: Path) -> dict[str, Any]:
    return {
        "schema_version": STATUS_SCHEMA,
        "pipeline_version": PIPELINE_VERSION,
        "date": date,
        "session": "premarket",
        "public_only": False,
        "run_id": run_id,
        "created_at": now(),
        "updated_at": now(),
        "status": "running",
        "role_boundary": {
            "judgement_owner": "ChatGPT",
            "execution_owner": "Codex",
            "independent_market_view": False,
            "private_plan_excluded_from_public_feed": True,
        },
        "handoff": {"path": relative_to_root(handoff_path, root), "status": "template", "errors": []},
        "steps": [],
        "artifacts": {},
        "publication": {
            "ima": "not_attempted",
            "public_feed": "not_attempted",
            "publish_requested": False,
        },
        "attempt_id": "",
        "attempt_started_at": "",
        "attempt_finished_at": "",
        "attempt_status": "",
        "blockers": [],
        "warnings": [],
    }


def run_id_for_date(payload: Any, date: str) -> str | None:
    if not isinstance(payload, dict) or payload.get("date") not in (None, date):
        return None
    value = payload.get("run_id")
    return value if isinstance(value, str) and value else None


def load_or_create_status(
    path: Path,
    root: Path,
    folder: Path,
    date: str,
    run_id: str,
    handoff_path: Path,
) -> dict[str, Any]:
    existing = read_json(path, {})
    if not isinstance(existing, dict) or existing.get("date") != date:
        return make_status(root, folder, date, run_id, handoff_path)
    template = make_status(root, folder, date, run_id, handoff_path)
    for key, value in template.items():
        existing.setdefault(key, value)
    existing["schema_version"] = STATUS_SCHEMA
    existing["pipeline_version"] = PIPELINE_VERSION
    existing["date"] = date
    existing["run_id"] = run_id
    return existing


def reset_current_blockers(status: dict[str, Any]) -> None:
    """Blockers describe the current attempt; steps retain prior attempts."""

    status["blockers"] = []


def persist_status(status: dict[str, Any], path: Path) -> None:
    status["updated_at"] = now()
    write_json(path, status)


def add_step(status: dict[str, Any], name: str, state: str, detail: str = "", **extra: Any) -> None:
    row: dict[str, Any] = {"name": name, "status": state, "detail": detail, "updated_at": now()}
    if status.get("attempt_id"):
        row["attempt_id"] = status["attempt_id"]
    row.update(extra)
    status["steps"].append(row)


CLOSE_FRESH_ARTIFACTS = (
    "data_source_health.json",
    "evidence_index.json",
    "daily_market_discovery.json",
)


def _artifact_snapshot(folder: Path) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for name in CLOSE_FRESH_ARTIFACTS:
        path = folder / name
        if not path.is_file():
            snapshot[name] = {"exists": False, "sha256": "", "mtime_ns": 0}
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot[name] = {
            "exists": True,
            "sha256": digest,
            "mtime_ns": path.stat().st_mtime_ns,
        }
    return snapshot


def _parse_iso_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_close_freshness(
    folder: Path,
    target_date: str,
    attempt_started_at: str,
    baseline: dict[str, dict[str, Any]],
    *,
    allow_reuse: bool = False,
) -> tuple[bool, dict[str, str], list[str]]:
    """Fail closed if close evidence is absent, stale, or relabeled.

    The result intentionally contains artifact names only.  It is safe to
    persist in the public collection status and proves that the morning bundle
    was not silently reused by the close handoff.
    """

    if allow_reuse:
        return True, {name: "reused_for_controlled_smoke" for name in CLOSE_FRESH_ARTIFACTS}, []
    _, dashed = normalize_date(target_date)
    started = _parse_iso_timestamp(attempt_started_at)
    current = _artifact_snapshot(folder)
    states: dict[str, str] = {}
    errors: list[str] = []
    for name in CLOSE_FRESH_ARTIFACTS:
        row = current[name]
        before = baseline.get(name, {"exists": False, "sha256": "", "mtime_ns": 0})
        if not row["exists"]:
            states[name] = "missing"
            errors.append(f"{name} missing after close attempt")
            continue
        if before.get("exists") and row["sha256"] == before.get("sha256"):
            states[name] = "stale"
            errors.append(f"{name} was not regenerated by close attempt")
            continue
        payload = read_json(folder / name, {})
        if not isinstance(payload, dict):
            states[name] = "invalid"
            errors.append(f"{name} is not a JSON object")
            continue
        fact_date = str(payload.get("date") or "")
        try:
            _, fact_date = normalize_date(fact_date)
        except ValueError:
            states[name] = "wrong_date"
            errors.append(f"{name} date is invalid")
            continue
        if fact_date != dashed:
            states[name] = "wrong_date"
            errors.append(f"{name} date is not target trading date")
            continue
        if name == "data_source_health.json" and str(payload.get("as_of_trading_date") or dashed) != dashed:
            states[name] = "wrong_market_date"
            errors.append("data_source_health.json as_of_trading_date is not close date")
            continue
        generated = _parse_iso_timestamp(
            payload.get("generated_at") or payload.get("created_at") or payload.get("updated_at")
        )
        if not generated or not started or generated < started:
            states[name] = "stale_timestamp"
            errors.append(f"{name} timestamp predates close attempt")
            continue
        states[name] = "fresh"
    return not errors, states, errors


def script_path(name: str) -> Path:
    return SCRIPT_DIR / name


def run_existing(
    status: dict[str, Any],
    status_path: Path,
    root: Path,
    name: str,
    args: list[str],
    *,
    required: bool = True,
    env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
) -> bool:
    command = [sys.executable, str(script_path(name)), *args]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**dict(os.environ), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **(env or {})},
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        detail = f"timeout after {timeout_seconds}s"
        state = "failed" if required else "warning"
        add_step(
            status,
            name,
            state,
            detail,
            returncode=None,
            command=[Path(command[0]).name, Path(command[1]).name, *command[2:]],
            required=required,
        )
        persist_status(status, status_path)
        if required:
            status["blockers"].append(f"{name} timed out")
        else:
            status["warnings"].append(f"{name} timed out; source gap retained")
        return False
    detail = (completed.stdout or completed.stderr or "").strip()
    if len(detail) > 800:
        detail = detail[-800:]
    state = "passed" if completed.returncode == 0 else ("failed" if required else "warning")
    add_step(
        status,
        name,
        state,
        detail,
        returncode=completed.returncode,
        command=[Path(command[0]).name, Path(command[1]).name, *command[2:]],
        required=required,
    )
    persist_status(status, status_path)
    if completed.returncode != 0 and required:
        status["blockers"].append(f"{name} returned {completed.returncode}")
    elif completed.returncode != 0:
        status["warnings"].append(f"{name} returned {completed.returncode}; source gap retained")
    return completed.returncode == 0


def stage_explicit_holdings(folder: Path, compact: str, holding_input: Path | None, explicit: str) -> Path | None:
    """Put an explicitly supplied broker export where legacy collectors expect it."""

    if not holding_input or not explicit:
        return holding_input
    target = folder / f"holdings_input_{compact}.json"
    if holding_input.resolve() != target.resolve():
        shutil.copy2(holding_input, target)
        return target
    return holding_input


def collect_stage(
    status: dict[str, Any],
    status_path: Path,
    root: Path,
    date: str,
    compact: str,
    holding_input: Path | None,
    args: argparse.Namespace,
) -> None:
    if args.from_existing:
        add_step(status, "collection", "skipped", "--from-existing: reuse dated local artifacts")
        persist_status(status, status_path)
        return

    if args.data_only:
        # ``data-only`` is still a public-only run, but close collection must
        # refresh the complete public evidence path before the Bus handoff.
        # Holdings are excluded by ``isolated_public_run`` in main().
        public_collection_args = ["--public-only"]
        market_date_args = ["--as-of-trading-date", args.as_of_trading_date]
        zsxq_window_args: list[str] = []
        if args.session == "premarket":
            zsxq_window_args = [
                "--window-start", args.overnight_window_start,
                "--window-end", args.overnight_window_end,
            ]
        run_existing(
            status,
            status_path,
            root,
            "init_evidence_workspace.py",
            [date, "--root", str(root)],
        )
        run_existing(
            status,
            status_path,
            root,
            "data_preflight.py",
            [date, "--root", str(root), *public_collection_args, *market_date_args]
            + (["--cache-root", str(args.kline_cache)] if args.kline_cache else []),
            required=False,
        )
        run_existing(
            status,
            status_path,
            root,
            "data_source_health_v41.py",
            [date, "--root", str(root), *public_collection_args, *market_date_args],
            required=False,
        )
        run_existing(
            status,
            status_path,
            root,
            "market_fund_leverage.py",
            [date, "--root", str(root), "--refresh", *market_date_args],
            required=False,
        )
        if not args.skip_zsxq:
            run_existing(
                status,
                status_path,
                root,
                "scan_zsxq_incremental.py",
                [compact, "--root", str(root), "--max-details", str(args.max_zsxq_details), *zsxq_window_args],
                required=False,
                timeout_seconds=30 if args.session == "close" else None,
            )
        else:
            add_step(status, "scan_zsxq_incremental.py", "skipped", "details skipped; coverage gap retained")
            status["warnings"].append("Knowledge Star close details were not collected; coverage gap retained")
            persist_status(status, status_path)
        if holding_input:
            # Defensive invariant: even a deliberately supplied holdings file
            # cannot enter the data-only/public-only branch.
            add_step(status, "holdings_collectors", "skipped", "public-only data-only run ignores holdings input")
        else:
            add_step(status, "holdings_collectors", "skipped", "public-only data-only run")
        persist_status(status, status_path)
        return

    public_collection_args: list[str] = []
    market_date_args: list[str] = []
    zsxq_window_args: list[str] = []
    if args.public_only and args.session == "premarket":
        public_collection_args = ["--public-only"]
        market_date_args = ["--as-of-trading-date", args.as_of_trading_date]
        zsxq_window_args = [
            "--window-start", args.overnight_window_start,
            "--window-end", args.overnight_window_end,
        ]

    run_existing(status, status_path, root, "init_evidence_workspace.py", [date, "--root", str(root)])
    run_existing(
        status,
        status_path,
        root,
        "data_preflight.py",
        [date, "--root", str(root), *public_collection_args, *market_date_args]
        + (["--cache-root", str(args.kline_cache)] if args.kline_cache else []),
        required=False,
    )
    run_existing(
        status,
        status_path,
        root,
        "data_source_health_v41.py",
        [date, "--root", str(root), *public_collection_args, *market_date_args],
        required=False,
    )
    run_existing(
        status,
        status_path,
        root,
        "market_fund_leverage.py",
        [date, "--root", str(root), "--refresh", *market_date_args],
        required=False,
    )
    if not args.skip_zsxq:
        run_existing(
            status,
            status_path,
            root,
            "scan_zsxq_incremental.py",
            [compact, "--root", str(root), "--max-details", str(args.max_zsxq_details), *zsxq_window_args],
            required=False,
        )
    else:
        add_step(status, "scan_zsxq_incremental.py", "skipped", "--skip-zsxq")
        persist_status(status, status_path)
    if holding_input:
        run_existing(status, status_path, root, "fetch_holdings_announcements.py", [date, "--root", str(root)])
        run_existing(status, status_path, root, "fetch_holdings_irm.py", [date, "--root", str(root)])
    else:
        add_step(status, "holdings_collectors", "skipped", "no holdings input found")
        persist_status(status, status_path)


def prepare_stage(
    status: dict[str, Any],
    status_path: Path,
    root: Path,
    date: str,
    compact: str,
    folder: Path,
    holding_input: Path | None,
    args: argparse.Namespace,
) -> None:
    run_existing(status, status_path, root, "build_daily_research_evidence.py", [date, "--root", str(root)])
    run_existing(status, status_path, root, "augment_breadth_emotion_evidence.py", [date, "--root", str(root)], required=False)
    run_existing(status, status_path, root, "daily_market_discovery.py", [date, "--root", str(root)])
    run_existing(status, status_path, root, "daily_market_questions.py", [date, "--root", str(root)])

    # The following scripts only normalize private source material and map
    # positions.  They do not generate a second market conclusion.
    if holding_input:
        review = read_json(folder / "holdings_event_review.json", {})
        if not review.get("review_complete"):
            run_existing(
                status,
                status_path,
                root,
                "holdings_event_review.py",
                ["prepare", date, "--root", str(root), "--holdings", str(holding_input)],
            )
            run_existing(status, status_path, root, "enrich_holdings_event_review.py", [date, "--root", str(root)])
        else:
            add_step(status, "holdings_event_review.py", "skipped", "existing review_complete=true preserved")
            persist_status(status, status_path)
        run_existing(status, status_path, root, "holdings_event_review.py", ["check", date, "--root", str(root)])
        matrix_args = ["build", date, "--root", str(root)]
        if args.kline_cache:
            matrix_args.extend(["--kline-cache", str(args.kline_cache)])
        run_existing(status, status_path, root, "holdings_decision_matrix.py", matrix_args)
    else:
        add_step(status, "holdings_mapping", "skipped", "no holdings input found")
        persist_status(status, status_path)

    # These are judgement-owned artifacts.  Do not invoke the legacy builder
    # here; the handoff records that ChatGPT/Codex must supply them.
    expected = [
        "daily_mainline_decision_state.json",
        "prior_day_signal_validation.json",
        "same_day_market_validation.json",
    ]
    missing = [name for name in expected if not (folder / name).exists()]
    if args.public_only or args.data_only:
        # Public prepare ends at research-input.  ``bus_v1.collect`` uses
        # ``--data-only`` for close, while the premarket path uses
        # ``--public-only``; both are deliberately stopped at the same
        # ChatGPT-owned judgement boundary.  Missing judgement artifacts are
        # therefore not a collector failure for either public handoff.
        add_step(
            status,
            "judgement_handoff_boundary",
            "passed",
            "public-only prepare stops before judgement-owned artifacts",
            missing=[],
            wait_reason="",
        )
    else:
        if missing:
            status["warnings"].append("judgement-owned artifacts pending: " + ", ".join(missing))
        add_step(
            status,
            "judgement_handoff_boundary",
            "passed" if not missing else "waiting",
            "Codex does not generate market conclusions",
            missing=missing,
            wait_reason="handoff_or_artifacts" if missing else "",
        )
    persist_status(status, status_path)


def ima_receipt_state(folder: Path, compact: str) -> tuple[Path | None, bool, str]:
    ima = find_latest(folder, f"ima_upload_results_{compact}*.json")
    if not ima:
        return None, False, "IMA receipt is missing"
    payload = read_json(ima, None)
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return ima, False, "IMA receipt ok is not true"
    rows = payload.get("rows")
    if isinstance(rows, list) and len(rows) >= 2 and not all(
        isinstance(row, dict) and row.get("ok") is True for row in rows[:2]
    ):
        return ima, False, "IMA upload or same-attempt readback did not pass"
    return ima, True, "IMA upload and readback passed"


def update_publication_state(
    status: dict[str, Any],
    folder: Path,
    compact: str,
    publish_requested: bool,
    publish_attempted: bool = False,
) -> None:
    ima, ima_ok, _ = ima_receipt_state(folder, compact)
    feed = read_json(folder / "public_feed_publish_status.json", {})
    gate = read_json(folder / "market_intelligence_delivery_gate_v4.9.2.json", {})
    status["publication"] = {
        "ima": "passed" if ima_ok else ("pending" if not ima else "failed"),
        "public_feed": "published" if feed.get("public_feed_published") is True or feed.get("status") == "published" else (feed.get("status") or "not_attempted"),
        "delivery_gate": gate.get("status") or "not_attempted",
        "publish_requested": publish_requested,
        "receipt_scope": "current_attempt" if publish_attempted else "observed_existing",
    }


def delivery_stage(
    status: dict[str, Any],
    status_path: Path,
    root: Path,
    date: str,
    compact: str,
    folder: Path,
    handoff: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    errors = handoff_errors(handoff, date)
    status["handoff"] = {
        "path": "tmp_recap_" + compact + "/chatgpt_codex_handoff.json",
        "status": handoff.get("handoff_status"),
        "errors": errors,
    }
    if errors:
        status["blockers"].extend(errors)
    if handoff.get("handoff_status") != "ready_for_delivery":
        status["warnings"].append("ChatGPT/Codex handoff is not ready_for_delivery")
    artifacts = artifact_map(folder, compact, root)
    required = ["public_report", "private_plan"]
    missing = [key for key in required if not artifacts.get(key)]
    if missing:
        status["warnings"].append("delivery prerequisites pending: " + ", ".join(missing))
    _, ima_ok, ima_reason = ima_receipt_state(folder, compact)
    if not ima_ok:
        status["blockers"].append("delivery blocked: " + ima_reason)
    ready = not errors and handoff.get("handoff_status") == "ready_for_delivery" and not missing and ima_ok
    if not args.publish:
        add_step(
            status,
            "delivery_gate",
            "waiting" if ready else "blocked",
            "use --publish after IMA readback" if ready else "delivery prerequisites are incomplete",
            wait_reason="publish_confirmation" if ready else "handoff_or_artifacts",
        )
        update_publication_state(status, folder, compact, False, False)
        persist_status(status, status_path)
        return
    if not ready:
        add_step(status, "delivery_gate", "blocked", "publish not attempted")
        update_publication_state(status, folder, compact, True, False)
        persist_status(status, status_path)
        return
    run_existing(status, status_path, root, "market_intelligence_delivery_gate_v490.py", [date, "--root", str(root), "--stage", "delivery"], required=True)
    update_publication_state(status, folder, compact, True, True)
    persist_status(status, status_path)


def finalize_status(
    status: dict[str, Any],
    status_path: Path,
    folder: Path,
    compact: str,
    root: Path,
    *,
    public_only: bool = False,
) -> None:
    status["artifacts"] = {} if public_only else artifact_map(folder, compact, root)
    if status["blockers"]:
        status["status"] = "blocked"
    elif any(step.get("status") == "waiting" and step.get("wait_reason") == "handoff_or_artifacts" for step in status["steps"]):
        status["status"] = "awaiting_handoff"
    elif any(step.get("status") == "waiting" and step.get("wait_reason") == "publish_confirmation" for step in status["steps"]):
        status["status"] = "ready_to_publish"
    elif status["publication"].get("publish_requested") and status["publication"].get("public_feed") == "published":
        status["status"] = "published"
    elif status["publication"].get("publish_requested") and status["publication"].get("ima") == "passed":
        status["status"] = "ima_complete_public_feed_pending"
    elif status["warnings"]:
        status["status"] = "partial"
    else:
        status["status"] = "completed"
    if status.get("attempt_id"):
        status["attempt_status"] = status["status"]
        status["attempt_finished_at"] = now()
    persist_status(status, status_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AlphaEngine Daily Pipeline v1 thin coordinator")
    parser.add_argument("date", help="Actual trading date, YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--root", default=".", help="AlphaEngine root")
    parser.add_argument("--stage", choices=["collect", "prepare", "delivery", "full"], default="full")
    parser.add_argument("--session", choices=["premarket", "close"], default="premarket")
    parser.add_argument("--handoff", default="", help="Optional ChatGPT/Codex handoff JSON to import")
    parser.add_argument("--holdings", default="", help="Optional holdings JSON; otherwise use the dated/current_run input")
    parser.add_argument("--kline-cache", default="", help="Optional validated K-line cache directory")
    parser.add_argument("--max-zsxq-details", type=int, default=0)
    parser.add_argument("--skip-zsxq", action="store_true")
    parser.add_argument("--from-existing", action="store_true", help="Skip collectors and reuse the dated local bundle")
    parser.add_argument(
        "--data-only",
        action="store_true",
        help="Run only the public collection/prepare path; never process local position inputs",
    )
    parser.add_argument(
        "--public-only",
        action="store_true",
        help="Run the public premarket input path without reading or mapping local position inputs",
    )
    parser.add_argument(
        "--fresh-attempt",
        action="store_true",
        help="Start a new isolated collection attempt instead of retaining prior attempt steps",
    )
    parser.add_argument("--attempt-id", default="", help="Stable identifier for the current isolated collection attempt")
    parser.add_argument("--attempt-started-at", default="", help="UTC timestamp recorded when the current attempt began")
    parser.add_argument("--cutoff-at", default="", help="Optional fixed Asia/Shanghai premarket cutoff for controlled smoke tests")
    parser.add_argument("--publish", action="store_true", help="Run the existing delivery gate and public-feed publisher")
    parser.add_argument("--bus-branch", default="daily-bus", help="GitHub branch used for the private daily handoff bus")
    parser.add_argument("--bus-publish-research", action="store_true", help="Publish the compressed research input to the daily bus")
    parser.add_argument("--bus-pull-decision", action="store_true", help="Pull and import the dated ChatGPT decision from the daily bus")
    parser.add_argument("--trading-context", default="", help="explicit validated local trading_context_v1 artifact")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.data_only and args.stage not in {"collect", "prepare"}:
        print(json.dumps({"ok": False, "error": "--data-only only supports --stage collect or prepare"}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.data_only and any((args.handoff, args.holdings, args.publish, args.bus_publish_research, args.bus_pull_decision)):
        print(json.dumps({"ok": False, "error": "--data-only does not accept private or downstream inputs"}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.public_only and args.holdings:
        print(json.dumps({"ok": False, "error": "--public-only does not accept --holdings"}, ensure_ascii=False), file=sys.stderr)
        return 2
    compact, date = normalize_date(args.date)
    root = Path(args.root).resolve()
    folder = root / f"tmp_recap_{compact}"
    folder.mkdir(parents=True, exist_ok=True)
    if args.session == "premarket" and args.public_only:
        from daily_research_bus import BusError, build_premarket_context

        try:
            premarket_context = build_premarket_context(
                root,
                date,
                trading_context_path=context_path_for_root(root, args.trading_context),
                cutoff_at=args.cutoff_at,
            )
        except BusError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        args.as_of_trading_date = premarket_context["as_of_trading_date"]
        args.overnight_window_start = premarket_context["overnight_window_start"]
        args.overnight_window_end = premarket_context["overnight_window_end"]
        write_json(folder / "premarket_context.json", premarket_context)
    else:
        args.as_of_trading_date = date
        args.overnight_window_start = ""
        args.overnight_window_end = ""
    status_path = folder / "daily_pipeline_status.json"
    source = Path(args.handoff).resolve() if args.handoff else None
    if source and not source.is_file():
        raise FileNotFoundError(f"handoff not found: {source}")
    bus_pull_result: dict[str, Any] | None = None
    if args.bus_pull_decision:
        from daily_research_bus import BusError, pull_decision

        try:
            bus_pull_result = pull_decision(root, date, args.bus_branch)
        except BusError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
    existing_status = read_json(status_path, {})
    isolated_public_run = args.data_only or args.public_only
    existing_handoff = {} if isolated_public_run else read_json(folder / "chatgpt_codex_handoff.json", {})
    source_payload = {} if isolated_public_run else (read_json(source, {}) if source else {})
    run_id = (
        run_id_for_date(existing_status, date)
        or run_id_for_date(existing_handoff, date)
        or run_id_for_date(source_payload, date)
        or f"dpv1-{compact}-{uuid.uuid4().hex[:10]}"
    )
    holding_input = None if isolated_public_run else find_holding_input(folder, compact, Path(args.holdings).resolve() if args.holdings else None)
    holding_input = None if isolated_public_run else stage_explicit_holdings(folder, compact, holding_input, args.holdings)
    if isolated_public_run:
        handoff_path = folder / "chatgpt_codex_handoff.json"
        handoff: dict[str, Any] = {}
        handoff_errors_list: list[str] = []
    else:
        handoff_path, handoff, handoff_errors_list = update_handoff(folder, date, run_id, source, [])
    status = load_or_create_status(status_path, root, folder, date, run_id, handoff_path)
    if args.fresh_attempt:
        status["attempt_id"] = args.attempt_id or f"attempt-{compact}-{uuid.uuid4().hex[:10]}"
        status["attempt_started_at"] = args.attempt_started_at or now()
        status["attempt_finished_at"] = ""
        status["attempt_status"] = "running"
        status["steps"] = []
        status["warnings"] = []
        status["blockers"] = []
    else:
        status["attempt_id"] = ""
        status["attempt_started_at"] = ""
        status["attempt_finished_at"] = ""
        status["attempt_status"] = ""
    reset_current_blockers(status)
    status["handoff"] = {
        "path": "" if isolated_public_run else relative_to_root(handoff_path, root),
        "status": "not_applicable" if isolated_public_run else handoff.get("handoff_status"),
        "errors": handoff_errors_list,
    }
    status["session"] = args.session
    status["public_only"] = isolated_public_run
    if handoff_errors_list:
        status["blockers"].extend(handoff_errors_list)
    if bus_pull_result:
        add_step(
            status,
            "daily_bus_pull",
            "passed",
            "imported ChatGPT decision from daily-bus",
            branch=args.bus_branch,
            path=bus_pull_result["path"],
        )
    persist_status(status, status_path)

    # Capture the morning bundle before any close collector can overwrite it.
    # Hash comparison is the primary guard; artifact timestamps/date fields
    # provide a second check against copied or relabeled stale files.
    close_freshness_baseline = _artifact_snapshot(folder)
    close_freshness_ok = True

    if args.stage in {"collect", "prepare", "full"}:
        collect_stage(status, status_path, root, date, compact, holding_input, args)
    if args.stage in {"prepare", "full"}:
        prepare_stage(status, status_path, root, date, compact, folder, holding_input, args)

    if args.session == "close" and isolated_public_run and args.fresh_attempt:
        close_freshness_ok, freshness_states, freshness_errors = validate_close_freshness(
            folder,
            date,
            status.get("attempt_started_at", ""),
            close_freshness_baseline,
            allow_reuse=args.from_existing,
        )
        add_step(
            status,
            "close_freshness_gate",
            "passed" if close_freshness_ok else "failed",
            "close public artifacts regenerated for this attempt" if close_freshness_ok else "; ".join(freshness_errors),
            artifacts=freshness_states,
        )
        if not close_freshness_ok:
            status["blockers"].append("close freshness gate failed")
        persist_status(status, status_path)
    if args.bus_publish_research:
        from daily_research_bus import BusError, publish_research

        # Publish a truthful collection state.  The final coordinator status is
        # finalized after Bus transport, but research-input must not expose the
        # transient ``running`` state from before collect/prepare completed.
        if isolated_public_run:
            status["status"] = "blocked" if status["blockers"] else ("partial" if status["warnings"] else "completed")
            if status.get("attempt_id"):
                status["attempt_status"] = status["status"]
            persist_status(status, status_path)
        if not close_freshness_ok:
            add_step(status, "daily_bus_publish", "skipped", "close freshness gate failed; research input not published")
            persist_status(status, status_path)
        else:
            try:
                bus_publish_result = publish_research(
                    root,
                    date,
                    args.bus_branch,
                    session=args.session,
                    attempt_id=status.get("attempt_id", ""),
                    attempt_started_at=status.get("attempt_started_at", ""),
                    trading_context_path=context_path_for_root(root, args.trading_context),
                    cutoff_at=args.overnight_window_end,
                )
            except BusError as exc:
                publish_details = getattr(exc, "details", {})
                if not isinstance(publish_details, dict):
                    publish_details = {}
                observability = {
                    key: publish_details[key]
                    for key in (
                        "prepared_status",
                        "prepared_path",
                        "prepared_sha256",
                        "prepared_size",
                        "prepared_session",
                        "prepared_attempt_id",
                        "publish_status",
                        "publish_attempt_count",
                        "publish_error",
                    )
                    if key in publish_details
                }
                add_step(status, "daily_bus_publish", "failed", str(exc), branch=args.bus_branch, **observability)
                status["blockers"].append(f"daily bus publish failed: {exc}")
                persist_status(status, status_path)
            else:
                add_step(
                    status,
                    "daily_bus_publish",
                    "passed",
                    "published compressed research input to daily-bus",
                    branch=args.bus_branch,
                    path=bus_publish_result["path"],
                    prepared_status=bus_publish_result.get("prepared_status", ""),
                    prepared_path=bus_publish_result.get("prepared_path", ""),
                    prepared_sha256=bus_publish_result.get("prepared_sha256", ""),
                    prepared_size=bus_publish_result.get("prepared_size", 0),
                    prepared_session=bus_publish_result.get("prepared_session", args.session),
                    prepared_attempt_id=bus_publish_result.get("prepared_attempt_id", status.get("attempt_id", "")),
                    changed=bus_publish_result["changed"],
                    publish_status=bus_publish_result.get("publish_status", "not_attempted"),
                    publish_attempt_count=bus_publish_result.get("publish_attempt_count", 0),
                    publish_attempts=bus_publish_result.get("publish_attempts", []),
                    publish_error=bus_publish_result.get("publish_error", ""),
                )
                persist_status(status, status_path)
    if args.stage in {"delivery", "full"}:
        # Reload in case ChatGPT/Codex edited the canonical package between stages.
        handoff = read_json(handoff_path, handoff)
        delivery_stage(status, status_path, root, date, compact, folder, handoff, args)

    if isolated_public_run:
        finalize_status(status, status_path, folder, compact, root, public_only=True)
    else:
        collected = [value for key, value in artifact_map(folder, compact, root).items() if value and key not in {"pipeline_status", "handoff"}]
        update_handoff(folder, date, run_id, None, collected)
        finalize_status(status, status_path, folder, compact, root)
    incomplete = status["status"] in {"blocked", "awaiting_handoff", "ready_to_publish"}
    print(json.dumps({
        "ok": not status["blockers"] and not incomplete,
        "status": status["status"],
        "date": date,
        "run_id": run_id,
        "handoff": "" if isolated_public_run else str(handoff_path),
        "pipeline_status": str(status_path),
        "publication": status["publication"],
        "blockers": status["blockers"],
        "warnings": status["warnings"],
    }, ensure_ascii=False))
    return 2 if status["blockers"] or incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
