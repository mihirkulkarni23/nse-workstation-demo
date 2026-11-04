"""Standalone read-only importer for the six supplied application CSVs.

Reads the originals as bytes (never writes to them), keeps every source row with its
row number and raw strings, and builds a new immutable SQLite generation outside Git.
Unknown values stay NULL: no invented dates, prices, balances, scores or research.

    python -m producer.import_csv --source-dir <csv folder> --output-dir <private folder>
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import math
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path

SCHEMA_VERSION = 1
IMPORTER_VERSION = "1.0.0"
RECORD_ORIGIN = "application_recorded"
DATA_LABELS = ("supplied_csv", "synthetic_test")
CROSS_FILE_REASON = ("No upstream completion marker: the six files are checked one by one, but they may come "
                     "from different trading-app cycles.")
SNAPSHOT_NAME = "dashboard_snapshot.sqlite"
DIRECTIONS = {"LONG", "SHORT"}
MATCH_TOLERANCE = 0.011
MAX_RESEARCH_CHARS = 20_000
SAMPLE_ROWS = 20

SIGNAL_INDICATORS_30M = {
    "rsi": ["entry_rsi", "rsi"], "adx": ["entry_adx", "adx"], "atr": ["entry_atr", "atr"],
    "di_plus": ["di_plus_14"], "di_minus": ["di_minus_14"], "stoch_k": ["stoch_k"], "stoch_d": ["stoch_d"],
}
SIGNAL_INDICATORS_1H_1D = {
    "rsi": ["rsi"], "adx": ["adx"], "atr": ["atr"],
    "di_plus": ["di_plus"], "di_minus": ["di_minus"], "stoch_k": ["k"], "stoch_d": ["d"],
}


@dataclass(frozen=True)
class Dataset:
    name: str
    kind: str
    columns: tuple[str, ...]
    timeframe: str | None = None

    @property
    def file_name(self) -> str:
        return f"{self.name}.csv"


DATASETS = (
    Dataset("scanner_30m_signals", "signal", (
        "grade", "symbol", "stock_code", "direction", "signal_status", "signal_time", "entry_price", "atr",
        "stop_price", "qty", "risk_per_share", "risk_amount", "entry_rsi", "entry_adx", "entry_atr",
        "di_plus_14", "di_minus_14", "stoch_k", "stoch_d", "signal_score", "confidence_score",
        "setup_quality", "trade_readiness", "research_available", "research_context", "trend_rating",
        "momentum_rating", "entry_volume_ratio", "volume_qualified", "day_strength", "day_range_position",
        "day_return_pct", "historical_matches", "historical_winrate", "historical_avgr", "historical_mfe",
        "historical_mae", "historical_hold", "best_exit_model", "top_matches", "feature_summary",
        "candle_datetime", "scan_date", "signal", "rsi", "adx", "timeframe", "signal_key"), "30M"),
    Dataset("hourly_signal_scan", "signal", (
        "grade", "stock_code", "symbol", "candle_datetime", "signal", "entry_price", "adx", "atr", "rsi",
        "k", "d", "di_plus", "di_minus", "entry_volume_ratio", "stop_price", "qty", "day_return_pct",
        "day_strength", "day_range_position"), "1H"),
    Dataset("daily_signal_scan", "signal", (
        "grade", "stock_code", "symbol", "candle_datetime", "signal", "entry_price", "adx", "atr", "rsi",
        "k", "d", "di_plus", "di_minus", "entry_volume_ratio", "stop_price", "qty", "day_return_pct",
        "day_strength", "day_range_position", "direction", "timeframe", "signal_time", "signal_key"), "1D"),
    Dataset("open_positions", "open_position", (
        "trade_id", "symbol", "stock_code", "direction", "entry_time", "entry_price", "qty", "capital_used",
        "stop_price", "atr", "risk_per_share", "risk_amount", "status", "last_action", "entry_trend_rating",
        "entry_momentum_rating", "entry_signal_score", "entry_confidence_score", "entry_volume_ratio",
        "volume_qualified", "day_strength", "day_range_position", "day_return_pct", "entry_rsi", "entry_adx",
        "entry_di_plus", "entry_di_minus", "entry_dmi_spread", "entry_stoch_k", "entry_stoch_d",
        "entry_stoch_spread")),
    Dataset("closed_positions", "closed_trade", (
        "trade_id", "symbol", "stock_code", "direction", "entry_time", "exit_time", "entry_price",
        "exit_price", "qty", "capital_used", "risk_amount", "pnl", "pnl_pct", "r_multiple", "exit_reason",
        "entry_volume_ratio", "volume_qualified", "day_strength", "day_range_position", "day_return_pct")),
    Dataset("daily_equity", "pnl_history", (
        "date", "realized_pnl", "open_mtm", "total_equity", "open_positions", "capital_used",
        "portfolio_risk")),
)

TABLES = {
    "source_files": [
        ("dataset", "TEXT PRIMARY KEY"), ("file_name", "TEXT NOT NULL"), ("sha256", "TEXT NOT NULL"),
        ("sha256_after_import", "TEXT NOT NULL"), ("size_bytes", "INTEGER NOT NULL"),
        ("modified_utc", "TEXT NOT NULL"), ("header_json", "TEXT NOT NULL"), ("data_rows", "INTEGER NOT NULL"),
        ("stored_rows", "INTEGER NOT NULL"), ("quarantined_rows", "INTEGER NOT NULL")],
    "instruments": [
        ("stock_code", "TEXT PRIMARY KEY"), ("symbol", "TEXT"), ("mapping_status", "TEXT NOT NULL"),
        ("symbols_seen_json", "TEXT NOT NULL"), ("datasets_json", "TEXT NOT NULL"),
        ("occurrences", "INTEGER NOT NULL")],
    "app_signals": [
        ("dataset", "TEXT NOT NULL"), ("source_row", "INTEGER NOT NULL"), ("record_origin", "TEXT NOT NULL"),
        ("signal_id", "TEXT NOT NULL"), ("supplied_signal_key", "TEXT"), ("timeframe", "TEXT"),
        ("timeframe_basis", "TEXT"), ("stock_code", "TEXT"), ("symbol", "TEXT"), ("direction", "TEXT"),
        ("direction_basis", "TEXT"), ("signal_recorded", "TEXT"), ("grade", "TEXT"),
        ("candle_time_raw", "TEXT"), ("candle_time", "TEXT"), ("candle_time_status", "TEXT NOT NULL"),
        ("signal_time_raw", "TEXT"), ("signal_time", "TEXT"), ("signal_time_basis", "TEXT NOT NULL"),
        ("timezone_status", "TEXT NOT NULL"), ("entry_price", "REAL"), ("stop_price", "REAL"),
        ("stop_status", "TEXT NOT NULL"), ("qty", "REAL"), ("risk_per_share", "REAL"), ("risk_amount", "REAL"),
        ("signal_score", "REAL"), ("confidence_score", "REAL"), ("setup_quality", "TEXT"),
        ("trade_readiness", "TEXT"), ("signal_status", "TEXT"), ("trend_rating", "TEXT"),
        ("momentum_rating", "TEXT"), ("rsi", "REAL"), ("adx", "REAL"), ("atr", "REAL"), ("di_plus", "REAL"),
        ("di_minus", "REAL"), ("stoch_k", "REAL"), ("stoch_d", "REAL"), ("entry_volume_ratio", "REAL"),
        ("volume_qualified", "TEXT"), ("day_strength", "TEXT"), ("day_range_position", "REAL"),
        ("day_return_pct", "REAL"), ("research_available_recorded", "TEXT"),
        ("research_context_status", "TEXT NOT NULL"), ("research_context_json", "TEXT"),
        ("field_provenance_json", "TEXT NOT NULL"), ("quality_flags_json", "TEXT NOT NULL"),
        ("raw_record_json", "TEXT NOT NULL")],
    "factor_research": [
        ("dataset", "TEXT NOT NULL"), ("source_row", "INTEGER NOT NULL"), ("signal_id", "TEXT NOT NULL"),
        ("factor_key", "TEXT NOT NULL"), ("research_kind", "TEXT NOT NULL"), ("factor", "TEXT"),
        ("bucket", "TEXT"), ("trades", "INTEGER"), ("winners", "INTEGER"), ("losers", "INTEGER"),
        ("win_rate", "REAL"), ("avg_r", "REAL"), ("median_r", "REAL"), ("avg_hold", "REAL")],
    "app_open_positions": [
        ("dataset", "TEXT NOT NULL"), ("source_row", "INTEGER NOT NULL"), ("record_origin", "TEXT NOT NULL"),
        ("book_id", "TEXT NOT NULL"), ("trade_id", "TEXT"), ("stock_code", "TEXT"), ("symbol", "TEXT"),
        ("direction", "TEXT"), ("status", "TEXT"), ("last_action", "TEXT"), ("entry_time_raw", "TEXT"),
        ("entry_time", "TEXT"), ("entry_time_status", "TEXT NOT NULL"), ("timezone_status", "TEXT NOT NULL"),
        ("entry_price", "REAL"), ("qty", "REAL"), ("capital_used", "REAL"), ("stop_price", "REAL"),
        ("stop_status", "TEXT NOT NULL"), ("atr", "REAL"), ("risk_per_share", "REAL"), ("risk_amount", "REAL"),
        ("entry_trend_rating", "TEXT"), ("entry_momentum_rating", "TEXT"), ("entry_signal_score", "REAL"),
        ("entry_confidence_score", "REAL"), ("entry_rsi", "REAL"), ("entry_adx", "REAL"),
        ("entry_di_plus", "REAL"), ("entry_di_minus", "REAL"), ("entry_dmi_spread", "REAL"),
        ("entry_stoch_k", "REAL"), ("entry_stoch_d", "REAL"), ("entry_stoch_spread", "REAL"),
        ("entry_volume_ratio", "REAL"), ("volume_qualified", "TEXT"), ("day_strength", "TEXT"),
        ("day_range_position", "REAL"), ("day_return_pct", "REAL"), ("quality_flags_json", "TEXT NOT NULL"),
        ("raw_record_json", "TEXT NOT NULL")],
    "app_closed_trades": [
        ("dataset", "TEXT NOT NULL"), ("source_row", "INTEGER NOT NULL"), ("record_origin", "TEXT NOT NULL"),
        ("book_id", "TEXT NOT NULL"), ("trade_id", "TEXT"), ("stock_code", "TEXT"), ("symbol", "TEXT"),
        ("direction", "TEXT"), ("entry_time_raw", "TEXT"), ("entry_time", "TEXT"),
        ("entry_time_status", "TEXT NOT NULL"), ("exit_time_raw", "TEXT"), ("exit_time", "TEXT"),
        ("exit_time_status", "TEXT NOT NULL"), ("timezone_status", "TEXT NOT NULL"), ("entry_price", "REAL"),
        ("exit_price", "REAL"), ("qty", "REAL"), ("capital_used", "REAL"), ("risk_amount", "REAL"),
        ("pnl_recorded", "REAL"), ("pnl_pct_recorded", "REAL"), ("r_multiple_recorded", "REAL"),
        ("pnl_calculated", "REAL"), ("pnl_pct_calculated", "REAL"), ("r_multiple_calculated", "REAL"),
        ("exit_reason", "TEXT"), ("entry_volume_ratio", "REAL"), ("volume_qualified", "TEXT"),
        ("day_strength", "TEXT"), ("day_range_position", "REAL"), ("day_return_pct", "REAL"),
        ("quality_flags_json", "TEXT NOT NULL"), ("raw_record_json", "TEXT NOT NULL")],
    "recorded_pnl_history": [
        ("dataset", "TEXT NOT NULL"), ("source_row", "INTEGER NOT NULL"), ("record_origin", "TEXT NOT NULL"),
        ("date_raw", "TEXT"), ("date", "TEXT"), ("realized_pnl", "REAL"), ("open_mtm", "REAL"),
        ("recorded_cumulative_pnl", "REAL"), ("identity_holds", "INTEGER"), ("open_positions", "REAL"),
        ("capital_used", "REAL"), ("portfolio_risk", "REAL"), ("quality_flags_json", "TEXT NOT NULL"),
        ("raw_record_json", "TEXT NOT NULL")],
    "quarantined_rows": [
        ("dataset", "TEXT NOT NULL"), ("source_row", "INTEGER NOT NULL"), ("reason", "TEXT NOT NULL"),
        ("raw_fields_json", "TEXT NOT NULL")],
    "data_quality": [
        ("dataset", "TEXT NOT NULL"), ("check_code", "TEXT NOT NULL"), ("field", "TEXT"),
        ("severity", "TEXT NOT NULL"), ("affected_rows", "INTEGER NOT NULL"), ("description", "TEXT NOT NULL"),
        ("sample_source_rows_json", "TEXT NOT NULL")],
    "publication_meta": [("key", "TEXT PRIMARY KEY"), ("value_json", "TEXT NOT NULL")],
}
PRIMARY_KEYS = {
    "app_signals": "dataset, source_row", "factor_research": "dataset, source_row, factor_key",
    "app_open_positions": "dataset, source_row", "app_closed_trades": "dataset, source_row",
    "recorded_pnl_history": "dataset, source_row", "quarantined_rows": "dataset, source_row",
}
INDEXES = (
    "CREATE INDEX idx_signals_lookup ON app_signals (timeframe, stock_code, candle_time)",
    "CREATE INDEX idx_signals_candle ON app_signals (candle_time)",
    "CREATE INDEX idx_signals_id ON app_signals (signal_id)",
)

CHECKS = {
    "ragged_row": ("error", "Row field count differs from the header; kept only in quarantined_rows."),
    "extra_column": ("warning", "Column not in schema version 1; preserved in raw_record_json only."),
    "column_entirely_blank": ("info", "Every row is blank for this column; values stay unavailable (NULL)."),
    "numeric_unparseable": ("warning", "Value is not a finite number; stored as NULL, raw value kept."),
    "timestamp_missing": ("warning", "Timestamp is blank; normalized value is NULL."),
    "timestamp_incomplete_fragment": ("warning", "Time fragment without a calendar date; normalized value "
                                      "left NULL and no date was assumed."),
    "timestamp_unparseable": ("warning", "Timestamp not in an accepted full format; normalized value is NULL."),
    "timezone_unverified": ("info", "Source times carry no timezone offset; shown as source time, timezone "
                            "unverified."),
    "indicator_alias_used": ("info", "Primary column blank; value normalized from the named alias column "
                             "(see field_provenance_json)."),
    "indicator_alias_conflict": ("warning", "Primary and alias columns are both populated and disagree; "
                                 "primary kept."),
    "direction_derived_from_signal": ("info", "Blank direction filled from the recorded signal column."),
    "direction_signal_conflict": ("warning", "Recorded direction differs from the recorded signal."),
    "direction_missing": ("warning", "No direction or signal recorded."),
    "direction_invalid": ("warning", "Direction is not LONG or SHORT."),
    "timeframe_from_dataset_identity": ("info", "Blank timeframe filled from this file's timeframe identity."),
    "timeframe_mismatch": ("warning", "Recorded timeframe differs from this file's timeframe identity."),
    "signal_time_derived_from_candle_time": ("info", "Blank signal_time labelled as derived from candle time; "
                                             "this is not a recovered publication time."),
    "hourly_outside_original_loader_window": ("info", "Candle minute/hour outside the original Hourly "
                                              "loader's filter (minute 15, hours 10-15); retained, not excluded."),
    "stop_non_positive": ("warning", "Stop price is zero or negative; preserved but not valid for risk."),
    "stop_wrong_side": ("warning", "Stop is on the wrong side of entry for the direction; not valid for risk."),
    "stop_missing": ("warning", "Stop price is blank."),
    "research_context_parsed": ("info", "Factor-bucket summary parsed safely into JSON and factor_research. "
                                "Not historical trade matches or a backtest."),
    "research_context_invalid": ("warning", "research_context failed safe parsing/validation; not stored."),
    "research_context_missing": ("info", "research_context is blank."),
    "pnl_recalc_mismatch": ("warning", "Recorded P&L differs from (exit-entry)*qty*direction."),
    "pnl_pct_recalc_mismatch": ("warning", "Recorded pnl_pct differs from pnl/capital_used*100."),
    "r_multiple_recalc_mismatch": ("warning", "Recorded R differs from pnl/risk_amount."),
    "recalc_unavailable": ("info", "Comparison value not calculated because an input is missing or invalid."),
    "equity_identity_mismatch": ("warning", "total_equity differs from realized_pnl + open_mtm."),
    "duplicate_signal_identity": ("warning", "Another row has the same dataset/timeframe/code/direction/"
                                  "raw candle identity."),
    "duplicate_trade_id": ("warning", "trade_id repeats within this file."),
    "trade_id_open_and_closed": ("warning", "trade_id appears in both open and closed files."),
    "instrument_mapping_conflict": ("warning", "Stock code and display symbol are not one-to-one."),
}

_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FRAGMENT = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?(\.\d+)?$")
_TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")
_RESEARCH_TEXT = ("factor", "bucket")
_RESEARCH_INT = ("trades", "winners", "losers")
_RESEARCH_FLOAT = ("win_rate", "avg_r", "median_r", "avg_hold")


class ImportFailed(Exception):
    """The generation was not activated; nothing was written to the output folder."""


class Flags:
    """Per-row quality flags, aggregated per dataset for the data_quality table."""

    def __init__(self) -> None:
        self.counts: dict[tuple[str, str, str | None], list[int]] = defaultdict(list)

    def add(self, dataset: str, row: int, row_flags: list[str], code: str, field: str | None) -> None:
        if code not in CHECKS:
            raise KeyError(code)
        self.counts[(dataset, code, field)].append(row)
        row_flags.append(f"{code}:{field}" if field else code)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _number(rec: dict, column: str, ctx) -> float | None:
    raw = (rec.get(column) or "").strip()
    if not raw:
        return None
    if _NUMBER.match(raw):
        value = float(raw)
        if math.isfinite(value):
            return value
    ctx("numeric_unparseable", column)
    return None


def parse_timestamp(raw: str | None) -> tuple[str | None, str]:
    """Strict full-date formats only. Fragments never receive an assumed date."""
    value = (raw or "").strip()
    if not value:
        return None, "missing"
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, fmt).isoformat(), "parsed_naive"
        except ValueError:
            pass
    return None, "incomplete_fragment" if _FRAGMENT.match(value) else "unparseable"


def _timestamp(rec: dict, column: str, ctx) -> tuple[str | None, str]:
    value, status = parse_timestamp(rec.get(column))
    if status != "parsed_naive":
        ctx(f"timestamp_{status}", column)
    return value, status


def parse_research_context(raw: str | None) -> dict | None:
    """Safe literal parsing and strict structure validation. Never uses eval."""
    value = (raw or "").strip()
    if not value or len(value) > MAX_RESEARCH_CHARS:
        return None
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    expected = set(_RESEARCH_TEXT + _RESEARCH_INT + _RESEARCH_FLOAT)
    for key, item in parsed.items():
        if not isinstance(key, str) or not isinstance(item, dict) or set(item) != expected:
            return None
        if not all(isinstance(item[k], str) for k in _RESEARCH_TEXT):
            return None
        if not all(type(item[k]) is int and item[k] >= 0 for k in _RESEARCH_INT):
            return None
        for k in _RESEARCH_FLOAT:
            v = item[k]
            if v is not None and (type(v) not in (int, float) or not math.isfinite(v)):
                return None
    return parsed


def _stop_status(direction: str | None, entry: float | None, stop: float | None, ctx) -> str:
    if stop is None:
        ctx("stop_missing", "stop_price")
        return "missing"
    if stop <= 0:
        ctx("stop_non_positive", "stop_price")
        return "invalid_non_positive"
    if entry is None or direction not in DIRECTIONS:
        return "unverified"
    if (direction == "LONG" and stop >= entry) or (direction == "SHORT" and stop <= entry):
        ctx("stop_wrong_side", "stop_price")
        return "invalid_wrong_side"
    return "valid"


def _direction(rec: dict, ctx) -> str | None:
    direction = _text(rec.get("direction"))
    if direction is not None and direction not in DIRECTIONS:
        ctx("direction_invalid", "direction")
    return direction


def _signal_id(dataset: str, timeframe: str | None, code: str | None, direction: str | None, raw: str) -> str:
    identity = "|".join([dataset, timeframe or "", code or "", direction or "", raw])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _build_signal(ds: Dataset, row_no: int, rec: dict, ctx, research_rows: list) -> dict:
    tf_raw = _text(rec.get("timeframe"))
    if tf_raw:
        timeframe, tf_basis = tf_raw, "recorded"
        if tf_raw != ds.timeframe:
            ctx("timeframe_mismatch", "timeframe")
    else:
        timeframe, tf_basis = ds.timeframe, "dataset_identity"
        if "timeframe" in rec:
            ctx("timeframe_from_dataset_identity", "timeframe")

    recorded_direction, signal = _text(rec.get("direction")), _text(rec.get("signal"))
    if recorded_direction:
        direction, direction_basis = recorded_direction, "direction_column"
        if signal and signal != recorded_direction:
            ctx("direction_signal_conflict", "direction")
    elif signal:
        direction, direction_basis = signal, "signal_column"
        if "direction" in rec:
            ctx("direction_derived_from_signal", "direction")
    else:
        direction, direction_basis = None, None
        ctx("direction_missing", "direction")
    if direction is not None and direction not in DIRECTIONS:
        ctx("direction_invalid", "direction")

    candle_raw = rec.get("candle_datetime", "")
    candle_time, candle_status = _timestamp(rec, "candle_datetime", ctx)
    if candle_time:
        ctx("timezone_unverified", "candle_datetime")
        if ds.timeframe == "1H":
            moment = datetime.fromisoformat(candle_time)
            if moment.minute != 15 or not 10 <= moment.hour <= 15:
                ctx("hourly_outside_original_loader_window", "candle_datetime")

    if "signal_time" not in rec:
        signal_time_raw, signal_time, signal_basis = None, None, "not_supplied"
    elif (rec["signal_time"] or "").strip():
        signal_time_raw = rec["signal_time"]
        signal_time, status = _timestamp(rec, "signal_time", ctx)
        signal_basis = "recorded" if signal_time else f"recorded_{status}"
    elif candle_time:
        signal_time_raw, signal_time, signal_basis = rec["signal_time"], candle_time, "derived_from_candle_time"
        ctx("signal_time_derived_from_candle_time", "signal_time")
    else:
        signal_time_raw, signal_time, signal_basis = rec["signal_time"], None, "unavailable"

    aliases = SIGNAL_INDICATORS_30M if ds.timeframe == "30M" else SIGNAL_INDICATORS_1H_1D
    indicators, provenance = {}, {}
    for field, candidates in aliases.items():
        values = [(col, _number(rec, col, ctx)) for col in candidates if col in rec]
        populated = [(col, v) for col, v in values if v is not None]
        indicators[field], provenance[field] = (populated[0][1], populated[0][0]) if populated else (None, None)
        if populated and populated[0][0] != candidates[0]:
            ctx("indicator_alias_used", field)
        if len(populated) > 1 and any(abs(v - populated[0][1]) > 1e-9 for _, v in populated[1:]):
            ctx("indicator_alias_conflict", field)

    entry, stop = _number(rec, "entry_price", ctx), _number(rec, "stop_price", ctx)
    code = _text(rec.get("stock_code"))
    signal_id = _signal_id(ds.name, timeframe, code, direction, candle_raw)

    research_status, research_json = "not_supplied", None
    if "research_context" in rec:
        if not (rec["research_context"] or "").strip():
            research_status = "missing"
            ctx("research_context_missing", "research_context")
        elif (parsed := parse_research_context(rec["research_context"])) is None:
            research_status = "invalid"
            ctx("research_context_invalid", "research_context")
        else:
            research_status, research_json = "parsed_factor_buckets", _dumps(parsed)
            ctx("research_context_parsed", "research_context")
            for key, item in parsed.items():
                research_rows.append({
                    "dataset": ds.name, "source_row": row_no, "signal_id": signal_id, "factor_key": key,
                    "research_kind": "factor_bucket_summary", **item})

    return {
        "signal_id": signal_id, "supplied_signal_key": _text(rec.get("signal_key")),
        "timeframe": timeframe, "timeframe_basis": tf_basis, "stock_code": code,
        "symbol": _text(rec.get("symbol")), "direction": direction, "direction_basis": direction_basis,
        "signal_recorded": signal, "grade": _text(rec.get("grade")),
        "candle_time_raw": candle_raw, "candle_time": candle_time, "candle_time_status": candle_status,
        "signal_time_raw": signal_time_raw, "signal_time": signal_time, "signal_time_basis": signal_basis,
        "timezone_status": "unverified", "entry_price": entry, "stop_price": stop,
        "stop_status": _stop_status(direction, entry, stop, ctx), "qty": _number(rec, "qty", ctx),
        "risk_per_share": _number(rec, "risk_per_share", ctx), "risk_amount": _number(rec, "risk_amount", ctx),
        "signal_score": _number(rec, "signal_score", ctx), "confidence_score": _number(rec, "confidence_score", ctx),
        **{k: _text(rec.get(k)) for k in ("setup_quality", "trade_readiness", "signal_status", "trend_rating",
                                          "momentum_rating", "volume_qualified", "day_strength")},
        **indicators,
        "entry_volume_ratio": _number(rec, "entry_volume_ratio", ctx),
        "day_range_position": _number(rec, "day_range_position", ctx),
        "day_return_pct": _number(rec, "day_return_pct", ctx),
        "research_available_recorded": _text(rec.get("research_available")),
        "research_context_status": research_status, "research_context_json": research_json,
        "field_provenance_json": _dumps(provenance),
    }


def _build_open_position(ds: Dataset, row_no: int, rec: dict, ctx, _research) -> dict:
    direction = _direction(rec, ctx)
    entry_time, entry_status = _timestamp(rec, "entry_time", ctx)
    if entry_time:
        ctx("timezone_unverified", "entry_time")
    entry, stop = _number(rec, "entry_price", ctx), _number(rec, "stop_price", ctx)
    numeric = ("qty", "capital_used", "atr", "risk_per_share", "risk_amount", "entry_signal_score",
               "entry_confidence_score", "entry_rsi", "entry_adx", "entry_di_plus", "entry_di_minus",
               "entry_dmi_spread", "entry_stoch_k", "entry_stoch_d", "entry_stoch_spread", "entry_volume_ratio",
               "day_range_position", "day_return_pct")
    text = ("trade_id", "stock_code", "symbol", "status", "last_action", "entry_trend_rating",
            "entry_momentum_rating", "volume_qualified", "day_strength")
    return {
        "book_id": RECORD_ORIGIN, "direction": direction, "entry_time_raw": rec.get("entry_time"),
        "entry_time": entry_time, "entry_time_status": entry_status, "timezone_status": "unverified",
        "entry_price": entry, "stop_price": stop, "stop_status": _stop_status(direction, entry, stop, ctx),
        **{k: _number(rec, k, ctx) for k in numeric}, **{k: _text(rec.get(k)) for k in text},
    }


def _matches(recorded: float | None, calculated: float | None) -> bool:
    return recorded is None or calculated is None or abs(recorded - calculated) <= MATCH_TOLERANCE


def _build_closed_trade(ds: Dataset, row_no: int, rec: dict, ctx, _research) -> dict:
    direction = _direction(rec, ctx)
    entry_time, entry_status = _timestamp(rec, "entry_time", ctx)
    exit_time, exit_status = _timestamp(rec, "exit_time", ctx)
    if entry_time or exit_time:
        ctx("timezone_unverified", "entry_time" if entry_time else "exit_time")
    entry, exit_ = _number(rec, "entry_price", ctx), _number(rec, "exit_price", ctx)
    qty, capital = _number(rec, "qty", ctx), _number(rec, "capital_used", ctx)
    risk, pnl = _number(rec, "risk_amount", ctx), _number(rec, "pnl", ctx)
    pnl_pct, r_multiple = _number(rec, "pnl_pct", ctx), _number(rec, "r_multiple", ctx)

    pnl_calc = pnl_pct_calc = r_calc = None
    if None not in (entry, exit_, qty) and direction in DIRECTIONS:
        pnl_calc = (exit_ - entry) * qty * (1 if direction == "LONG" else -1)
    else:
        ctx("recalc_unavailable", "pnl")
    if pnl is not None and capital:
        pnl_pct_calc = pnl / capital * 100
    else:
        ctx("recalc_unavailable", "pnl_pct")
    if pnl is not None and risk:
        r_calc = pnl / risk
    else:
        ctx("recalc_unavailable", "r_multiple")
    for code, recorded, calculated in (("pnl_recalc_mismatch", pnl, pnl_calc),
                                       ("pnl_pct_recalc_mismatch", pnl_pct, pnl_pct_calc),
                                       ("r_multiple_recalc_mismatch", r_multiple, r_calc)):
        if not _matches(recorded, calculated):
            ctx(code, None)

    return {
        "book_id": RECORD_ORIGIN, "direction": direction, "trade_id": _text(rec.get("trade_id")),
        "stock_code": _text(rec.get("stock_code")), "symbol": _text(rec.get("symbol")),
        "entry_time_raw": rec.get("entry_time"), "entry_time": entry_time, "entry_time_status": entry_status,
        "exit_time_raw": rec.get("exit_time"), "exit_time": exit_time, "exit_time_status": exit_status,
        "timezone_status": "unverified", "entry_price": entry, "exit_price": exit_, "qty": qty,
        "capital_used": capital, "risk_amount": risk, "pnl_recorded": pnl, "pnl_pct_recorded": pnl_pct,
        "r_multiple_recorded": r_multiple, "pnl_calculated": pnl_calc, "pnl_pct_calculated": pnl_pct_calc,
        "r_multiple_calculated": r_calc, "exit_reason": _text(rec.get("exit_reason")),
        "entry_volume_ratio": _number(rec, "entry_volume_ratio", ctx),
        "volume_qualified": _text(rec.get("volume_qualified")), "day_strength": _text(rec.get("day_strength")),
        "day_range_position": _number(rec, "day_range_position", ctx),
        "day_return_pct": _number(rec, "day_return_pct", ctx),
    }


def _build_pnl_history(ds: Dataset, row_no: int, rec: dict, ctx, _research) -> dict:
    raw_date = rec.get("date")
    value = (raw_date or "").strip()
    try:
        if not _DATE.match(value):
            raise ValueError
        parsed_date = date.fromisoformat(value).isoformat()
    except ValueError:
        parsed_date = None
        ctx("timestamp_unparseable" if value else "timestamp_missing", "date")
    realized, mtm = _number(rec, "realized_pnl", ctx), _number(rec, "open_mtm", ctx)
    total = _number(rec, "total_equity", ctx)
    holds = None
    if None not in (realized, mtm, total):
        holds = int(abs(realized + mtm - total) <= MATCH_TOLERANCE)
        if not holds:
            ctx("equity_identity_mismatch", "total_equity")
    return {
        "date_raw": raw_date, "date": parsed_date, "realized_pnl": realized, "open_mtm": mtm,
        "recorded_cumulative_pnl": total, "identity_holds": holds,
        **{k: _number(rec, k, ctx) for k in ("open_positions", "capital_used", "portfolio_risk")},
    }


BUILDERS = {
    "signal": ("app_signals", _build_signal), "open_position": ("app_open_positions", _build_open_position),
    "closed_trade": ("app_closed_trades", _build_closed_trade),
    "pnl_history": ("recorded_pnl_history", _build_pnl_history),
}


def _read_source(path: Path) -> tuple[bytes, list[str], list[list[str]]]:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFailed(f"{path.name}: not valid UTF-8 ({exc}).") from exc
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise ImportFailed(f"{path.name}: file is empty (no header).")
    return data, rows[0], rows[1:]


def _inside_git_worktree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in (path, *path.parents))


def _insert(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    columns = [name for name, _ in TABLES[table]]
    for row in rows:
        unknown = set(row) - set(columns)
        if unknown:
            raise KeyError(f"{table}: unexpected fields {sorted(unknown)}")
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        [[row.get(c) for c in columns] for row in rows])


def _create_schema(conn: sqlite3.Connection) -> None:
    for table, columns in TABLES.items():
        body = ", ".join(f"{name} {kind}" for name, kind in columns)
        if table in PRIMARY_KEYS:
            body += f", PRIMARY KEY ({PRIMARY_KEYS[table]})"
        conn.execute(f"CREATE TABLE {table} ({body})")
    for statement in INDEXES:
        conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _instruments(tables: dict[str, list[dict]], flags: Flags) -> list[dict]:
    symbols, datasets, occurrences = defaultdict(set), defaultdict(set), Counter()
    for table in ("app_signals", "app_open_positions", "app_closed_trades"):
        for row in tables[table]:
            if row["stock_code"]:
                symbols[row["stock_code"]].add(row["symbol"])
                datasets[row["stock_code"]].add(row["dataset"])
                occurrences[row["stock_code"]] += 1
    codes_per_symbol = defaultdict(set)
    for code, seen in symbols.items():
        for symbol in seen:
            codes_per_symbol[symbol].add(code)
    result = []
    for code in sorted(symbols):
        seen = sorted(s for s in symbols[code] if s is not None)
        one_to_one = len(symbols[code]) == 1 and seen and len(codes_per_symbol[seen[0]]) == 1
        if not one_to_one:
            flags.add("instruments", 0, [], "instrument_mapping_conflict", code)
        result.append({
            "stock_code": code, "symbol": seen[0] if one_to_one else None,
            "mapping_status": "observed_one_to_one" if one_to_one else "conflict",
            "symbols_seen_json": _dumps(seen), "datasets_json": _dumps(sorted(datasets[code])),
            "occurrences": occurrences[code]})
    return result


def _capabilities(tables: dict[str, list[dict]]) -> dict:
    def state(available: bool, reason: str) -> dict:
        return {"status": "available" if available else "unavailable", "reason": reason}
    open_rows, closed_rows = tables["app_open_positions"], tables["app_closed_trades"]
    undated_open = sum(r["entry_time"] is None for r in open_rows)
    undated_closed = sum(r["entry_time"] is None or r["exit_time"] is None for r in closed_rows)
    return {
        "signals_archive": state(bool(tables["app_signals"]), "Supplied 30M/1H/1D signal archives; timezone "
                                 "unverified; not newly generated signals."),
        "factor_research": state(bool(tables["factor_research"]), "Supplied 30M factor-bucket summaries only; "
                                 "not historical trade matches or backtests."),
        "app_positions": state(bool(open_rows), f"Application-recorded, not proven broker positions; "
                               f"{undated_open} of {len(open_rows)} entry times lack a calendar date; no marks."),
        "app_closed_trades": state(bool(closed_rows), f"Application-recorded; {undated_closed} of "
                                   f"{len(closed_rows)} trades lack full entry/exit dates."),
        "recorded_pnl": state(bool(tables["recorded_pnl_history"]), "Recorded cumulative P&L (realized + open "
                              "MTM); opening funds, cash flows and fee basis unknown; not account equity."),
        "quotes": state(False, "No verified market observations in this snapshot."),
        "candles": state(False, "No OHLC history supplied; signal rows are not candles."),
        "broker_book": state(False, "No broker identifiers or broker-sourced records supplied."),
        "original_strategy": state(False, "Original signal engine/configuration not available; no new signals."),
        "backtests": state(False, "Original historical cache not supplied."),
        "reviews": state(False, "Trade review records not supplied."),
    }


def _frontiers(tables: dict[str, list[dict]]) -> dict:
    result = {}
    for ds in DATASETS:
        table, _ = BUILDERS[ds.kind]
        rows = [r for r in tables[table] if r["dataset"] == ds.name]
        if ds.kind == "signal":
            times = [r["candle_time"] for r in rows if r["candle_time"]]
            basis = "max candle_time in file; timezone unverified"
        elif ds.kind == "pnl_history":
            times, basis = [r["date"] for r in rows if r["date"]], "max date in file"
        else:
            times = [r["entry_time"] for r in rows if r["entry_time"]]
            basis = "max complete entry_time; most rows undated, so not a reliable frontier"
        result[ds.name] = {"latest_in_file": max(times) if times else None, "basis": basis}
    return result


def run_import(source_dir: Path, output_dir: Path, data_label: str = "supplied_csv") -> dict:
    """Build one validated generation. Raises ImportFailed and leaves no partial output on failure.

    data_label records what the CSVs are; `synthetic_test` makes every viewer show a synthetic banner.
    """
    source_dir = Path(source_dir).resolve()
    if not source_dir.is_dir():
        raise ImportFailed(f"Source folder not found: {source_dir}")
    return run_import_files({ds.name: source_dir / ds.file_name for ds in DATASETS}, output_dir, data_label)


def run_import_files(paths: dict[str, Path], output_dir: Path, data_label: str = "supplied_csv") -> dict:
    """run_import for six explicitly configured files, keyed by dataset name."""
    if data_label not in DATA_LABELS:
        raise ImportFailed(f"Unknown data label {data_label!r}")
    if set(paths) != {ds.name for ds in DATASETS}:
        raise ImportFailed(f"Expected exactly these sources: {', '.join(ds.name for ds in DATASETS)}")
    paths = {name: Path(p).resolve() for name, p in paths.items()}
    output_dir = Path(output_dir).resolve()
    if _inside_git_worktree(output_dir):
        raise ImportFailed(f"Output folder is inside a Git working tree; refusing: {output_dir}")
    missing = [str(paths[ds.name]) for ds in DATASETS if not paths[ds.name].is_file()]
    if missing:
        raise ImportFailed(f"Missing source files: {', '.join(missing)}")

    flags = Flags()
    tables: dict[str, list[dict]] = {name: [] for name in TABLES}
    sources, problems = [], []
    for ds in DATASETS:
        path = paths[ds.name]
        data, header, rows = _read_source(path)
        absent = [c for c in ds.columns if c not in header]
        if absent:
            problems.append(f"{path.name}: missing columns {absent}")
        if len(set(header)) != len(header):
            problems.append(f"{path.name}: duplicate column names")
        stat = path.stat()
        sources.append({"ds": ds, "path": path, "sha256": hashlib.sha256(data).hexdigest(),
                        "size_bytes": len(data), "header": header, "rows": rows,
                        "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    if problems:
        raise ImportFailed("Schema validation failed: " + "; ".join(problems))

    for src in sources:
        ds, header, rows = src["ds"], src["header"], src["rows"]
        table, builder = BUILDERS[ds.kind]
        for extra in (c for c in header if c not in ds.columns):
            flags.counts[(ds.name, "extra_column", extra)] = list(range(1, len(rows) + 1))
        well_formed = [r for r in rows if len(r) == len(header)]
        for column in ds.columns:
            idx = header.index(column)
            if well_formed and all(not r[idx].strip() for r in well_formed):
                flags.counts[(ds.name, "column_entirely_blank", column)] = list(range(1, len(rows) + 1))
        stored = quarantined = 0
        for row_no, fields in enumerate(rows, start=1):
            if len(fields) != len(header):
                flags.add(ds.name, row_no, [], "ragged_row", None)
                tables["quarantined_rows"].append({"dataset": ds.name, "source_row": row_no,
                                                   "reason": "ragged_row", "raw_fields_json": _dumps(fields)})
                quarantined += 1
                continue
            rec = dict(zip(header, fields))
            row_flags: list[str] = []
            ctx = partial(flags.add, ds.name, row_no, row_flags)
            row = builder(ds, row_no, rec, ctx, tables["factor_research"])
            row.update({"dataset": ds.name, "source_row": row_no, "record_origin": RECORD_ORIGIN,
                        "quality_flags_json": _dumps(sorted(set(row_flags))), "raw_record_json": _dumps(rec)})
            tables[table].append(row)
            stored += 1
        src.update(stored_rows=stored, quarantined_rows=quarantined)

    _flag_duplicates(tables, flags)
    tables["instruments"] = _instruments(tables, flags)

    created = datetime.now(timezone.utc).replace(microsecond=0)
    combined = hashlib.sha256("\n".join(f"{s['ds'].file_name}:{s['sha256']}" for s in sources).encode()).hexdigest()
    generation_id = f"{created:%Y%m%dT%H%M%SZ}-{combined[:8]}"
    snapshots = output_dir / "snapshots"
    final_dir, work_dir = snapshots / generation_id, snapshots / f".incomplete-{generation_id}"
    if final_dir.exists() or work_dir.exists():
        raise ImportFailed(f"Generation already exists: {final_dir}")

    work_dir.mkdir(parents=True)
    try:
        report = _write_generation(work_dir, generation_id, created, sources, tables, flags, data_label)
        report["snapshot_path"] = str(final_dir / SNAPSHOT_NAME)
        (work_dir / "validation_report.json").write_text(_dumps(report) + "\n", encoding="utf-8")
        (work_dir / "validation_report.txt").write_text(format_summary(report), encoding="utf-8")
        work_dir.rename(final_dir)
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return report


def _flag_row(flags: Flags, row: dict, code: str, field: str | None) -> None:
    row_flags = json.loads(row["quality_flags_json"])
    flags.add(row["dataset"], row["source_row"], row_flags, code, field)
    row["quality_flags_json"] = _dumps(sorted(set(row_flags)))


def _flag_duplicates(tables: dict[str, list[dict]], flags: Flags) -> None:
    for table, key, code, field in (("app_signals", "signal_id", "duplicate_signal_identity", None),
                                    ("app_open_positions", "trade_id", "duplicate_trade_id", "trade_id"),
                                    ("app_closed_trades", "trade_id", "duplicate_trade_id", "trade_id")):
        counts = Counter(r[key] for r in tables[table] if r[key] is not None)
        for row in tables[table]:
            if counts[row[key]] > 1:
                _flag_row(flags, row, code, field)
    shared = ({r["trade_id"] for r in tables["app_open_positions"]}
              & {r["trade_id"] for r in tables["app_closed_trades"]}) - {None}
    for row in tables["app_open_positions"] + tables["app_closed_trades"]:
        if row["trade_id"] in shared:
            _flag_row(flags, row, "trade_id_open_and_closed", "trade_id")


def _write_generation(work_dir: Path, generation_id: str, created: datetime, sources: list[dict],
                      tables: dict[str, list[dict]], flags: Flags, data_label: str) -> dict:
    quality = [{
        "dataset": dataset, "check_code": code, "field": field, "severity": CHECKS[code][0],
        "affected_rows": len(rows), "description": CHECKS[code][1],
        "sample_source_rows_json": _dumps(rows[:SAMPLE_ROWS])}
        for (dataset, code, field), rows in sorted(flags.counts.items(), key=lambda i: (i[0][0], i[0][1], i[0][2] or ""))]
    tables["data_quality"] = quality

    after = {s["ds"].name: _sha256_file(s["path"]) for s in sources}
    changed = [s["path"].name for s in sources if after[s["ds"].name] != s["sha256"]]
    if changed:
        raise ImportFailed(f"Source changed while importing; generation discarded: {', '.join(changed)}")
    tables["source_files"] = [{
        "dataset": s["ds"].name, "file_name": s["path"].name, "sha256": s["sha256"],
        "sha256_after_import": after[s["ds"].name], "size_bytes": s["size_bytes"],
        "modified_utc": s["modified_utc"], "header_json": _dumps(s["header"]), "data_rows": len(s["rows"]),
        "stored_rows": s["stored_rows"], "quarantined_rows": s["quarantined_rows"]} for s in sources]

    capabilities, frontiers = _capabilities(tables), _frontiers(tables)
    meta = {
        "schema_version": SCHEMA_VERSION, "generation_id": generation_id,
        "created_at_utc": created.isoformat(), "importer": "producer.import_csv",
        "importer_version": IMPORTER_VERSION, "mode": "uploaded_snapshot", "record_origin": RECORD_ORIGIN,
        "data_label": data_label,
        "cross_file_consistency": {"status": "unverified", "reason": CROSS_FILE_REASON},
        "timezone_policy": {"status": "unverified", "note": "Source timestamps carry no offset. Normalized "
                            "values are naive source times, not UTC and not confirmed Asia/Kolkata."},
        "time_frontiers": frontiers, "strategy_processed_through": None,
        "last_market_observation": None, "last_published": None,
        "recorded_pnl_semantics": "daily_equity.total_equity is stored as recorded_cumulative_pnl "
                                  "(realized_pnl + open_mtm). Opening funds, cash flows and fee basis are unknown.",
        "research_semantics": "factor_research holds supplied factor-bucket summaries, not original historical "
                              "trade matches or backtests.",
        "capabilities": capabilities,
    }
    tables["publication_meta"] = [{"key": k, "value_json": _dumps(v)} for k, v in meta.items()]

    db_path = work_dir / SNAPSHOT_NAME
    conn = sqlite3.connect(db_path)
    try:
        _create_schema(conn)
        for table in TABLES:
            _insert(conn, table, tables[table])
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {}
        for s in sources:
            table, _ = BUILDERS[s["ds"].kind]
            stored = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE dataset = ?", (s["ds"].name,)).fetchone()[0]
            held = conn.execute("SELECT COUNT(*) FROM quarantined_rows WHERE dataset = ?",
                                (s["ds"].name,)).fetchone()[0]
            counts[s["ds"].name] = (stored, held)
    finally:
        conn.close()
    if integrity != "ok":
        raise ImportFailed(f"SQLite integrity check failed: {integrity}")

    datasets = {}
    for s in sources:
        stored, held = counts[s["ds"].name]
        datasets[s["ds"].name] = {
            "file_name": s["path"].name, "sha256_before": s["sha256"], "sha256_after": after[s["ds"].name],
            "source_unchanged": after[s["ds"].name] == s["sha256"], "source_rows": len(s["rows"]),
            "stored_rows": stored, "quarantined_rows": held, "reconciled": stored + held == len(s["rows"])}
    if not all(d["reconciled"] for d in datasets.values()):
        raise ImportFailed("Row reconciliation failed; generation discarded.")
    return {
        "generation_id": generation_id, "schema_version": SCHEMA_VERSION, "created_at_utc": created.isoformat(),
        "data_label": data_label, "integrity_check": integrity, "datasets": datasets,
        "factor_research_rows": len(tables["factor_research"]), "instruments": len(tables["instruments"]),
        "instrument_mapping_conflicts": sum(i["mapping_status"] == "conflict" for i in tables["instruments"]),
        "quality_checks": [{k: q[k] for k in ("dataset", "check_code", "field", "severity", "affected_rows")}
                           for q in quality],
        "capabilities": capabilities,
    }


def format_summary(report: dict) -> str:
    lines = [f"NSE snapshot import - generation {report['generation_id']}",
             f"Snapshot: {report['snapshot_path']}", f"SQLite integrity: {report['integrity_check']}", "",
             f"{'Dataset':<22}{'Source rows':>12}{'Stored':>9}{'Quarantined':>13}  Reconciled  Source unchanged"]
    for name, d in report["datasets"].items():
        lines.append(f"{name:<22}{d['source_rows']:>12,}{d['stored_rows']:>9,}{d['quarantined_rows']:>13,}  "
                     f"{'yes' if d['reconciled'] else 'NO':<10}  {'yes' if d['source_unchanged'] else 'NO'}")
    lines += ["", f"Instruments: {report['instruments']} codes, {report['instrument_mapping_conflicts']} "
              f"mapping conflicts. Factor research rows: {report['factor_research_rows']}.", "",
              "Quality checks (row counts only; no record values):"]
    for q in report["quality_checks"]:
        if q["check_code"] == "timezone_unverified":
            continue
        field = f" [{q['field']}]" if q["field"] else ""
        lines.append(f"  {q['severity']:<8}{q['dataset']:<22}{q['check_code']}{field}: {q['affected_rows']:,}")
    lines += ["", "Capabilities:"]
    for name, c in report["capabilities"].items():
        lines.append(f"  {name:<18}{c['status']:<12}{c['reason']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the six application CSVs into a private SQLite snapshot.")
    parser.add_argument("--source-dir", required=True, type=Path, help="Folder containing the six CSV files.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Private folder outside Git.")
    args = parser.parse_args(argv)
    try:
        report = run_import(args.source_dir, args.output_dir)
    except ImportFailed as exc:
        print(f"IMPORT FAILED - no snapshot was created.\n{exc}", file=sys.stderr)
        return 1
    print(format_summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
