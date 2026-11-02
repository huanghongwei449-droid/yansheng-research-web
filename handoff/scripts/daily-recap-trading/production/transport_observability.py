#!/usr/bin/env python3
"""Deterministic transport-stage tracing for the production Daily Bus path."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from production_task_logging import redact


TRANSPORT_TRACE_SCHEMA = "alphaengine-transport-trace-v1"

STAGE_LOCAL_ARTIFACT_GENERATED = "local_artifact_generated"
STAGE_LOCAL_ARTIFACT_VALIDATION = "local_artifact_validation"
STAGE_DAILY_BUS_FETCH = "daily_bus_fetch"
STAGE_WORKTREE_BRANCH_SYNC = "worktree_branch_sync"
STAGE_REMOTE_DIVERGENCE_CHECK = "remote_divergence_check"
STAGE_FILE_COPY_STAGE = "file_copy_stage"
STAGE_COMMIT = "commit"
STAGE_PUSH = "push"
STAGE_REMOTE_FETCH_READBACK = "remote_fetch_readback"
STAGE_REMOTE_SHA_CONTENT_VALIDATION = "remote_sha_content_validation"

TRANSPORT_STAGES = (
    STAGE_LOCAL_ARTIFACT_GENERATED,
    STAGE_LOCAL_ARTIFACT_VALIDATION,
    STAGE_DAILY_BUS_FETCH,
    STAGE_WORKTREE_BRANCH_SYNC,
    STAGE_REMOTE_DIVERGENCE_CHECK,
    STAGE_FILE_COPY_STAGE,
    STAGE_COMMIT,
    STAGE_PUSH,
    STAGE_REMOTE_FETCH_READBACK,
    STAGE_REMOTE_SHA_CONTENT_VALIDATION,
)

# The values are intentionally stable and distinct so Windows Task Scheduler
# can distinguish a transport failure without parsing human-readable output.
EXIT_SUCCESS = 0
EXIT_DOWNSTREAM_WAITING = 10
EXIT_COLLECT_FAILURE = 20
EXIT_LOCAL_VALIDATION_FAILURE = 21
EXIT_REMOTE_FETCH_FAILURE = 22
EXIT_BRANCH_WORKTREE_CONFLICT = 23
EXIT_REMOTE_LINEAGE_CONFLICT = 24
EXIT_COMMIT_FAILURE = 25
EXIT_PUSH_FAILURE = 26
EXIT_REMOTE_READBACK_FAILURE = 27
EXIT_REMOTE_CONTENT_VALIDATION_FAILURE = 28
EXIT_INTERNAL_FAILURE = 29

EXIT_CODE_SEMANTICS = {
    EXIT_SUCCESS: "success",
    EXIT_DOWNSTREAM_WAITING: "downstream_waiting_for_brain",
    EXIT_COLLECT_FAILURE: "collect_failure",
    EXIT_LOCAL_VALIDATION_FAILURE: "local_validation_failure",
    EXIT_REMOTE_FETCH_FAILURE: "remote_fetch_failure",
    EXIT_BRANCH_WORKTREE_CONFLICT: "branch_worktree_conflict",
    EXIT_REMOTE_LINEAGE_CONFLICT: "remote_lineage_conflict",
    EXIT_COMMIT_FAILURE: "commit_failure",
    EXIT_PUSH_FAILURE: "push_failure",
    EXIT_REMOTE_READBACK_FAILURE: "remote_readback_failure",
    EXIT_REMOTE_CONTENT_VALIDATION_FAILURE: "remote_content_validation_failure",
    EXIT_INTERNAL_FAILURE: "internal_failure",
}

T = TypeVar("T")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_error(value: Any, limit: int = 1200) -> str:
    text = str(redact(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class TransportTrace:
    """Collect one or more immutable, bounded records for each transport step."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    @property
    def records(self) -> list[dict[str, Any]]:
        return self._records

    def start(self, stage: str) -> int:
        if stage not in TRANSPORT_STAGES:
            raise ValueError(f"unsupported transport stage: {stage}")
        self._records.append(
            {
                "stage": stage,
                "started_at": iso_now(),
                "finished_at": None,
                "status": "RUNNING",
                "exit_code": None,
                "error_summary": "",
            }
        )
        return len(self._records) - 1

    def finish(
        self,
        index: int,
        *,
        status: str,
        exit_code: int,
        error_summary: str = "",
        details: Any = None,
    ) -> None:
        if status not in {"PASS", "FAIL", "SKIPPED"}:
            raise ValueError(f"unsupported transport status: {status}")
        record = self._records[index]
        record.update(
            {
                "finished_at": iso_now(),
                "status": status,
                "exit_code": int(exit_code),
                "error_summary": sanitize_error(error_summary),
            }
        )
        if details is not None:
            record["details"] = redact(details)

    def run(
        self,
        stage: str,
        operation: Callable[[], T],
        *,
        failure_exit_code: int,
        details: Any = None,
    ) -> T:
        index = self.start(stage)
        try:
            result = operation()
        except Exception as exc:
            exit_code = int(getattr(exc, "exit_code", failure_exit_code) or failure_exit_code)
            self.finish(index, status="FAIL", exit_code=exit_code, error_summary=exc, details=details)
            raise
        self.finish(index, status="PASS", exit_code=EXIT_SUCCESS, details=details)
        return result

    def skip(self, stage: str, *, reason: str, exit_code: int = EXIT_SUCCESS) -> None:
        index = self.start(stage)
        self.finish(index, status="SKIPPED", exit_code=exit_code, error_summary=reason)

    def skip_unvisited(self, *, reason: str) -> None:
        visited = {str(item.get("stage")) for item in self._records}
        for stage in TRANSPORT_STAGES:
            if stage not in visited:
                self.skip(stage, reason=reason)

    def failed_stage(self) -> str:
        for record in reversed(self._records):
            if record.get("status") == "FAIL":
                return str(record.get("stage") or "")
        return ""

    def to_dict(self) -> dict[str, Any]:
        failed = any(item.get("status") == "FAIL" for item in self._records)
        return {
            "schema_version": TRANSPORT_TRACE_SCHEMA,
            "status": "FAIL" if failed else "PASS",
            "failure_stage": self.failed_stage(),
            "stages": redact(self._records),
        }
