"""Production-side trading_context_v1 contract owner.

The module reads one explicit, validated local versioned SSE/SZSE calendar
artifact.  It never reads the historical closure-set configuration and never
uses input, Brain, GitHub, canonical, or Feed state as calendar truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "trading-context-v1"
CALENDAR_SOURCE_SCHEMA = "a-share-trading-calendar-source-v1"
CALENDAR_PUBLISHED_SCHEMA = "a-share-trading-calendar-published-v1"
MARKET = "CN-A-share"
TIMEZONE = "Asia/Shanghai"
SHANGHAI = timezone(timedelta(hours=8), name=TIMEZONE)
MISMATCH = "TRADING_CALENDAR_MISMATCH"
DEFAULT_CONTEXT_RELATIVE = Path("calendar") / "published" / "current.json"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class TradingContextError(ValueError):
    """Calendar or context data cannot safely be consumed."""


def context_path_for_root(root: Path, requested: Path | str | None = None) -> Path:
    """Resolve an explicit context path relative to the owning repository root."""

    root = Path(root).resolve()
    if requested in (None, ""):
        return root / DEFAULT_CONTEXT_RELATIVE
    path = Path(requested)
    return path if path.is_absolute() else root / path


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise TradingContextError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise TradingContextError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise TradingContextError(f"{field} must be YYYY-MM-DD")
    return parsed


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TradingContextError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TradingContextError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise TradingContextError(f"{field} must include a timezone")
    return parsed.astimezone(SHANGHAI).isoformat(timespec="seconds")


def _date_list(value: Any, field: str, start: date, end: date) -> frozenset[date]:
    if not isinstance(value, list):
        raise TradingContextError(f"{field} must be an array")
    parsed = [_parse_date(item, field) for item in value]
    if len(parsed) != len(set(parsed)):
        raise TradingContextError(f"{field} contains duplicate dates")
    if any(item < start or item > end for item in parsed):
        raise TradingContextError(f"{field} contains date outside coverage")
    return frozenset(parsed)


def _load_json(source: Mapping[str, Any] | Path | str) -> dict[str, Any]:
    if isinstance(source, Mapping):
        payload = dict(source)
    else:
        try:
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TradingContextError("versioned calendar artifact unavailable or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise TradingContextError("versioned calendar artifact root must be an object")
    return payload


def _normalized_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") == CALENDAR_PUBLISHED_SCHEMA:
        if payload.get("market") != MARKET or payload.get("reconciliation_status") != "reconciled":
            raise TradingContextError("published calendar is not validated")
        if not isinstance(payload.get("reconciliation_hash"), str) or len(payload["reconciliation_hash"]) != 64:
            raise TradingContextError("published calendar reconciliation lineage is missing")
        normalized = payload.get("normalized_calendar")
        if not isinstance(normalized, dict):
            raise TradingContextError("published calendar normalized source is missing")
        if normalized.get("calendar_version") != payload.get("calendar_version"):
            raise TradingContextError("published calendar version lineage mismatch")
        return normalized
    if payload.get("schema_version") == CALENDAR_SOURCE_SCHEMA:
        return payload
    raise TradingContextError("unsupported calendar artifact schema")


@dataclass(frozen=True)
class NormalizedCalendar:
    calendar_version: str
    coverage_start: date
    coverage_end: date
    source_hashes: Mapping[str, str]
    non_trading_dates: frozenset[date]
    special_trading_dates: frozenset[date]

    def is_trading_day(self, target: date) -> bool:
        if not self.coverage_start <= target <= self.coverage_end:
            raise TradingContextError(f"calendar cannot verify {target.isoformat()} within coverage")
        if target in self.special_trading_dates:
            return True
        if target in self.non_trading_dates:
            return False
        return target.weekday() < 5


def load_normalized_calendar(source: Mapping[str, Any] | Path | str) -> NormalizedCalendar:
    """Validate one published/source artifact and reconcile SSE with SZSE."""

    payload = _normalized_payload(_load_json(source))
    version = payload.get("calendar_version")
    if not isinstance(version, str) or not version.strip():
        raise TradingContextError("calendar_version is missing")
    if payload.get("market") != MARKET:
        raise TradingContextError("calendar market is invalid")
    start = _parse_date(payload.get("effective_coverage_start"), "effective_coverage_start")
    end = _parse_date(payload.get("effective_coverage_end"), "effective_coverage_end")
    if start > end:
        raise TradingContextError("calendar coverage is reversed")
    _timestamp(payload.get("generated_at"), "generated_at")
    _timestamp(payload.get("fetched_at"), "fetched_at")
    reconciliation = payload.get("reconciliation")
    if not isinstance(reconciliation, dict) or reconciliation.get("status") != "reconciled":
        raise TradingContextError("SSE/SZSE reconciliation is not validated")
    if set(reconciliation.get("required_exchanges") or []) != {"SSE", "SZSE"}:
        raise TradingContextError("SSE/SZSE reconciliation requires both exchanges")
    _timestamp(reconciliation.get("validated_at"), "reconciliation.validated_at")
    rows = payload.get("exchange_sources")
    if not isinstance(rows, list) or len(rows) != 2:
        raise TradingContextError("calendar must contain exactly two exchange sources")
    by_exchange: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("exchange") not in {"SSE", "SZSE"}:
            raise TradingContextError("calendar exchange source is invalid")
        exchange = str(row["exchange"])
        if exchange in by_exchange:
            raise TradingContextError("calendar exchange source is duplicated")
        for field in ("source_publication", "source_reference", "source_sha256"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise TradingContextError(f"{exchange} {field} is missing")
        unsigned = {key: value for key, value in row.items() if key != "source_sha256"}
        actual_hash = sha256_bytes(canonical_json_bytes(unsigned))
        if row["source_sha256"].lower() != actual_hash:
            raise TradingContextError(f"{exchange} source hash mismatch")
        closures = _date_list(row.get("non_trading_dates"), f"{exchange}.non_trading_dates", start, end)
        specials = _date_list(row.get("special_trading_dates"), f"{exchange}.special_trading_dates", start, end)
        if closures & specials:
            raise TradingContextError(f"{exchange} date is both closed and special trading")
        by_exchange[exchange] = {"closures": closures, "specials": specials, "hash": row["source_sha256"].lower()}
    if set(by_exchange) != {"SSE", "SZSE"}:
        raise TradingContextError("SSE/SZSE source is incomplete")
    universe = {start + timedelta(days=offset) for offset in range((end - start).days + 1)}
    universe.update(*(set(row["closures"]) | set(row["specials"]) for row in by_exchange.values()))
    for target in sorted(universe):
        decisions = set()
        for row in by_exchange.values():
            if target in row["specials"]:
                decisions.add(True)
            elif target in row["closures"]:
                decisions.add(False)
            else:
                decisions.add(target.weekday() < 5)
        if len(decisions) != 1:
            raise TradingContextError(f"SSE/SZSE reconciliation conflict at {target.isoformat()}")
    return NormalizedCalendar(
        calendar_version=version.strip(),
        coverage_start=start,
        coverage_end=end,
        source_hashes={key: row["hash"] for key, row in sorted(by_exchange.items())},
        non_trading_dates=frozenset().union(*(row["closures"] for row in by_exchange.values())),
        special_trading_dates=frozenset().union(*(row["specials"] for row in by_exchange.values())),
    )


def _parse_target(value: Any) -> date:
    return _parse_date(value, "target_date")


def _now_shanghai() -> str:
    return datetime.now(timezone.utc).astimezone(SHANGHAI).isoformat(timespec="seconds")


def _context_hash(base: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(dict(base)))


def _make_validated_context(calendar: NormalizedCalendar, target: date, validated_at: str) -> dict[str, Any]:
    previous = target - timedelta(days=1)
    while calendar.coverage_start <= previous <= calendar.coverage_end and not calendar.is_trading_day(previous):
        previous -= timedelta(days=1)
    following = target + timedelta(days=1)
    while calendar.coverage_start <= following <= calendar.coverage_end and not calendar.is_trading_day(following):
        following += timedelta(days=1)
    if not calendar.coverage_start <= previous <= calendar.coverage_end or not calendar.coverage_start <= following <= calendar.coverage_end:
        raise TradingContextError("calendar cannot verify previous/next trading day")
    base = {
        "schema_version": SCHEMA_VERSION,
        "target_date": target.isoformat(),
        "timezone": TIMEZONE,
        "weekday": WEEKDAYS[target.weekday()],
        "is_trading_day": calendar.is_trading_day(target),
        "previous_trading_day": previous.isoformat(),
        "next_trading_day": following.isoformat(),
        "calendar_source": "SSE+SZSE versioned reconciled source contract",
        "calendar_version": calendar.calendar_version,
        "validated_at": _timestamp(validated_at, "validated_at"),
        "validation_status": "validated",
        "calendar_source_hashes": dict(calendar.source_hashes),
    }
    return {**base, "context_sha256": _context_hash(base)}


def mismatch_context(target_date: Any, validated_at: Any, reason: str) -> dict[str, Any]:
    parsed = None
    try:
        parsed = _parse_target(target_date)
    except (TradingContextError, TypeError, ValueError):
        pass
    checked = validated_at if isinstance(validated_at, str) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "target_date": parsed.isoformat() if parsed else None,
        "timezone": TIMEZONE,
        "weekday": WEEKDAYS[parsed.weekday()] if parsed else None,
        "is_trading_day": None,
        "previous_trading_day": None,
        "next_trading_day": None,
        "calendar_source": None,
        "calendar_version": None,
        "validated_at": checked,
        "validation_status": "mismatch",
        "error_code": MISMATCH,
        "error": reason,
    }


def resolve_trading_context(
    root: Path,
    target_date: str,
    *,
    context_path: Path | None = None,
    context: Mapping[str, Any] | None = None,
    validated_at: str | None = None,
) -> dict[str, Any]:
    """Resolve an explicit context; no legacy or latest-artifact fallback."""

    checked_at = validated_at or _now_shanghai()
    try:
        if context is not None:
            value = dict(context)
            validate_context(value)
            if value.get("target_date") != target_date:
                raise TradingContextError("context target_date does not match requested date")
            return value
        source_path = (context_path or Path(root) / DEFAULT_CONTEXT_RELATIVE).resolve()
        calendar = load_normalized_calendar(source_path)
        return _make_validated_context(calendar, _parse_target(target_date), checked_at)
    except (TradingContextError, OSError, TypeError, ValueError) as exc:
        return mismatch_context(target_date, checked_at, str(exc))


def validate_context(value: Any) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise TradingContextError("trading context schema_version invalid")
    required = {"target_date", "timezone", "weekday", "is_trading_day", "previous_trading_day", "next_trading_day", "calendar_source", "calendar_version", "validated_at", "validation_status", "context_sha256"}
    if not required <= set(value):
        raise TradingContextError("trading context required field missing")
    if value["timezone"] != TIMEZONE or value["validation_status"] != "validated" or not isinstance(value["is_trading_day"], bool):
        raise TradingContextError("trading context is not validated")
    base = {key: item for key, item in value.items() if key != "context_sha256"}
    if value["context_sha256"] != _context_hash(base):
        raise TradingContextError("trading context hash mismatch")


def context_bytes(value: Mapping[str, Any]) -> bytes:
    validate_context(dict(value))
    return canonical_json_bytes(dict(value))


def context_sha256(value: Mapping[str, Any]) -> str:
    return sha256_bytes(context_bytes(value))
