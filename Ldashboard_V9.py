"""Self-contained read-only demonstration of the nine-section workstation.

This is a new demo entry point. It does not import or execute the trading engine.
"""

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard_data import (
    NAV_OPTIONS, load_snapshot, participation, performance, positions_frame, summary, table,
)

st.set_page_config(page_title="NSE Workstation | Demo", page_icon="📊", layout="wide")
st.markdown("""
<style>
 .block-container {padding-bottom:2rem; max-width:1560px;}
 [data-testid="stMetric"] {background:#131f33; border:1px solid #24344b;
   border-radius:12px; padding:14px 18px;}
 [data-testid="stMetricLabel"] {color:#a8b8ce;}
 [data-testid="stMetricValue"] {font-size:1.65rem;}
 h1 {letter-spacing:-.04em;} h2,h3 {letter-spacing:-.02em;}
 .eyebrow {font-size:.78rem; letter-spacing:.16em; color:#4adeb0; font-weight:650;}
</style>
""", unsafe_allow_html=True)


def money(value: float) -> str:
    return f"₹{value:,.0f}"


def draw(fig: go.Figure, height: int = 320) -> None:
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", height=height,
                      margin=dict(l=10, r=10, t=24, b=10),
                      font=dict(family="Arial", color="#c9d4e5"),
                      xaxis=dict(showgrid=False), yaxis=dict(gridcolor="#24344b"))
    st.plotly_chart(fig, width="stretch", config={"displaylogo": False})


def dataframe(frame: pd.DataFrame, height: int | str = "auto") -> None:
    st.dataframe(frame, hide_index=True, width="stretch", height=height,
                 column_config={
                     "entry_price": st.column_config.NumberColumn("Entry", format="₹%.2f"),
                     "current_price": st.column_config.NumberColumn("Mark", format="₹%.2f"),
                     "stop_price": st.column_config.NumberColumn("Stop", format="₹%.2f"),
                     "pnl": st.column_config.NumberColumn("P&L", format="₹%.2f"),
                     "mtm": st.column_config.NumberColumn("MTM", format="₹%.2f"),
                     "signal_score": st.column_config.ProgressColumn("Score", min_value=0, max_value=10, format="%.1f"),
                 })


def candles(snapshot: dict, symbol: str, entry: float | None = None, stop: float | None = None) -> None:
    frame = pd.DataFrame(snapshot["candles"].get(symbol, []))
    if frame.empty:
        st.info("No demo candles for this symbol.")
        return
    fig = go.Figure(go.Candlestick(x=frame["time"], open=frame["open"], high=frame["high"],
                                  low=frame["low"], close=frame["close"],
                                  increasing_line_color="#4adeb0", decreasing_line_color="#fb7185",
                                  name=symbol))
    if entry is not None:
        fig.add_hline(y=entry, line_dash="dot", line_color="#a0b8de", annotation_text="Entry")
    if stop is not None:
        fig.add_hline(y=stop, line_dash="dash", line_color="#fb7185", annotation_text="Stop")
    fig.update_layout(xaxis_rangeslider_visible=False)
    draw(fig, 330)
    st.caption("Synthetic 5-minute price path · illustrative only · timestamps shown in IST")


def signal_view(snapshot: dict, timeframe: str) -> None:
    labels = {"30M": "30-minute scanner", "1H": "Hourly signals", "1D": "Daily signals"}
    signals = pd.DataFrame(snapshot["signals"][timeframe])
    st.subheader(labels[timeframe])
    st.caption("Published demo signals with inspection-only controls. No signal generation runs here.")
    if signals.empty:
        st.info("This snapshot contains no signals for this timeframe.")
        return
    last = signals["candle_datetime"].max()
    latest = signals[signals["candle_datetime"].eq(last)].copy()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Latest signals", len(latest))
    c2.metric("Long / Short", f"{latest.direction.eq('LONG').sum()} / {latest.direction.eq('SHORT').sum()}")
    c3.metric("Volume qualified", f"{participation(latest):.0f}%")
    c4.metric("Signal candle · IST", pd.Timestamp(last).strftime("%d %b · %H:%M"))
    st.write("")
    c1, c2, c3 = st.columns([2, 2, 3])
    window = c1.selectbox("Signal window", ["Latest candle", "Earlier signals", "All signals"], key=f"window_{timeframe}")
    direction = c2.selectbox("Direction", ["All", "LONG", "SHORT"], key=f"direction_{timeframe}")
    selected_symbols = c3.multiselect("Symbols", sorted(signals.symbol.unique()), key=f"symbols_{timeframe}")
    shown = signals.copy()
    if window == "Latest candle":
        shown = shown[shown.candle_datetime.eq(last)]
    elif window == "Earlier signals":
        shown = shown[shown.candle_datetime.lt(last)]
    if direction != "All":
        shown = shown[shown.direction.eq(direction)]
    if selected_symbols:
        shown = shown[shown.symbol.isin(selected_symbols)]
    columns = ["symbol", "direction", "grade", "signal_score", "entry_price", "stop_price",
               "qty", "entry_adx", "entry_rsi", "volume_qualified", "candle_datetime"]
    dataframe(shown.sort_values(["candle_datetime", "signal_score"], ascending=False)[columns])
    if shown.empty:
        st.info("No signals match these filters. Change the window or clear a filter.")
        return
    by_key = shown.set_index("signal_key")
    key = st.selectbox("Inspect a signal", shown.signal_key.tolist(),
                       format_func=lambda k: f"{by_key.loc[k, 'symbol']} · {by_key.loc[k, 'direction']} · {pd.Timestamp(by_key.loc[k, 'candle_datetime']).strftime('%d %b %H:%M')}",
                       key=f"inspect_{timeframe}")
    row = by_key.loc[key]
    left, right = st.columns([3, 1])
    with left:
        st.markdown(f"#### {row.symbol} · {row.direction}")
        candles(snapshot, row.symbol, float(row.entry_price), float(row.stop_price))
    with right:
        st.markdown("#### Signal context")
        st.metric("Historical matches", int(row.historical_matches))
        st.metric("Historical win rate", f"{row.historical_winrate:.1f}%")
        st.metric("Historical average R", f"{row.historical_avg_r:.2f}R")
        st.caption("Illustrative research statistics; these are generated fixtures, not a strategy backtest.")
        st.info("Read-only · trading controls unavailable")


def historical_view(snapshot: dict) -> None:
    st.subheader("Historical engine cache")
    st.caption("A synthetic ledger replaces the live SQLite cache for this deployment.")
    first, second, third = st.columns(3)
    tf = first.selectbox("Timeframe", ["All", "30M", "1H", "1D"])
    dataset = second.selectbox("Dataset", ["Trades", "Signals"])
    symbols = sorted({r["symbol"] for r in snapshot["trades"]})
    symbol = third.selectbox("Stock code", ["All"] + symbols)
    if dataset == "Trades":
        frame = table(snapshot, "trades")
    else:
        frame = pd.DataFrame([r for rows in snapshot["signals"].values() for r in rows])
    if tf != "All":
        frame = frame[frame.timeframe.eq(tf)]
    if symbol != "All":
        frame = frame[frame.symbol.eq(symbol)]
    c1, c2 = st.columns(2)
    c1.metric("Matching rows", len(frame))
    c2.metric("Dataset", "Synthetic / validated")
    dataframe(frame)
    st.download_button("Download filtered demo CSV", frame.to_csv(index=False),
                       file_name=f"demo_{dataset.lower()}.csv", mime="text/csv")


def portfolio_view(snapshot: dict, totals: dict) -> None:
    st.subheader("Portfolio intelligence")
    opened = positions_frame(snapshot)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Portfolio MTM", money(totals["open_mtm"]))
    c2.metric("Capital deployed", money(totals["capital_deployed"]))
    c3.metric("Defined entry risk", money(totals["portfolio_risk"]))
    c4.metric("Account equity", money(totals["total_equity"]))
    st.markdown("#### Open positions")
    dataframe(opened[["trade_id", "symbol", "direction", "entry_price", "current_price",
                      "stop_price", "qty", "mtm", "current_r", "risk_status"]])
    if opened.empty:
        st.info("No open positions in this snapshot.")
        return
    selected = st.selectbox("Inspect position", opened.symbol.tolist())
    row = opened[opened.symbol.eq(selected)].iloc[0]
    left, right = st.columns([3, 1])
    with left:
        candles(snapshot, selected, float(row.entry_price), float(row.stop_price))
    with right:
        st.markdown("#### Position context")
        st.metric("Current R", f"{row.current_r:.2f}R")
        st.metric("Risk state", row.risk_status)
        st.caption(f"Price as of {snapshot['generated_at']} · fixed demo snapshot")
        st.info("Monitoring only. Position changes are unavailable.")


def journal_view(snapshot: dict) -> None:
    st.subheader("Trading journal")
    trades = table(snapshot, "trades")
    c1, c2 = st.columns(2)
    tf = c1.selectbox("Journal timeframe", ["All", "30M", "1H", "1D"])
    outcome = c2.selectbox("Outcome", ["All", "Winners", "Losers"])
    if tf != "All":
        trades = trades[trades.timeframe.eq(tf)]
    if outcome == "Winners":
        trades = trades[trades.pnl > 0]
    elif outcome == "Losers":
        trades = trades[trades.pnl < 0]
    perf = performance(trades)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Closed trades", perf["count"])
    c2.metric("Realized P&L", money(perf["pnl"]))
    c3.metric("Win rate", f"{perf['win_rate']:.1f}%")
    c4.metric("Average R", f"{perf['average_r']:.2f}R")
    dataframe(trades.sort_values("exit_time", ascending=False))
    st.download_button("Download demo journal", trades.to_csv(index=False), "demo_journal.csv", "text/csv")


def analytics_view(snapshot: dict) -> None:
    st.subheader("Performance analytics")
    trades = table(snapshot, "trades")
    perf = performance(trades)
    equity = table(snapshot, "equity")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Realized P&L", money(perf["pnl"]))
    c2.metric("Win rate", f"{perf['win_rate']:.1f}%")
    c3.metric("Profit factor", f"{perf['profit_factor']:.2f}" if perf["profit_factor"] is not None else "—")
    c4.metric("Expectancy / trade", money(perf["expectancy"]))
    if equity.empty:
        st.info("No equity history in this snapshot.")
        return
    equity["drawdown_pct"] = (equity.total_equity / equity.total_equity.cummax() - 1) * 100
    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Account equity")
        draw(go.Figure(go.Scatter(x=equity.date, y=equity.total_equity, mode="lines",
                                 line=dict(color="#4adeb0", width=3), name="Equity")))
    with right:
        st.markdown("#### P&L by timeframe")
        grouped = trades.groupby("timeframe").pnl.sum().reindex(["30M", "1H", "1D"])
        draw(go.Figure(go.Bar(x=grouped.index, y=grouped.values,
                             marker_color=["#4adeb0" if n >= 0 else "#fb7185" for n in grouped.values])))
    st.markdown("#### Drawdown")
    draw(go.Figure(go.Scatter(x=equity.date, y=equity.drawdown_pct, fill="tozeroy",
                             line=dict(color="#fb7185"), name="Drawdown %")), 200)
    st.caption("All results come from fabricated trades. They do not represent actual or backtested returns.")


def review_view(snapshot: dict) -> None:
    st.subheader("Trade reviews")
    reviews = table(snapshot, "reviews")
    if reviews.empty:
        st.info("No reviews in this snapshot.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Reviewed trades", len(reviews))
    c2.metric("Review coverage", f"{len(reviews) / len(snapshot['trades']) * 100:.0f}%" if snapshot["trades"] else "—")
    c3.metric("Mistakes flagged", int(reviews.mistake_flag.eq("YES").sum()))
    grade = st.selectbox("Setup grade", ["All", "A", "B", "C"])
    shown = reviews if grade == "All" else reviews[reviews.setup_grade.eq(grade)]
    dataframe(shown[["trade_id", "symbol", "setup_grade", "execution_grade", "mistake_flag"]])
    if shown.empty:
        st.info("No reviews match this grade.")
        return
    selected = st.selectbox("Read review", shown.trade_id.tolist())
    row = shown[shown.trade_id.eq(selected)].iloc[0]
    with st.container(border=True):
        st.markdown(f"#### {row.symbol} · {row.trade_id}")
        st.write(row.notes)
        st.markdown("**Lesson learned**")
        st.write(row.lesson_learned)
    st.caption("Read-only review archive. Editing and saving are unavailable in this hosted viewer.")


def health_view(snapshot: dict) -> None:
    st.subheader("System health")
    st.info("DEMO MODE · fixture validation passed. This does not indicate a live engine or data feed is healthy.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Data source", "Synthetic")
    c2.metric("Access", "Read-only")
    c3.metric("Broker", "Not connected")
    c4.metric("Drive sync", "Not connected")
    checks = [
        {"Check": "Snapshot schema and required fields", "Status": "PASS"},
        {"Check": "Unique open and closed trade identifiers", "Status": "PASS"},
        {"Check": "Review-to-trade references", "Status": "PASS"},
        {"Check": "Positive position quantities and prices", "Status": "PASS"},
        {"Check": "Live source freshness", "Status": "NOT APPLICABLE — fixed fixture"},
        {"Check": "Broker and production credentials", "Status": "NOT REQUIRED"},
    ]
    dataframe(pd.DataFrame(checks))
    datasets = [{"Dataset": f"{tf} signals", "Rows": len(rows),
                 "Source timestamp": snapshot["generated_at"]} for tf, rows in snapshot["signals"].items()]
    datasets += [{"Dataset": k.title(), "Rows": len(snapshot[k]),
                  "Source timestamp": snapshot["generated_at"]} for k in ("positions", "trades", "equity", "reviews")]
    st.markdown("#### Data inventory")
    dataframe(pd.DataFrame(datasets))
    st.caption(f"Generation {snapshot['generation_id']} · schema {snapshot['schema_version']} · timezone Asia/Kolkata")


try:
    snapshot = load_snapshot()
    totals = summary(snapshot)
except (OSError, ValueError, KeyError, TypeError) as exc:
    st.error(f"Demo snapshot unavailable: {exc}")
    st.stop()

st.markdown('<div class="eyebrow">NSE / F&O · MONITORING WORKSPACE</div>', unsafe_allow_html=True)
left, right = st.columns([5, 1])
with left:
    st.title("Trading workstation")
    st.caption("Signals, positions and research — one read-only workspace.")
with right:
    st.write("")
    if st.button("↻ Reload demo", width="stretch"):
        st.rerun()
st.warning("DEMO DATA — Every signal, price, position and return is synthetic. No live market feed or broker is connected.", icon="🧪")
st.caption(f"Fixed example snapshot · {snapshot['generated_at']} · Asia/Kolkata · reload does not advance market data")

latest30 = pd.DataFrame(snapshot["signals"]["30M"])
signal_count = int(latest30.candle_datetime.eq(latest30.candle_datetime.max()).sum()) if not latest30.empty else 0
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("30M signals", signal_count)
c2.metric("Open positions", totals["open_positions"])
c3.metric("Closed trades", totals["closed_positions"])
c4.metric("Capital deployed", money(totals["capital_deployed"]))
c5.metric("Open MTM", money(totals["open_mtm"]))
c6.metric("Source status", "DEMO")
st.write("")
selected_view = st.radio("Dashboard section", NAV_OPTIONS, horizontal=True, key="dashboard_view",
                         label_visibility="collapsed")
st.divider()
if selected_view in NAV_OPTIONS[:3]:
    signal_view(snapshot, {"30M Scanner": "30M", "Hourly Signals": "1H", "Daily Signals": "1D"}[selected_view])
elif selected_view == "Historical Cache":
    historical_view(snapshot)
elif selected_view == "Portfolio":
    portfolio_view(snapshot, totals)
elif selected_view == "Journal":
    journal_view(snapshot)
elif selected_view == "Analytics":
    analytics_view(snapshot)
elif selected_view == "Trade Reviews":
    review_view(snapshot)
else:
    health_view(snapshot)
st.divider()
st.caption("DEMO / READ-ONLY · No orders, position updates, review edits or equity writes · Synthetic examples only")
