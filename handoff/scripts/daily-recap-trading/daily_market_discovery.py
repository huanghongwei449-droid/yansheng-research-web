#!/usr/bin/env python3
"""Build the daily cross-sectional market discovery artifact.

This is intentionally a light-weight observation layer.  It does not replace
the durable topic state or the six-part research score.  Its job is to record
what the tape selected today before yesterday's topics are reviewed.  Broad
industry/concept observations are kept separately from durable themes so a new
market direction can be discovered before it has a topic_key.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any


VERSION = "v4.9.2"


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_date(value: str) -> tuple[str, str]:
    compact = value.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError("date must be YYYYMMDD or YYYY-MM-DD")
    return compact, f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def rows(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        value = payload.get(key, [])
        return value if isinstance(value, list) else []
    return []


def as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def market_snapshot(folder: Path) -> dict[str, Any]:
    compact = folder.name.removeprefix("tmp_recap_")
    snapshot = read_json(folder / "market_decision_snapshot.json", {}) or {}
    state = read_json(folder / "daily_mainline_decision_state.json", {}) or {}
    close_data = read_json(folder / f"market_data_{compact}.json", {}) or {}
    market = snapshot.get("market") or state.get("market") or {}
    sentiment = market.get("sentiment") or {}
    return {
        "score": market.get("score"),
        "yesterday_score": market.get("yesterday_score"),
        "delta_1d": market.get("delta_1d"),
        "state": market.get("state"),
        "turnover_trillion": market.get("turnover_trillion"),
        "main_net_billion": market.get("main_net_billion"),
        "index_summary": market.get("index_summary"),
        "breadth": market.get("breadth") or close_data.get("breadth"),
        "turnover": market.get("turnover") or close_data.get("turnover"),
        "sector_funds": market.get("sector_funds") or close_data.get("sector_funds") or [],
        "sentiment": {
            key: sentiment.get(key)
            for key in (
                "limit_up", "limit_down", "broken_board", "highest_board",
                "prior_limit_up_relimit", "prior_limit_up_total",
            )
            if key in sentiment
        },
    }


def load_theme_rows(folder: Path) -> list[dict[str, Any]]:
    snapshot = read_json(folder / "market_decision_snapshot.json", {}) or {}
    state = read_json(folder / "daily_mainline_decision_state.json", {}) or {}
    source = rows(snapshot, "themes") or rows(state, "themes")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in source:
        name = str(item.get("topic_name") or item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(item)
    return result


def load_candidate_context(folder: Path, compact: str) -> dict[str, dict[str, Any]]:
    """Load current-day candidates without treating them as durable themes."""
    payloads = [
        read_json(folder / f"weekend_research_candidates_{compact}.json", {}),
        read_json(folder / "research_candidate_manifest.json", {}),
    ]
    result: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        for item in rows(payload, "candidates"):
            name = str(item.get("topic_name") or "").strip()
            if name:
                result[name] = item
    return result


def candidate_as_theme(candidate: dict[str, Any]) -> dict[str, Any]:
    """Adapt a same-day research candidate to the discovery input shape."""
    return {
        "topic_key": candidate.get("topic_key") or candidate.get("suggested_topic_key"),
        "topic_name": candidate.get("topic_name"),
        "source_state": candidate.get("source_state"),
        "industry_layer": candidate.get("industry_layer"),
        "market_pricing_layer": candidate.get("market_pricing_layer"),
        "short_term_layer": candidate.get("short_term_layer"),
        "research_question": candidate.get("research_question"),
        "core_logic": candidate.get("core_logic"),
        "main_conflict": candidate.get("main_conflict"),
        "representative_companies": candidate.get("representative_companies") or [],
        "evidence_ids": candidate.get("evidence_ids") or candidate.get("supporting_evidence_ids") or [],
        "candidate_only": True,
        "candidate_priority": candidate.get("candidate_priority") or candidate.get("monday_priority"),
    }


def load_same_day_claims(folder: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(folder / "same_day_market_validation.json", {}) or {}
    result: dict[str, dict[str, Any]] = {}
    for item in rows(payload, "validated_stock_claims"):
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        if code:
            result[code] = item
        if name:
            result[name] = item
    return result


def load_evidence_ids(folder: Path) -> set[str]:
    payload = read_json(folder / "evidence_index.json", {}) or {}
    result: set[str] = set()
    for item in rows(payload, "evidence"):
        evidence_id = item.get("evidence_id") or item.get("id")
        if evidence_id:
            result.add(str(evidence_id))
    return result


def observation_from_row(row: dict[str, Any], board_type: str, source: str) -> dict[str, Any] | None:
    name = str(row.get("board_name") or row.get("name") or row.get("f14") or "").strip()
    if not name:
        return None
    declared = row.get("topic_keys") or []
    if isinstance(declared, str):
        declared = [declared]
    return {
        "board_code": str(row.get("board_code") or row.get("code") or row.get("f12") or ""),
        "board_name": name,
        "board_type": board_type,
        "pct_change": as_number(row.get("pct_change") if "pct_change" in row else row.get("f3")),
        "turnover_rate": as_number(row.get("turnover_rate") if "turnover_rate" in row else row.get("f8")),
        "turnover_yuan": as_number(row.get("turnover_yuan")),
        "company_count": as_number(row.get("company_count")),
        "main_net_yuan": as_number(
            row.get("main_net_yuan")
            if "main_net_yuan" in row
            else row.get("main_net") if "main_net" in row else row.get("f62")
        ),
        "advance_count": as_number(row.get("advance_count") if "advance_count" in row else row.get("f104")),
        "decline_count": as_number(row.get("decline_count") if "decline_count" in row else row.get("f105")),
        "leader": row.get("leader") or row.get("f128"),
        "leader_code": str(row.get("leader_code") or row.get("f140") or ""),
        "fund_flow_available": bool(row.get("fund_flow_available", "main_net_yuan" in row or "f62" in row)),
        "breadth_available": bool(row.get("breadth_available", "advance_count" in row or "f104" in row)),
        "declared_topic_keys": [str(key) for key in declared if key],
        "source_artifacts": [source],
    }


def load_broad_market_observations(
    folder: Path,
    compact: str,
    market: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Load blind cross-sectional observations before durable-theme matching.

    The preferred input is the industry/concept rank captured by
    data_source_health.json.  A same-day market_data file or a manually
    validated market_cross_section_input.json can provide the same contract.
    """
    observations: list[dict[str, Any]] = []
    # Only sources that cover an actual industry/concept cross-section qualify
    # the run as a blind full-market scan. A hand-built list of familiar theme
    # baskets is useful context, but it cannot prove that new directions were
    # searched for.
    full_market_sources: list[str] = []

    health_path = folder / "data_source_health.json"
    health = read_json(health_path, {}) or {}
    for endpoint in rows(health, "endpoints"):
        endpoint_name = str(endpoint.get("endpoint") or "")
        if endpoint_name not in {
            "eastmoney_industry_rank_with_breadth",
            "eastmoney_concept_rank_with_breadth",
            "ths_industry_concept_cross_section",
            "sina_industry_concept_cross_section",
        }:
            continue
        for row in endpoint.get("discovery_rows") or endpoint.get("sample") or []:
            if isinstance(row, dict):
                board_type = str(row.get("board_type") or ("industry" if "industry" in endpoint_name else "concept"))
                item = observation_from_row(row, board_type, "data_source_health.json")
                if item:
                    observations.append(item)
        if endpoint.get("usable_for_evidence"):
            full_market_sources.append("data_source_health.json")

    market_data_path = folder / f"market_data_{compact}.json"
    market_data = read_json(market_data_path, {}) or {}
    sector_rows = market_data.get("sector_funds") or market.get("sector_funds") or []
    for row in sector_rows:
        if isinstance(row, dict):
            item = observation_from_row(row, "sector_fund", market_data_path.name)
            if item:
                observations.append(item)
    if sector_rows:
        full_market_sources.append(
            market_data_path.name if market_data_path.exists() else "market_snapshot.sector_funds"
        )

    # THS is an independent public cross-section fallback.  Its adapter
    # records industry and concept detail pages separately from Eastmoney;
    # only a complete adapter snapshot can promote the run to full_market.
    dashed_date = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    ths_path = folder / "ths_cross_section_snapshot.json"
    ths = read_json(ths_path, {}) or {}
    ths_is_same_day = ths.get("date") == dashed_date
    if ths_is_same_day:
        for row in ths.get("observations") or []:
            if isinstance(row, dict):
                item = observation_from_row(row, str(row.get("board_type") or "ths"), ths_path.name)
                if item:
                    observations.append(item)
    if ths_is_same_day and ths.get("scan_complete") is True and ths.get("blind_scan_complete") is True:
        full_market_sources.append(ths_path.name)

    sina_path = folder / "sina_sector_snapshot.json"
    sina = read_json(sina_path, {}) or {}
    sina_is_same_day = sina.get("date") == dashed_date and sina.get("date_match") is True
    if sina_is_same_day:
        for row in sina.get("observations") or []:
            if isinstance(row, dict):
                item = observation_from_row(row, str(row.get("board_type") or "sina"), sina_path.name)
                if item:
                    observations.append(item)
    if sina_is_same_day and sina.get("scan_complete") is True and sina.get("blind_scan_complete") is True:
        full_market_sources.append(sina_path.name)

    manual_path = folder / "market_cross_section_input.json"
    manual = read_json(manual_path, {}) or {}
    for row in manual.get("observations") or []:
        if isinstance(row, dict):
            item = observation_from_row(row, str(row.get("board_type") or "verified_manual"), manual_path.name)
            if item:
                observations.append(item)
    manual_scope = str(manual.get("scan_scope") or "").strip().lower()
    manual_types = {
        str(row.get("board_type") or "").strip().lower()
        for row in manual.get("observations") or []
        if isinstance(row, dict)
    }
    manual_is_full_market = (
        manual.get("scan_complete") is True
        and (
            manual_scope in {"full_market", "industry_and_concept", "all_market"}
            or bool(manual_types & {"industry", "concept", "full_market_sector"})
        )
    )
    if manual_is_full_market:
        full_market_sources.append(manual_path.name)

    merged: dict[str, dict[str, Any]] = {}
    for item in observations:
        key = item["board_name"]
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        existing["source_artifacts"] = sorted(set(existing["source_artifacts"] + item["source_artifacts"]))
        existing["declared_topic_keys"] = list(dict.fromkeys(
            (existing.get("declared_topic_keys") or []) + (item.get("declared_topic_keys") or [])
        ))
        for field in (
            "board_code", "pct_change", "turnover_rate", "main_net_yuan",
            "turnover_yuan", "company_count", "advance_count", "decline_count",
            "leader", "leader_code", "fund_flow_available", "breadth_available",
        ):
            if existing.get(field) in (None, "") and item.get(field) not in (None, ""):
                existing[field] = item[field]

    result = list(merged.values())
    for item in result:
        advance = item.get("advance_count")
        decline = item.get("decline_count")
        total = (advance or 0) + (decline or 0)
        item["advance_share"] = round((advance or 0) / total, 4) if total else None
    result.sort(
        key=lambda item: (
            item.get("pct_change") is not None,
            item.get("pct_change") if item.get("pct_change") is not None else -999,
            item.get("main_net_yuan") if item.get("main_net_yuan") is not None else -999,
        ),
        reverse=True,
    )
    if full_market_sources:
        scan_scope = "full_market"
    elif result:
        scan_scope = "representative_baskets_only"
    else:
        scan_scope = "missing"
    return result[:60], sorted(set(full_market_sources)), scan_scope


def observation_matches_theme(observation: dict[str, Any], theme: dict[str, Any]) -> bool:
    name = str(observation.get("board_name") or "").strip()
    if not name:
        return False
    haystack = json.dumps(theme, ensure_ascii=False)
    if name in haystack:
        return True
    pieces = [part for part in re.split(r"[与、/及+（）()\s]+", name) if len(part) >= 2]
    return any(part in haystack for part in pieces)


def theme_members(theme: dict[str, Any], claims: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = theme.get("metrics") or {}
    members = metrics.get("members") or theme.get("members") or []
    if not members:
        references = (
            theme.get("reference_members")
            or theme.get("fixed_reference_basket")
            or theme.get("representative_companies")
            or []
        )
        members = [{"name": item} for item in references]
    result: list[dict[str, Any]] = []
    for item in members:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        name = str(item.get("name") or item.get("stock_name") or code)
        claim = claims.get(code) or claims.get(name) or {}
        change = item.get("return_1d_pct")
        if change is None:
            change = item.get("pct_change")
        if change is None:
            change = claim.get("change_pct") or claim.get("pct_change")
        result.append({
            "code": code,
            "name": name,
            "return_1d_pct": as_number(change),
            "return_5d_pct": as_number(item.get("return_5d_pct")),
            "volume_ratio_20d": as_number(item.get("volume_ratio_20d")),
            "above_ma20": item.get("above_ma20"),
            "source": "theme_metrics_or_same_day_market_validation",
        })
    return result


def stable_roles(theme: dict[str, Any]) -> dict[str, Any]:
    roles = theme.get("roles") or theme.get("representative_roles") or {}
    if not roles:
        companies = theme.get("representative_companies") or []
        for company in companies:
            if not isinstance(company, dict):
                continue
            role = str(company.get("role") or company.get("industry_role") or "")
            name = company.get("stock_name") or company.get("name")
            if not name:
                continue
            if "产业" in role or "基本面" in role:
                roles.setdefault("fundamental_core", name)
            elif "容量" in role or "中军" in role:
                roles.setdefault("capacity_mid", name)
            elif "弹性" in role or "情绪" in role:
                roles.setdefault("emotion_leader", name)
    return {
        "industry_core": roles.get("fundamental_core") or roles.get("industry_core"),
        "capacity_core": roles.get("capacity_mid") or roles.get("capacity_core"),
        "sentiment_leader": roles.get("emotion_leader") or roles.get("sentiment_leader"),
    }


def role_snapshot(theme: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    roles = stable_roles(theme)
    priced = [item for item in members if item.get("return_1d_pct") is not None]
    leader = max(priced, key=lambda item: item["return_1d_pct"]) if priced else None
    laggard = min(priced, key=lambda item: item["return_1d_pct"]) if priced else None
    return {
        **roles,
        "price_leader_today": leader.get("name") if leader else None,
        "price_leader_today_code": leader.get("code") if leader else None,
        "risk_sample_today": laggard.get("name") if laggard else None,
        "risk_sample_today_code": laggard.get("code") if laggard else None,
        "role_basis": {
            "industry_core": "stable role from company/industry evidence; not daily return",
            "capacity_core": "stable role from company/industry evidence; not daily return",
            "price_leader_today": "highest verified same-day return among available members",
            "risk_sample_today": "lowest verified same-day return among available members",
        },
    }


def market_structure(theme: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = theme.get("metrics") or {}
    priced = [item for item in members if item.get("return_1d_pct") is not None]
    positive_share = (
        sum(item["return_1d_pct"] > 0 for item in priced) / len(priced)
        if priced else None
    )
    average_return = mean(item["return_1d_pct"] for item in priced) if priced else None
    volume_values = [item["volume_ratio_20d"] for item in members if item.get("volume_ratio_20d") is not None]
    volume_ratio = mean(volume_values) if volume_values else as_number(metrics.get("average_volume_ratio_20d"))
    above_ma20 = [item["above_ma20"] for item in members if isinstance(item.get("above_ma20"), bool)]
    above_share = sum(above_ma20) / len(above_ma20) if above_ma20 else as_number(metrics.get("above_ma20_share"))
    limit_up = theme.get("limit_up_count")
    if limit_up is None:
        limit_up = (theme.get("market_structure") or {}).get("limit_up_count")
    effect = "数据不足"
    if as_number(limit_up) is not None and as_number(limit_up) >= 3:
        effect = "情绪板块效应"
    elif positive_share is not None and positive_share >= 0.6 and (volume_ratio or 0) >= 1.05:
        effect = "趋势板块效应"
    elif positive_share is not None and positive_share >= 0.6:
        effect = "局部价格共振"
    elif priced:
        effect = "单股行情或弱共振"

    pricing_text = " ".join(
        str(theme.get(key) or "")
        for key in ("market_pricing_layer", "market_fact", "fund_validation", "change_log")
    )
    if "净流入" in pricing_text or "流入" in pricing_text:
        money = "当日流入线索"
    elif "净流出" in pricing_text or "流出" in pricing_text:
        money = "当日流出线索"
    else:
        money = "资金连续性待确认"

    if money == "当日流入线索" and effect in {"情绪板块效应", "趋势板块效应", "局部价格共振"}:
        relation = "资金与价格共振"
    elif money == "当日流入线索":
        relation = "有资金、情绪不足"
    elif money == "当日流出线索" and effect in {"情绪板块效应", "局部价格共振"}:
        relation = "有情绪、资金背离"
    else:
        relation = "连续性待确认"

    return {
        "member_count": len(members),
        "priced_member_count": len(priced),
        "positive_member_share": round(positive_share, 4) if positive_share is not None else None,
        "average_return_1d_pct": round(average_return, 3) if average_return is not None else None,
        "above_ma20_share": round(above_share, 4) if above_share is not None else None,
        "average_volume_ratio_20d": round(volume_ratio, 3) if volume_ratio is not None else None,
        "limit_up_count": int(limit_up) if as_number(limit_up) is not None else None,
        "effect_type": effect,
        "money_signal": money,
        "money_emotion_relation": relation,
        "data_complete": len(priced) >= 3,
    }


def source_state(theme: dict[str, Any]) -> str:
    value = str(theme.get("source_state") or theme.get("status") or "").strip()
    aliases = {
        "新增": "新增",
        "延续": "延续",
        "重新升温": "重新升温",
        "风险线索": "风险线索",
        "strengthened": "重新升温",
        "continued": "延续",
        "新增候选": "新增",
        "新增侧线": "新增",
        "旧链条深化": "重新升温",
        "旧链条改变": "重新升温",
        "延续并重新升温": "重新升温",
    }
    return aliases.get(value, value or "未分类")


def discovery_score(structure: dict[str, Any], theme: dict[str, Any]) -> float | None:
    """A ranking aid, not the durable six-factor research score."""
    if not structure.get("data_complete"):
        return None
    score = 0.0
    positive = structure.get("positive_member_share") or 0
    average = structure.get("average_return_1d_pct") or 0
    volume = structure.get("average_volume_ratio_20d") or 0
    score += min(max(positive, 0), 1) * 35
    score += min(max(average + 5, 0), 10) / 10 * 30
    score += min(max(volume, 0), 2) / 2 * 20
    if structure.get("effect_type") in {"情绪板块效应", "趋势板块效应"}:
        score += 15
    return round(min(score, 100), 2)


def build(args: argparse.Namespace) -> dict[str, Any]:
    compact, dashed = normalize_date(args.date)
    root = Path(args.root).resolve()
    folder = root / f"tmp_recap_{compact}"
    snapshot_path = folder / "market_decision_snapshot.json"
    state_path = folder / "daily_mainline_decision_state.json"
    claims = load_same_day_claims(folder)
    evidence_ids_available = load_evidence_ids(folder)
    theme_rows = load_theme_rows(folder)
    candidate_context = load_candidate_context(folder, compact)
    market = market_snapshot(folder)
    broad_observations, blind_scan_sources, blind_scan_scope = load_broad_market_observations(
        folder, compact, market
    )
    existing_names = {
        str(item.get("topic_name") or "").strip()
        for item in theme_rows
        if item.get("topic_name")
    }
    for candidate in candidate_context.values():
        name = str(candidate.get("topic_name") or "").strip()
        if name and name not in existing_names:
            theme_rows.append(candidate_as_theme(candidate))
            existing_names.add(name)

    themes: list[dict[str, Any]] = []
    for theme in theme_rows:
        context = candidate_context.get(str(theme.get("topic_name")), {})
        combined = dict(theme)
        for key in ("source_state", "roles", "representative_companies", "evidence_ids", "next_day_triggers"):
            if not combined.get(key) and context.get(key):
                combined[key] = context[key]
        members = theme_members(combined, claims)
        structure = market_structure(combined, members)
        roles = role_snapshot(combined, members)
        score = discovery_score(structure, theme)
        raw_evidence_ids = [str(item) for item in (combined.get("evidence_ids") or [])]
        valid_evidence_ids = [item for item in raw_evidence_ids if item in evidence_ids_available]
        themes.append({
            "topic_key": theme.get("topic_key") or f"theme_{theme.get('topic_name')}",
            "topic_name": theme.get("topic_name"),
            "source_state": source_state(combined),
            "candidate_only": bool(combined.get("candidate_only")),
            "candidate_priority": combined.get("candidate_priority"),
            "market_discovery_score": score,
            "market_structure": structure,
            "roles": roles,
            "members": members,
            "evidence_ids": valid_evidence_ids[:12],
            "unresolved_evidence_ids": [
                item for item in raw_evidence_ids if item not in evidence_ids_available
            ][:12],
            "reason_today": combined.get("market_pricing_layer") or combined.get("market_fact") or "当天盘面解释待补",
            "next_verification": combined.get("next_day_triggers") or combined.get("short_term_layer") or "等待下一交易日连续性验证",
            "public_priority": False,
        })

    # A theme with verified cross-sectional data outranks a theme that only has
    # a narrative.  The durable research score remains a separate field.
    themes.sort(
        key=lambda item: (
            item.get("market_discovery_score") is not None,
            item.get("market_discovery_score") if item.get("market_discovery_score") is not None else -1,
            item.get("candidate_only") is False,
        ),
        reverse=True,
    )
    for index, item in enumerate(themes, start=1):
        item["market_discovery_rank"] = index
        item["public_priority"] = index <= 6 and (
            item["market_structure"].get("effect_type") not in {"数据不足", "单股行情或弱共振"}
            or item.get("source_state") in {"新增", "重新升温", "风险线索"}
        )

    for observation in broad_observations:
        inferred = [
            str(theme.get("topic_key") or f"theme_{theme.get('topic_name')}")
            for theme in themes
            if observation_matches_theme(observation, theme)
        ]
        observation["matched_topic_keys"] = list(dict.fromkeys(
            (observation.get("declared_topic_keys") or []) + inferred
        ))

    unmapped_market_leaders = [
        observation
        for observation in broad_observations
        if (observation.get("pct_change") or 0) > 0
        and not observation.get("matched_topic_keys")
    ][:12]

    has_scan_inputs = bool(broad_observations) or any(
        (folder / name).exists()
        for name in (
            "market_decision_snapshot.json",
            "daily_mainline_decision_state.json",
            "data_source_health.json",
            "market_cross_section_input.json",
            "ths_cross_section_snapshot.json",
            "sina_sector_snapshot.json",
        )
    )
    conclusion = "no_material_increment" if not themes else "market_discovery_complete"
    payload = {
        "schema_version": "daily-market-discovery-v1",
        "version": VERSION,
        "date": dashed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scan_complete": bool(has_scan_inputs),
        "conclusion": conclusion,
        "scan_order": [
            "market_cross_section",
            "money_and_sentiment",
            "role_split",
            "new_and_reheated_themes",
            "prior_day_validation",
        ],
        "market": market,
        "blind_scan_complete": blind_scan_scope == "full_market",
        "blind_scan_scope": blind_scan_scope,
        "blind_scan_sources": blind_scan_sources,
        "blind_market_observations": broad_observations,
        "unmapped_market_leaders": unmapped_market_leaders,
        "discovery_order": [item["topic_key"] for item in themes],
        "public_priority_order": [item["topic_key"] for item in themes if item["public_priority"]],
        "research_candidate_order": [
            item["topic_key"] for item in themes
            if item.get("candidate_only")
            and item.get("source_state") in {"新增", "重新升温", "风险线索"}
        ],
        "themes": themes,
        "data_gaps": [
            "未提供逐板块连续资金序列时，不把当日价格表现写成连续资金流入。",
            "缺少完整成员日K时，不输出精确市场发现分数。",
            "短线梯队与趋势板块分别判断，不用涨停数量替代趋势强度。",
            *(
                []
                if blind_scan_sources
                else [
                    "行业/概念横截面未完成；代表性旧主题篮子只能用于跟踪，不能宣称已经发现当天全部新方向。"
                ]
            ),
        ],
        "source_artifacts": [
            name for name in (
                "market_decision_snapshot.json",
                "daily_mainline_decision_state.json",
                "same_day_market_validation.json",
                "evidence_index.json",
                f"market_data_{compact}.json",
                f"weekend_research_candidates_{compact}.json",
                "research_candidate_manifest.json",
                "data_source_health.json",
                "market_cross_section_input.json",
                "ths_cross_section_snapshot.json",
                "sina_sector_snapshot.json",
            ) if (folder / name).exists()
        ],
        "public_safe": True,
        "private_related": False,
    }
    write_json(folder / "daily_market_discovery.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("date")
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
