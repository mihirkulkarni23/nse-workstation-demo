"""Read-only snapshot interface. The bundled fixture is entirely synthetic."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

SCHEMA_VERSION = 1
DEMO_PATH = Path(__file__).resolve().parent / "data" / "demo_snapshot.json"
NAV_OPTIONS = [
    "30M Scanner", "Hourly Signals", "Daily Signals", "Historical Cache",
    "Portfolio", "Journal", "Analytics", "Trade Reviews", "System Health",
]


def validate_snapshot(snapshot: dict) -> None:
    """Fail closed instead of presenting unavailable state as zero balances."""
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported snapshot schema.")
    if snapshot.get("mode") != "demo":
        raise ValueError("This release accepts synthetic demo snapshots only.")
    for key in ("generation_id", "generated_at", "capital", "signals", "positions",
                "trades", "equity", "reviews", "candles"):
        if key not in snapshot:
            raise ValueError(f"Missing snapshot field: {key}")
    if not isinstance(snapshot["capital"], (int, float)) or snapshot["capital"] <= 0:
        raise ValueError("Snapshot capital must be positive.")
    if not isinstance(snapshot["signals"], dict):
        raise ValueError("Signals must be grouped by timeframe.")
    for tf in ("30M", "1H", "1D"):
        rows = snapshot["signals"].get(tf)
        if not isinstance(rows, list):
            raise ValueError(f"Missing signal dataset: {tf}")
        for row in rows:
            for key in ("signal_key", "stock_code", "symbol", "direction", "candle_datetime",
                        "entry_price", "stop_price", "qty", "signal_score", "volume_qualified"):
                if key not in row:
                    raise ValueError(f"{tf} signal missing {key}.")
            if row["direction"] not in {"LONG", "SHORT"}:
                raise ValueError("Invalid signal direction.")
            if pd.isna(pd.to_datetime(row["candle_datetime"], errors="coerce", utc=True)):
                raise ValueError("Invalid signal timestamp.")
    for key in ("positions", "trades", "equity", "reviews"):
        if not isinstance(snapshot[key], list):
            raise ValueError(f"Invalid dataset: {key}")
    position_ids = [p["trade_id"] for p in snapshot["positions"]]
    trade_ids = [p["trade_id"] for p in snapshot["trades"]]
    if len(position_ids) != len(set(position_ids)) or len(trade_ids) != len(set(trade_ids)):
        raise ValueError("Duplicate trade identifiers.")
    if set(position_ids) & set(trade_ids):
        raise ValueError("A trade cannot be both open and closed.")
    for p in snapshot["positions"]:
        if p["direction"] not in {"LONG", "SHORT"}:
            raise ValueError("Invalid position direction.")
        for key in ("entry_price", "current_price", "stop_price", "qty", "risk_amount"):
            if not isinstance(p[key], (int, float)) or not math.isfinite(p[key]) or p[key] <= 0:
                raise ValueError(f"Invalid position {key}.")
    for trade in snapshot["trades"]:
        if not math.isfinite(trade["pnl"]):
            raise ValueError("Invalid trade P&L.")
    if any(r["trade_id"] not in set(trade_ids) for r in snapshot["reviews"]):
        raise ValueError("Review references an unknown trade.")


def load_snapshot() -> dict:
    """Only reads the fixture; importing this module never writes a file."""
    with DEMO_PATH.open(encoding="utf-8") as stream:
        snapshot = json.load(stream)
    validate_snapshot(snapshot)
    return snapshot


def table(snapshot: dict, name: str) -> pd.DataFrame:
    return pd.DataFrame(snapshot[name]).copy()


def positions_frame(snapshot: dict) -> pd.DataFrame:
    frame = table(snapshot, "positions")
    if frame.empty:
        return pd.DataFrame(columns=["trade_id", "symbol", "direction", "entry_price",
                                     "current_price", "qty", "mtm", "current_r", "capital_used"])
    sign = frame["direction"].map({"LONG": 1, "SHORT": -1})
    frame["mtm"] = ((frame["current_price"] - frame["entry_price"]) * frame["qty"] * sign).round(2)
    frame["current_r"] = (frame["mtm"] / frame["risk_amount"]).round(2)
    frame["capital_used"] = (frame["entry_price"] * frame["qty"]).round(2)
    return frame


def summary(snapshot: dict) -> dict:
    opened = positions_frame(snapshot)
    closed = table(snapshot, "trades")
    realized = float(closed["pnl"].sum()) if not closed.empty else 0.0
    mtm = float(opened["mtm"].sum())
    return {
        "open_positions": len(opened), "closed_positions": len(closed),
        "realized_pnl": realized, "open_mtm": mtm,
        "capital_deployed": float(opened["capital_used"].sum()),
        "portfolio_risk": float(opened["risk_amount"].sum()) if not opened.empty else 0.0,
        "total_equity": snapshot["capital"] + realized + mtm,
    }


def performance(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"count": 0, "win_rate": 0.0, "pnl": 0.0, "profit_factor": None,
                "average_r": 0.0, "expectancy": 0.0}
    pnl = trades["pnl"]
    gains = float(pnl.clip(lower=0).sum())
    losses = float(-pnl.clip(upper=0).sum())
    return {
        "count": len(trades), "win_rate": float((pnl > 0).mean() * 100),
        "pnl": float(pnl.sum()), "profit_factor": gains / losses if losses else None,
        "average_r": float(trades["r_multiple"].mean()), "expectancy": float(pnl.mean()),
    }


def participation(signals: pd.DataFrame) -> float:
    if signals.empty:
        return 0.0
    return float(signals["volume_qualified"].eq("YES").mean() * 100)
