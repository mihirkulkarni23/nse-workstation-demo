"""Read-only loader for private uploaded_snapshot SQLite generations (schema 1).

Opens the database through a read-only URI, validates its structure and each feature's
reconciliation, and never substitutes demo or default values for missing data.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pandas as pd

SCHEMA_VERSION = 1
SNAPSHOT_NAME = "dashboard_snapshot.sqlite"
GENERATION_NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
SIGNAL_DATASETS = {"30M": "scanner_30m_signals", "1H": "hourly_signal_scan", "1D": "daily_signal_scan"}
HIDDEN_FLAGS = {"timezone_unverified", "research_context_parsed"}

COLUMNS = {
    "source_files": ["dataset", "file_name", "sha256", "sha256_after_import", "size_bytes", "modified_utc",
                     "data_rows", "stored_rows", "quarantined_rows"],
    "data_quality": ["dataset", "check_code", "field", "severity", "affected_rows", "description"],
    "app_signals": [
        "dataset", "source_row", "signal_id", "timeframe", "stock_code", "symbol", "direction", "direction_basis",
        "grade", "candle_time_raw", "candle_time", "candle_time_status", "signal_time_raw", "signal_time",
        "signal_time_basis", "timezone_status", "entry_price", "stop_price", "stop_status", "qty",
        "risk_per_share", "risk_amount", "signal_score", "confidence_score", "setup_quality", "trade_readiness",
        "trend_rating", "momentum_rating", "rsi", "adx", "atr", "di_plus", "di_minus", "stoch_k", "stoch_d",
        "entry_volume_ratio", "volume_qualified", "day_strength", "day_range_position", "day_return_pct",
        "research_context_status", "field_provenance_json", "quality_flags_json"],
    "factor_research": ["dataset", "source_row", "factor_key", "research_kind", "factor", "bucket", "trades",
                        "winners", "losers", "win_rate", "avg_r", "median_r", "avg_hold"],
    "app_open_positions": [
        "dataset", "source_row", "book_id", "trade_id", "stock_code", "symbol", "direction", "status",
        "entry_time_raw", "entry_time", "entry_time_status", "entry_price", "qty", "capital_used", "stop_price",
        "stop_status", "risk_per_share", "risk_amount", "entry_trend_rating", "entry_momentum_rating",
        "entry_signal_score", "entry_confidence_score", "entry_rsi", "entry_adx", "entry_di_plus",
        "entry_di_minus", "entry_stoch_k", "entry_stoch_d", "entry_volume_ratio", "quality_flags_json"],
    "app_closed_trades": [
        "dataset", "source_row", "book_id", "trade_id", "stock_code", "symbol", "direction", "entry_time_raw",
        "entry_time", "entry_time_status", "exit_time_raw", "exit_time", "exit_time_status", "entry_price",
        "exit_price", "qty", "capital_used", "risk_amount", "pnl_recorded", "pnl_pct_recorded",
        "r_multiple_recorded", "pnl_calculated", "pnl_pct_calculated", "r_multiple_calculated", "exit_reason",
        "quality_flags_json"],
    "recorded_pnl_history": ["dataset", "source_row", "date_raw", "date", "realized_pnl", "open_mtm",
                             "recorded_cumulative_pnl", "identity_holds", "quality_flags_json"],
}
CORE_TABLES = ("source_files", "data_quality")
FEATURE_TABLES = {
    "signals_archive": ("app_signals", tuple(SIGNAL_DATASETS.values())),
    "factor_research": ("factor_research", ("scanner_30m_signals",)),
    "app_positions": ("app_open_positions", ("open_positions",)),
    "app_closed_trades": ("app_closed_trades", ("closed_positions",)),
    "recorded_pnl": ("recorded_pnl_history", ("daily_equity",)),
}


class SnapshotError(Exception):
    """The snapshot cannot be shown at all. Callers must not fall back to demo data."""


def resolve_snapshot_path(path: str | Path) -> Path:
    """Accept a snapshot file, a snapshots folder, or the private folder containing snapshots/."""
    path = Path(path)
    if path.is_file():
        return path
    if not path.is_dir():
        raise SnapshotError(f"Snapshot path not found: {path}")
    root = path / "snapshots" if (path / "snapshots").is_dir() else path
    generations = sorted(d for d in root.iterdir()
                         if d.is_dir() and GENERATION_NAME.match(d.name) and (d / SNAPSHOT_NAME).is_file())
    if not generations:
        raise SnapshotError(f"No completed snapshot generation found in {root}")
    return generations[-1] / SNAPSHOT_NAME


def load_uploaded_snapshot(path: str | Path) -> dict:
    db = resolve_snapshot_path(path)
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SnapshotError(f"Cannot open {db}: {exc}") from exc
    try:
        return _load(conn, db)
    except sqlite3.DatabaseError as exc:
        raise SnapshotError(f"{db.name} is not a readable snapshot database ({exc}).") from exc
    finally:
        conn.close()


def _table(conn: sqlite3.Connection, table: str) -> tuple[pd.DataFrame | None, str | None]:
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not present:
        return None, f"table {table} is missing"
    absent = [c for c in COLUMNS[table] if c not in present]
    if absent:
        return None, f"table {table} lacks {', '.join(absent)}"
    return pd.read_sql_query(f"SELECT {', '.join(COLUMNS[table])} FROM {table}", conn), None


def _load(conn: sqlite3.Connection, db: Path) -> dict:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise SnapshotError(f"Snapshot schema version {version} is not supported (expected {SCHEMA_VERSION}).")
    try:
        meta = {key: json.loads(value) for key, value in conn.execute("SELECT key, value_json FROM publication_meta")}
    except (sqlite3.OperationalError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"Publication metadata unreadable: {exc}") from exc
    if meta.get("schema_version") != SCHEMA_VERSION or meta.get("mode") != "uploaded_snapshot":
        raise SnapshotError("Publication metadata does not describe a schema-1 uploaded_snapshot.")
    for key, kind in (("generation_id", str), ("created_at_utc", str), ("capabilities", dict),
                      ("time_frontiers", dict)):
        if not isinstance(meta.get(key), kind):
            raise SnapshotError(f"Publication metadata lacks {key}.")

    frames, problems = {}, {}
    for table in COLUMNS:
        frames[table], problems[table] = _table(conn, table)
    for table in CORE_TABLES:
        if frames[table] is None:
            raise SnapshotError(problems[table].capitalize() + ".")

    severity = dict(zip(frames["data_quality"].check_code, frames["data_quality"].severity))
    for table in ("app_signals", "app_open_positions", "app_closed_trades", "recorded_pnl_history"):
        if frames[table] is not None:
            _add_flag_columns(frames[table], severity)
    if frames["app_signals"] is not None:
        frames["app_signals"]["candle_dt"] = pd.to_datetime(frames["app_signals"].candle_time, format="ISO8601")
    return {
        "mode": "uploaded_snapshot", "path": str(db), "meta": meta,
        "features": _features(meta, frames, problems),
        "sources": frames["source_files"], "quality": frames["data_quality"],
        "signals": frames["app_signals"], "factors": frames["factor_research"],
        "positions": frames["app_open_positions"], "trades": frames["app_closed_trades"],
        "pnl": frames["recorded_pnl_history"],
    }


def _add_flag_columns(frame: pd.DataFrame, severity: dict) -> None:
    codes = frame.quality_flags_json.map(json.loads)
    shown = codes.map(lambda flags: [f for f in flags if f.split(":")[0] not in HIDDEN_FLAGS])
    frame["quality_flags"] = shown.map(", ".join)
    frame["warnings"] = shown.map(
        lambda flags: ", ".join(f for f in flags if severity.get(f.split(":")[0]) in ("warning", "error")))


def _features(meta: dict, frames: dict, problems: dict) -> dict:
    features = {name: {"status": str(cap.get("status")), "reason": str(cap.get("reason", ""))}
                for name, cap in meta["capabilities"].items() if isinstance(cap, dict)}
    sources = frames["source_files"].set_index("dataset")
    for name, (table, datasets) in FEATURE_TABLES.items():
        if features.get(name, {}).get("status") != "available":
            features.setdefault(name, {"status": "unavailable", "reason": "Not described by this snapshot."})
            continue
        issues = [problems[table]] if problems[table] else []
        for dataset in datasets:
            if dataset not in sources.index:
                issues.append(f"{dataset}: no source-file record")
                continue
            src = sources.loc[dataset]
            if src.stored_rows + src.quarantined_rows != src.data_rows:
                issues.append(f"{dataset}: stored plus quarantined rows do not equal source rows")
            if src.sha256 != src.sha256_after_import:
                issues.append(f"{dataset}: source changed during import")
            if frames[table] is not None and table != "factor_research":
                actual = int(frames[table].dataset.eq(dataset).sum())
                if actual != src.stored_rows:
                    issues.append(f"{dataset}: {actual:,} rows present but {int(src.stored_rows):,} recorded at import")
        if issues:
            features[name] = {"status": "invalid", "reason": "; ".join(issues)}
    return features


def _total(series: pd.Series) -> float | None:
    values = series.dropna()
    return float(values.sum()) if len(values) else None


def portfolio_stats(positions: pd.DataFrame) -> dict:
    valid = positions.stop_status.eq("valid")
    return {
        "count": len(positions), "long": int(positions.direction.eq("LONG").sum()),
        "short": int(positions.direction.eq("SHORT").sum()), "capital": _total(positions.capital_used),
        "risk_valid": _total(positions.risk_amount[valid]), "risk_excluded": int((~valid).sum()),
        "undated": int(positions.entry_time.isna().sum()),
    }


def journal_stats(trades: pd.DataFrame) -> dict:
    pnl = trades.pnl_recorded.dropna()
    gains, losses = float(pnl[pnl > 0].sum()), float(-pnl[pnl < 0].sum())
    r_multiple = trades.r_multiple_recorded.dropna()
    return {
        "count": len(trades), "pnl_known": len(pnl), "pnl": _total(pnl),
        "winners": int((pnl > 0).sum()), "losers": int((pnl < 0).sum()),
        "win_rate": float((pnl > 0).mean() * 100) if len(pnl) else None,
        "profit_factor": gains / losses if losses else None,
        "expectancy": float(pnl.mean()) if len(pnl) else None,
        "average_r": float(r_multiple.mean()) if len(r_multiple) else None,
        "r_flagged": int(trades.quality_flags.str.contains("r_multiple_recalc_mismatch").sum()),
        "undated": int((trades.entry_time.isna() | trades.exit_time.isna()).sum()),
    }


def pnl_curve_stats(history: pd.DataFrame) -> dict | None:
    series = history.dropna(subset=["date", "recorded_cumulative_pnl"]).sort_values("date")
    if series.empty:
        return None
    values = series.recorded_cumulative_pnl.reset_index(drop=True)
    decline = values.cummax() - values
    return {
        "days": len(series), "first_date": series.date.iloc[0], "last_date": series.date.iloc[-1],
        "latest": float(values.iloc[-1]), "peak": float(values.max()), "max_decline": float(decline.max()),
        "trough_date": series.date.iloc[int(decline.idxmax())],
        "identity_failures": int(series.identity_holds.eq(0).sum()),
        "undated_rows": len(history) - len(series),
    }
