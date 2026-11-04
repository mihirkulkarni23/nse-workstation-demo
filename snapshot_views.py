"""uploaded_snapshot views: supplied application records only, read-only.

Nothing here generates signals, fetches prices or writes files. Missing data is shown
as unavailable rather than replaced with demo figures or zeros.
"""

from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard_data import NAV_OPTIONS
from drive_reader import CHECK_INTERVAL_SECONDS, DriveState, connect, reader_config
from snapshot_reader import (
    SIGNAL_DATASETS, SnapshotError, journal_stats, load_uploaded_snapshot, pnl_curve_stats, portfolio_stats,
    resolve_snapshot_path,
)

MAX_TABLE_ROWS = 2_000
UNKNOWN = "Unknown"
NOT_SUPPLIED = "Not supplied"
TIME_NOTE = "Times are source times as written in the files; timezone unverified."
TIMEFRAMES = {"30M": "30-minute scanner", "1H": "Hourly signals", "1D": "Daily signals"}
LABELS = {
    "trade_id": "Trade ID", "symbol": "Symbol", "stock_code": "Code", "direction": "Direction", "grade": "Grade",
    "candle": "Candle (source time)", "entry_price": "Entry", "exit_price": "Exit", "stop_price": "Stop (recorded)",
    "stop_status": "Stop status", "qty": "Qty", "risk_per_share": "Risk/share (valid stop)",
    "risk_amount": "Risk (valid stop)", "signal_score": "Score", "confidence_score": "Confidence",
    "setup_quality": "Setup", "trade_readiness": "Readiness", "rsi": "RSI", "adx": "ADX", "atr": "ATR",
    "di_plus": "DI+", "di_minus": "DI−", "stoch_k": "Stoch %K", "stoch_d": "Stoch %D",
    "entry_volume_ratio": "Volume ratio", "volume_qualified": "Volume qualified", "day_strength": "Day strength",
    "warnings": "Quality warnings", "entry_display": "Entry time", "exit_display": "Exit time",
    "capital_used": "Capital (recorded)", "entry_signal_score": "Entry score",
    "entry_confidence_score": "Entry confidence", "entry_rsi": "Entry RSI", "entry_adx": "Entry ADX",
    "entry_di_plus": "Entry DI+", "entry_di_minus": "Entry DI−", "entry_stoch_k": "Entry %K",
    "entry_stoch_d": "Entry %D", "entry_trend_rating": "Trend rating", "entry_momentum_rating": "Momentum rating",
    "pnl_recorded": "P&L (recorded)", "pnl_calculated": "P&L (recalculated)", "pnl_pct_recorded": "P&L % (recorded)",
    "r_multiple_recorded": "R (recorded)", "r_multiple_calculated": "R (recalculated)", "exit_reason": "Exit reason",
}
SIGNAL_COLUMNS = ["symbol", "direction", "grade", "candle", "entry_price", "stop_price", "stop_status", "qty",
                  "risk_per_share", "risk_amount", "signal_score", "confidence_score", "setup_quality",
                  "trade_readiness", "rsi", "adx", "atr", "di_plus", "di_minus", "stoch_k", "stoch_d",
                  "entry_volume_ratio", "volume_qualified", "day_strength", "warnings"]
POSITION_COLUMNS = ["trade_id", "symbol", "direction", "entry_display", "entry_price", "stop_price", "stop_status",
                    "qty", "capital_used", "risk_amount", "entry_signal_score", "entry_confidence_score",
                    "entry_rsi", "entry_adx", "entry_di_plus", "entry_di_minus", "entry_stoch_k", "entry_stoch_d",
                    "entry_volume_ratio", "entry_trend_rating", "entry_momentum_rating", "warnings"]
TRADE_COLUMNS = ["trade_id", "symbol", "direction", "entry_display", "exit_display", "entry_price", "exit_price",
                 "qty", "capital_used", "risk_amount", "pnl_recorded", "pnl_calculated", "pnl_pct_recorded",
                 "r_multiple_recorded", "r_multiple_calculated", "exit_reason", "warnings"]


def money(value) -> str:
    if value is None or pd.isna(value):
        return UNKNOWN
    return f"{'-' if value < 0 else ''}₹{abs(value):,.2f}"


def fmt_time(value) -> str:
    if value is None or pd.isna(value):
        return UNKNOWN
    return pd.Timestamp(value).strftime("%d %b %Y · %H:%M")


def fmt_date(value) -> str:
    return UNKNOWN if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%d %b %Y")


def recorded_time(parsed, raw) -> str:
    if parsed is not None and not pd.isna(parsed):
        return fmt_time(parsed)
    return f"Date unknown (raw '{raw}')" if raw else UNKNOWN


def draw(fig: go.Figure, height: int = 320) -> None:
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", height=height,
                      margin=dict(l=10, r=10, t=24, b=10),
                      font=dict(family="Arial", color="#c9d4e5"),
                      xaxis=dict(showgrid=False), yaxis=dict(gridcolor="#24344b"))
    st.plotly_chart(fig, width="stretch", config={"displaylogo": False})


def show_table(frame: pd.DataFrame, columns: list[str], height: int | str = "auto") -> None:
    shown = frame[columns].round(2).rename(columns=LABELS)
    st.dataframe(shown, hide_index=True, width="stretch", height=height)


def absent_columns(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    return [c for c in columns if c in frame and frame[c].isna().all()]


def note_absent(absent: list[str]) -> None:
    if absent:
        st.caption("Not supplied in this file (blank in every row): " + ", ".join(LABELS.get(c, c) for c in absent)
                   + ". Other blank cells mean that value was not recorded for that row; none are shown as zero.")


def feature_ok(snap: dict, name: str) -> bool:
    feature = snap["features"].get(name, {"status": "unavailable", "reason": "Not described by this snapshot."})
    if feature["status"] == "available":
        return True
    message = f"{name.replace('_', ' ').capitalize()} — {feature['status'].upper()}: {feature['reason']}"
    (st.error if feature["status"] == "invalid" else st.info)(message)
    return False


def valid_risk(frame: pd.DataFrame) -> pd.DataFrame:
    valid = frame.stop_status.eq("valid")
    return frame.assign(**{c: frame[c].where(valid) for c in ("risk_per_share", "risk_amount") if c in frame})


def details(row: pd.Series, fields: list[tuple[str, str]]) -> pd.DataFrame:
    def text(value) -> str:
        if value is None or (not isinstance(value, str) and pd.isna(value)) or value == "":
            return NOT_SUPPLIED
        return f"{value:,.2f}" if isinstance(value, float) else str(value)
    return pd.DataFrame({"Field": [label for label, _ in fields], "Value": [text(row[c]) for _, c in fields]})


def signal_notes(signals: pd.DataFrame, tf: str) -> None:
    invalid = signals[signals.stop_status.ne("valid")]
    if not invalid.empty:
        kinds = ", ".join(f"{k}: {v:,}" for k, v in invalid.stop_status.value_counts().items())
        st.warning(f"{len(invalid):,} rows have an invalid or unverified recorded stop ({kinds}). They are kept for "
                   "audit and marked in 'Stop status'; their stop is not valid and no risk is shown for them.")
    undated = int(signals.candle_dt.isna().sum())
    if undated:
        st.warning(f"{undated:,} rows have no usable candle time. They appear only under 'All signals'.")
    if tf == "30M":
        st.info("RSI, ADX and ATR come from the rsi/adx/atr columns because entry_rsi/entry_adx/entry_atr are blank "
                "in the source. Historical-match fields (matches, win rate, average R, top matches) are blank in the "
                "source, so they are not shown. Each signal's supplied factor-bucket research is under 'Inspect'.")
    derived = int(signals.signal_time_basis.eq("derived_from_candle_time").sum())
    if derived:
        st.info(f"{derived:,} rows had blank direction, timeframe and signal time. Direction comes from the "
                "recorded signal column, timeframe from this file (1D), and signal time is labelled as derived from "
                "candle time; the original publication time is unknown.")
    outside = int(signals.quality_flags.str.contains("hourly_outside_original_loader_window").sum())
    if outside:
        st.info(f"{outside:,} rows have candle times outside the original Hourly loader's filter (minute :15, "
                "hours 10–15). The original dashboard hid them; they are kept and shown here.")


def signal_view(snap: dict, tf: str) -> None:
    dataset = SIGNAL_DATASETS[tf]
    st.subheader(TIMEFRAMES[tf])
    st.caption(f"Archived signals recorded by the application in {dataset}.csv. No signals are generated here. "
               f"{TIME_NOTE}")
    if not feature_ok(snap, "signals_archive"):
        return
    signals = snap["signals"][snap["signals"].dataset.eq(dataset)]
    if signals.empty:
        st.info("This snapshot contains no rows for this timeframe.")
        return
    last = signals.candle_dt.max()
    latest = signals[signals.candle_dt.eq(last)]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Archive rows", f"{len(signals):,}")
    c2.metric("Latest candle in file", fmt_time(last), help=TIME_NOTE)
    c3.metric("Signals at latest candle", len(latest))
    c4.metric("Long / Short (latest)", f"{latest.direction.eq('LONG').sum()} / {latest.direction.eq('SHORT').sum()}")
    signal_notes(signals, tf)

    c1, c2, c3 = st.columns(3)
    window = c1.selectbox("Signal window", ["Latest candle in file", "Earlier signals", "All signals"],
                          key=f"window_{tf}")
    direction = c2.selectbox("Direction", ["All", "LONG", "SHORT"], key=f"direction_{tf}")
    grade = c3.selectbox("Grade", ["All"] + sorted(signals.grade.dropna().unique()), key=f"grade_{tf}")
    c1, c2 = st.columns([3, 2])
    symbols = c1.multiselect("Symbols", sorted(signals.symbol.dropna().unique()), key=f"symbols_{tf}")
    dates = signals.candle_dt.dropna().dt.date
    span = None
    if not dates.empty:
        full = (dates.min(), dates.max())
        span = c2.date_input("Candle dates (source)", value=full, min_value=full[0], max_value=full[1],
                             key=f"dates_{tf}")
        span = span if isinstance(span, tuple) and len(span) == 2 and span != full else None

    shown = signals
    if window == "Latest candle in file":
        shown = shown[shown.candle_dt.eq(last)]
    elif window == "Earlier signals":
        shown = shown[shown.candle_dt.lt(last)]
    if direction != "All":
        shown = shown[shown.direction.eq(direction)]
    if grade != "All":
        shown = shown[shown.grade.eq(grade)]
    if symbols:
        shown = shown[shown.symbol.isin(symbols)]
    if span:
        shown = shown[shown.candle_dt.dt.date.between(*span)]
    total = len(shown)
    shown = shown.sort_values(["candle_dt", "source_row"], ascending=[False, True], na_position="last")
    view = valid_risk(shown.head(MAX_TABLE_ROWS)).assign(candle=lambda f: f.candle_dt.map(fmt_time))
    absent = absent_columns(signals, SIGNAL_COLUMNS)
    st.caption(f"{total:,} matching rows" + (f" · showing the {MAX_TABLE_ROWS:,} most recent; narrow the filters "
                                             "to see others" if total > MAX_TABLE_ROWS else ""))
    show_table(view, [c for c in SIGNAL_COLUMNS if c not in absent])
    note_absent(absent)
    if view.empty:
        st.info("No signals match these filters. Change the window or clear a filter.")
        return

    by_row = view.set_index("source_row")
    selected = st.selectbox(
        "Inspect a signal", by_row.index.tolist(), key=f"inspect_{tf}",
        format_func=lambda r: f"{by_row.at[r, 'symbol']} · {by_row.at[r, 'direction']} · "
                              f"{by_row.at[r, 'candle']} · row {r}")
    row = by_row.loc[selected]
    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"#### {row.symbol} · {row.direction or UNKNOWN}")
        st.dataframe(details(row, [
            ("Stock code", "stock_code"), ("Grade", "grade"), ("Candle time (raw)", "candle_time_raw"),
            ("Signal time", "signal_time"), ("Signal time basis", "signal_time_basis"),
            ("Direction basis", "direction_basis"), ("Entry", "entry_price"), ("Stop (recorded)", "stop_price"),
            ("Stop status", "stop_status"), ("Qty", "qty"), ("Risk (valid stop only)", "risk_amount"),
            ("Score", "signal_score"), ("Confidence", "confidence_score"), ("Setup", "setup_quality"),
            ("Readiness", "trade_readiness"), ("Trend rating", "trend_rating"),
            ("Momentum rating", "momentum_rating"), ("RSI", "rsi"), ("ADX", "adx"), ("ATR", "atr"),
            ("DI+", "di_plus"), ("DI−", "di_minus"), ("Stoch %K", "stoch_k"), ("Stoch %D", "stoch_d"),
            ("Volume ratio", "entry_volume_ratio"), ("Volume qualified", "volume_qualified"),
            ("Day strength", "day_strength"), ("Day range position", "day_range_position"),
            ("Day return %", "day_return_pct")]), hide_index=True, width="stretch")
        st.info("Price chart unavailable — this snapshot contains no OHLC candles, and signal rows are not price "
                "history.")
    with right:
        st.markdown("#### Source and quality")
        st.write(f"**Source:** `{dataset}.csv`, data row {selected}")
        st.write(f"**Timezone:** {row.timezone_status}")
        sources = {k: v for k, v in json.loads(row.field_provenance_json).items() if v}
        if sources:
            st.write("**Indicator source columns:** " + ", ".join(f"{k} ← {v}" for k, v in sources.items()))
        st.write(f"**Quality flags:** {row.quality_flags or 'none'}")
        if tf == "30M":
            factor_panel(snap, dataset, selected)
        st.caption("Read-only · no trading controls")


def factor_panel(snap: dict, dataset: str, source_row: int) -> None:
    st.markdown("#### Supplied factor research")
    if not feature_ok(snap, "factor_research"):
        return
    factors = snap["factors"]
    rows = factors[factors.dataset.eq(dataset) & factors.source_row.eq(source_row)]
    if rows.empty:
        st.info("No factor research was supplied for this signal.")
        return
    st.dataframe(rows[["factor_key", "bucket", "trades", "winners", "losers", "win_rate", "avg_r", "median_r",
                       "avg_hold"]].round(2).rename(columns={"factor_key": "Factor", "win_rate": "Win rate (as supplied)"}),
                 hide_index=True, width="stretch")
    st.caption("Factor-bucket summary supplied with this signal. It is not a list of historical trade matches, "
               "top matches or a backtest, and its source period is not recorded.")


def historical_view(snap: dict) -> None:
    st.subheader("Historical engine cache")
    st.info("ORIGINAL DATA NOT SUPPLIED — The original historical engine cache "
            "(historical_engine_cache.sqlite) was not part of the supplied files. Its cached trades, win rates, "
            "top matches and engine metadata cannot be shown, and none are recreated or estimated here.")
    st.markdown("- The supplied **signal archives** remain browsable in the 30M, Hourly and Daily sections. They are "
                "recorded signals, not backtest results.\n"
                "- Each 30M signal's supplied **factor-bucket research** is shown under 'Inspect a signal'.\n"
                "- A new backtest would need collected price history and defined rules; it is not part of this "
                "viewer.")
    feature = snap["features"].get("backtests", {})
    st.caption(f"Snapshot capability 'backtests': {feature.get('status', 'unavailable')} — "
               f"{feature.get('reason', 'not described')}")


def portfolio_view(snap: dict) -> None:
    st.subheader("Recorded open positions")
    st.caption("Application-recorded positions from open_positions.csv. They are not proven broker holdings. "
               + TIME_NOTE)
    if not feature_ok(snap, "app_positions"):
        return
    positions = snap["positions"]
    stats = portfolio_stats(positions)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Recorded positions", stats["count"])
    c2.metric("Long / Short", f"{stats['long']} / {stats['short']}")
    c3.metric("Capital (recorded)", money(stats["capital"]))
    c4.metric("Entry risk (valid stops)", money(stats["risk_valid"]))
    c5.metric("Open MTM", "Unavailable", help="No verified current prices are in this snapshot.")
    st.info("No verified current prices are in this snapshot, so MTM, current R and current value are unavailable. "
            "Nothing here changes positions.")
    if stats["undated"]:
        st.warning(f"{stats['undated']} of {stats['count']} entry times are time fragments without a calendar date "
                   "(for example a value like '18:00.8'). Their dates are shown as unknown, not guessed.")
    if stats["risk_excluded"]:
        st.warning(f"{stats['risk_excluded']} positions have an invalid or unverified stop; their risk is excluded "
                   "from the total above.")
    c1, c2 = st.columns(2)
    direction = c1.selectbox("Direction", ["All", "LONG", "SHORT"], key="positions_direction")
    dated = c2.selectbox("Entry date", ["All", "Date recorded", "Date unknown"], key="positions_dated")
    shown = positions
    if direction != "All":
        shown = shown[shown.direction.eq(direction)]
    if dated == "Date recorded":
        shown = shown[shown.entry_time.notna()]
    elif dated == "Date unknown":
        shown = shown[shown.entry_time.isna()]
    shown = valid_risk(shown).assign(entry_display=lambda f: [recorded_time(p, r) for p, r in
                                                               zip(f.entry_time, f.entry_time_raw)])
    absent = absent_columns(positions, POSITION_COLUMNS)
    st.caption(f"{len(shown)} matching positions · file order")
    show_table(shown, [c for c in POSITION_COLUMNS if c not in absent])
    note_absent(absent)


def journal_view(snap: dict) -> None:
    st.subheader("Recorded closed trades")
    st.caption("Application-recorded trades from closed_positions.csv. Not proven broker executions.")
    if not feature_ok(snap, "app_closed_trades"):
        return
    trades = snap["trades"]
    c1, c2, c3 = st.columns(3)
    direction = c1.selectbox("Direction", ["All", "LONG", "SHORT"], key="journal_direction")
    outcome = c2.selectbox("Outcome", ["All", "Winners", "Losers"], key="journal_outcome")
    reason = c3.selectbox("Exit reason", ["All"] + sorted(trades.exit_reason.dropna().unique()), key="journal_reason")
    shown = trades
    if direction != "All":
        shown = shown[shown.direction.eq(direction)]
    if outcome == "Winners":
        shown = shown[shown.pnl_recorded > 0]
    elif outcome == "Losers":
        shown = shown[shown.pnl_recorded < 0]
    if reason != "All":
        shown = shown[shown.exit_reason.eq(reason)]
    stats = journal_stats(shown)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Closed trades", stats["count"])
    c2.metric("Reported P&L", money(stats["pnl"]))
    c3.metric("Win rate", UNKNOWN if stats["win_rate"] is None else f"{stats['win_rate']:.1f}%")
    c4.metric("Average R (recorded)", UNKNOWN if stats["average_r"] is None else f"{stats['average_r']:.2f}R")
    c5.metric("R checks flagged", stats["r_flagged"], help="Recorded R differs from recorded P&L ÷ recorded risk.")
    if stats["undated"]:
        st.warning(f"{stats['undated']} of {stats['count']} trades have no calendar date for entry and/or exit. "
                   "Holding periods and chronological order are unavailable; rows are shown in file order.")
    shown = shown.assign(entry_display=lambda f: [recorded_time(p, r) for p, r in zip(f.entry_time, f.entry_time_raw)],
                         exit_display=lambda f: [recorded_time(p, r) for p, r in zip(f.exit_time, f.exit_time_raw)])
    show_table(shown, TRADE_COLUMNS)
    st.caption("Recalculated values are independent checks: P&L = (exit − entry) × qty × direction; R = recorded P&L "
               "÷ recorded risk. Reported P&L is as recorded; whether it includes fees is unknown.")


def analytics_view(snap: dict) -> None:
    st.subheader("Recorded performance")
    st.caption("Two separate source products. They cover different records, cannot be dated against each other, and "
               "are not reconciled.")
    st.markdown("#### Recorded cumulative P&L · daily_equity.csv")
    if feature_ok(snap, "recorded_pnl"):
        history = snap["pnl"]
        stats = pnl_curve_stats(history)
        if stats is None:
            st.info("No dated rows in the recorded P&L history.")
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Latest recorded cumulative P&L", money(stats["latest"]), help=fmt_date(stats["last_date"]))
            c2.metric("Peak recorded cumulative P&L", money(stats["peak"]))
            c3.metric("Largest peak-to-trough decline", money(stats["max_decline"]),
                      help=f"Trough on {fmt_date(stats['trough_date'])}. Currency only; no % is possible "
                           "without opening funds.")
            c4.metric("Days recorded", stats["days"],
                      help=f"{fmt_date(stats['first_date'])} – {fmt_date(stats['last_date'])}")
            series = history.dropna(subset=["date"]).sort_values("date")
            fig = go.Figure()
            for column, name, color in (("recorded_cumulative_pnl", "Recorded cumulative P&L", "#4adeb0"),
                                        ("realized_pnl", "Realized P&L", "#a0b8de"),
                                        ("open_mtm", "Open MTM (recorded)", "#fbbf24")):
                fig.add_trace(go.Scatter(x=series.date, y=series[column], mode="lines", name=name,
                                         line=dict(color=color, width=3 if column == "recorded_cumulative_pnl" else 1.5)))
            fig.update_layout(legend=dict(orientation="h", y=1.12))
            draw(fig)
            identity = ("Every row satisfies total = realized P&L + open MTM." if not stats["identity_failures"] else
                        f"{stats['identity_failures']} rows do not satisfy total = realized P&L + open MTM.")
            st.caption(f"{fmt_date(stats['first_date'])} – {fmt_date(stats['last_date'])}. {identity} This is the "
                       "application's recorded cumulative P&L, not funded account equity: opening funds, cash flows "
                       "and fee basis are unknown, so no percentage returns are shown.")
    st.divider()
    st.markdown("#### Closed-trade aggregates · closed_positions.csv")
    if feature_ok(snap, "app_closed_trades"):
        trades = snap["trades"]
        stats = journal_stats(trades)
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Closed trades", stats["count"])
        c2.metric("Reported P&L (sum)", money(stats["pnl"]))
        c3.metric("Win rate", UNKNOWN if stats["win_rate"] is None else f"{stats['win_rate']:.1f}%")
        c4.metric("Profit factor", "n/a" if stats["profit_factor"] is None else f"{stats['profit_factor']:.2f}",
                  help="Gross reported gains ÷ gross reported losses; n/a when there are no losses.")
        c5.metric("Expectancy / trade", money(stats["expectancy"]))
        grouped = trades.groupby("exit_reason", dropna=False).pnl_recorded.sum(min_count=1)
        draw(go.Figure(go.Bar(x=[str(k) for k in grouped.index], y=grouped.values,
                              marker_color=["#4adeb0" if v >= 0 else "#fb7185" for v in grouped.fillna(0).values])),
             260)
        st.caption("Reported P&L by exit reason. These trades have no calendar dates, so they cannot be placed on "
                   "the daily curve above or shown as a time series.")


def review_view(snap: dict) -> None:
    st.subheader("Trade reviews")
    st.info("ORIGINAL DATA NOT SUPPLIED — Trade review records (trade_reviews.csv) were not part of the supplied "
            "files. No setup grades, execution grades, mistakes, notes or lessons exist in this snapshot, and none "
            "are invented. Writing or saving reviews is not available in this read-only viewer.")
    feature = snap["features"].get("reviews", {})
    st.caption(f"Snapshot capability 'reviews': {feature.get('status', 'unavailable')} — "
               f"{feature.get('reason', 'not described')}")


def health_view(snap: dict) -> None:
    meta = snap["meta"]
    st.subheader("System health")
    st.info("These checks describe the private snapshot file only. No trading engine, market feed, broker or Drive "
            "connection is attached, so their health is not reported.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Data mode", "Uploaded snapshot")
    c2.metric("Generation", meta["generation_id"])
    c3.metric("Imported (UTC)", fmt_time(meta["created_at_utc"]))
    c4.metric("Access", "Read-only")

    st.markdown("#### Feature availability")
    features = pd.DataFrame([{"Feature": k, "Status": v["status"], "Reason": v["reason"]}
                             for k, v in snap["features"].items()])
    st.dataframe(features, hide_index=True, width="stretch")

    st.markdown("#### Source files")
    sources = snap["sources"]
    st.dataframe(pd.DataFrame({
        "File": sources.file_name, "Source rows": sources.data_rows, "Stored": sources.stored_rows,
        "Quarantined": sources.quarantined_rows,
        "Reconciled": (sources.stored_rows + sources.quarantined_rows).eq(sources.data_rows).map({True: "yes", False: "NO"}),
        "Unchanged during import": sources.sha256.eq(sources.sha256_after_import).map({True: "yes", False: "NO"}),
        "SHA-256 (first 16)": sources.sha256.str[:16], "File modified (UTC)": sources.modified_utc.map(fmt_time)}),
        hide_index=True, width="stretch")

    st.markdown("#### Data dates")
    frontiers = [{"Dataset": k, "Latest in file": fmt_time(v.get("latest_in_file")), "Basis": v.get("basis", "")}
                 for k, v in meta["time_frontiers"].items()]
    frontiers += [{"Dataset": "Strategy processing", "Latest in file": UNKNOWN,
                   "Basis": "No engine completion record was supplied"},
                  {"Dataset": "Market observations", "Latest in file": "None",
                   "Basis": "No price feed in this snapshot"},
                  {"Dataset": "Cross-file consistency", "Latest in file": "Unverified",
                   "Basis": meta.get("cross_file_consistency", {}).get(
                       "reason", "No upstream completion marker; the six files may come from different cycles.")}]
    st.dataframe(pd.DataFrame(frontiers), hide_index=True, width="stretch")
    st.caption(TIME_NOTE + " A newer snapshot does not make the underlying signals newer.")

    st.markdown("#### Data quality checks")
    quality = snap["quality"]
    level = st.selectbox("Severity", ["Warnings and errors", "All", "Information only"], key="health_severity")
    if level == "Warnings and errors":
        quality = quality[quality.severity.isin(["warning", "error"])]
    elif level == "Information only":
        quality = quality[quality.severity.eq("info")]
    st.dataframe(quality.rename(columns={"check_code": "Check", "affected_rows": "Rows"}), hide_index=True,
                 width="stretch")
    st.caption(f"Snapshot file: {snap['path']} · schema {meta['schema_version']} · importer "
               f"{meta.get('importer_version', UNKNOWN)}")


@st.cache_resource(max_entries=2, show_spinner="Loading private snapshot…")
def _cached_snapshot(path: str, size: int, modified_ns: int) -> dict:
    return load_uploaded_snapshot(path)


def header(snap: dict, source: str, status: str, reload_label: str, reload_note: str, on_reload=None,
           notices: tuple[str, ...] = ()) -> None:
    meta, features = snap["meta"], snap["features"]
    st.markdown('<div class="eyebrow">NSE / F&O · MONITORING WORKSPACE</div>', unsafe_allow_html=True)
    left, right = st.columns([5, 1])
    with left:
        st.title("Trading workstation")
        st.caption("Supplied application records — one read-only workspace.")
    with right:
        st.write("")
        if st.button(reload_label, width="stretch", on_click=on_reload):
            st.rerun()
    if meta.get("data_label") == "synthetic_test":
        st.warning(f"SYNTHETIC TEST SNAPSHOT — Made-up records for testing the data pipeline, read from the {source}. "
                   f"Not real trading data. Generation {meta['generation_id']}.", icon="🧪")
    else:
        st.info(f"UPLOADED SNAPSHOT — Application-recorded data imported from the six supplied CSV files, read from "
                f"the {source}. Not live, not broker-verified; timezone unverified. Generation "
                f"{meta['generation_id']}.", icon="📁")
    for notice in notices:
        st.warning(notice, icon="⚠️")
    frontiers = meta["time_frontiers"]
    latest = [f"{label} {fmt_time(frontiers.get(ds, {}).get('latest_in_file'))}" for label, ds in
              (("30M", "scanner_30m_signals"), ("1H", "hourly_signal_scan"), ("1D", "daily_signal_scan"))]
    st.caption("Signals in file through " + " · ".join(latest) + " · recorded P&L through "
               + fmt_date(frontiers.get("daily_equity", {}).get("latest_in_file"))
               + ". " + reload_note)

    signals_ok = features.get("signals_archive", {}).get("status") == "available"
    positions_ok = features.get("app_positions", {}).get("status") == "available"
    trades_ok = features.get("app_closed_trades", {}).get("status") == "available"
    if signals_ok:
        s30 = snap["signals"][snap["signals"].dataset.eq("scanner_30m_signals")]
        latest_30m = int(s30.candle_dt.eq(s30.candle_dt.max()).sum()) if s30.candle_dt.notna().any() else UNKNOWN
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("30M signals (latest candle)", latest_30m if signals_ok else "Unavailable")
    c2.metric("Recorded open positions", len(snap["positions"]) if positions_ok else "Unavailable")
    c3.metric("Recorded closed trades", len(snap["trades"]) if trades_ok else "Unavailable")
    c4.metric("Capital (recorded, open)", money(portfolio_stats(snap["positions"])["capital"]) if positions_ok
              else "Unavailable")
    c5.metric("Open MTM", "Unavailable", help="No verified current prices are in this snapshot.")
    c6.metric("Source status", status)
    st.write("")


def render(path: str) -> None:
    try:
        db = resolve_snapshot_path(path)
        stat = db.stat()
        snap = _cached_snapshot(str(db), stat.st_size, stat.st_mtime_ns)
    except (SnapshotError, OSError) as exc:
        st.error(f"UPLOADED SNAPSHOT UNAVAILABLE — {exc}\n\nNo demo or synthetic data is shown in its place. Check "
                 "NSE_SNAPSHOT_PATH or re-run the importer.", icon="⛔")
        st.stop()
    show(snap, header_args=("local private folder", "SNAPSHOT", "↻ Reload snapshot",
                            "Reload shows the newest imported snapshot; it does not fetch new data."))


@st.cache_resource(show_spinner=False)
def _drive_state() -> DriveState:
    """One per server process and empty after every restart, so the snapshot is downloaded again."""
    return DriveState()


def _request_drive_check() -> None:
    st.session_state["_drive_check_now"] = True


def fmt_utc(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else UNKNOWN


def render_drive(secrets) -> None:
    try:
        folder_id, account = reader_config(secrets)
    except ValueError as exc:
        st.error(f"DATA SOURCE NOT CONFIGURED — {exc} No demo or synthetic data is shown in its place. Follow "
                 "docs/drive_setup.md to fill in Streamlit Secrets.", icon="⛔")
        st.stop()
    state = _drive_state()
    with st.spinner("Checking the private Google Drive folder…"):
        state.refresh(lambda: connect(account), folder_id, force=st.session_state.pop("_drive_check_now", False))
    current = state.active  # one generation for this whole rerun
    if current is None:
        st.error(f"DRIVE SNAPSHOT UNAVAILABLE — {state.last_error or 'Drive has not been checked yet.'}\n\nNo demo "
                 f"or synthetic data is shown in its place. Last check: {fmt_utc(state.last_check)}. The app checks "
                 f"again every {CHECK_INTERVAL_SECONDS // 60} minutes.", icon="⛔")
        st.button("↻ Check Drive now", on_click=_request_drive_check)
        st.stop()

    manifest, generation = current.manifest, current.manifest["generation_id"]
    if st.session_state.get("_drive_generation") not in (None, generation):
        for key in [k for k in st.session_state if k != "dashboard_view"]:
            del st.session_state[key]  # filters from the previous generation may not fit the new data
    st.session_state["_drive_generation"] = generation
    notices = ()
    if state.stale:
        notices = (f"STALE — Showing the last verified generation {generation} (downloaded "
                   f"{fmt_utc(current.activated_at)}). The latest Drive check at {fmt_utc(state.last_check)} failed: "
                   f"{state.last_error} Figures may be out of date.",)
    show(current.snapshot, header_args=(
        "private Google Drive folder", "STALE" if state.stale else "SNAPSHOT", "↻ Check Drive now",
        f"Exported {manifest.get('exported_at_utc', UNKNOWN)}; checksum verified; Drive last checked "
        f"{fmt_utc(state.last_check)}. Checking Drive picks up a newer uploaded generation; it does not fetch "
        "market data.", _request_drive_check, notices))


def show(snap: dict, header_args: tuple) -> None:
    header(snap, *header_args)
    selected_view = st.radio("Dashboard section", NAV_OPTIONS, horizontal=True, key="dashboard_view",
                             label_visibility="collapsed")
    st.divider()
    if selected_view in NAV_OPTIONS[:3]:
        signal_view(snap, {"30M Scanner": "30M", "Hourly Signals": "1H", "Daily Signals": "1D"}[selected_view])
    elif selected_view == "Historical Cache":
        historical_view(snap)
    elif selected_view == "Portfolio":
        portfolio_view(snap)
    elif selected_view == "Journal":
        journal_view(snap)
    elif selected_view == "Analytics":
        analytics_view(snap)
    elif selected_view == "Trade Reviews":
        review_view(snap)
    else:
        health_view(snap)
    st.divider()
    records = "Synthetic test records" if snap["meta"].get("data_label") == "synthetic_test" else \
        "Supplied application records"
    st.caption(f"UPLOADED SNAPSHOT / READ-ONLY · {records} · No orders, position updates, review "
               "edits, equity writes, signal generation or backtests")
