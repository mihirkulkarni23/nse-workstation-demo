"""Synthetic test publication: made-up records in the six application CSV layouts.

Used by the tests and for the first Google Drive trial. Nothing here comes from real application
data: the symbols (AAA, BBB) and January 2020 times are invented. The generation is labelled
`synthetic_test`, and the dashboard shows a SYNTHETIC TEST SNAPSHOT banner whenever it displays one.

    python -m producer.synthetic --output-dir <folder outside Git>
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from pathlib import Path

from producer.export_snapshot import ExportFailed, export_generation, format_export
from producer.import_csv import DATASETS, ImportFailed, run_import

SPEC = {ds.name: ds for ds in DATASETS}
RESEARCH = repr({f: {"factor": f, "bucket": "20-25", "trades": 10, "winners": 6, "losers": 4,
                     "win_rate": 60.0, "avg_r": 0.4, "median_r": 0.3, "avg_hold": 5.5}
                 for f in ("ADX", "RSI", "STOCH", "VOLUME", "DMI")})


def synthetic_rows() -> dict[str, list[dict]]:
    signal = {"grade": "A", "symbol": "AAA", "stock_code": "AAACOD", "entry_price": "100", "qty": "10",
              "rsi": "55.5", "adx": "21.0", "atr": "2.5", "entry_volume_ratio": "1.2", "day_return_pct": "0.5",
              "day_range_position": "0.6", "day_strength": "NEUTRAL"}
    return {
        "scanner_30m_signals": [
            {**signal, "direction": "LONG", "signal": "LONG", "stop_price": "95", "timeframe": "30M",
             "candle_datetime": "2020-01-06 10:15:00", "signal_time": "2020-01-06 10:15:00",
             "signal_key": "30M|AAACOD|LONG|2020-01-06T10:15:00", "research_available": "True",
             "research_context": RESEARCH, "signal_score": "7.5", "confidence_score": "7.0",
             "risk_per_share": "5", "risk_amount": "50"},
            {**signal, "symbol": "BBB", "stock_code": "BBBCOD", "direction": "SHORT", "signal": "SHORT",
             "stop_price": "105", "timeframe": "30M", "candle_datetime": "2020-01-06 10:45:00",
             "signal_time": "2020-01-06 10:45:00", "research_available": "True", "research_context": RESEARCH},
        ],
        "hourly_signal_scan": [
            {**signal, "signal": "LONG", "stop_price": "95", "candle_datetime": "2020-01-06 10:15:00",
             "k": "40", "d": "45", "di_plus": "25", "di_minus": "15"},
            {**signal, "signal": "LONG", "stop_price": "-4.5", "candle_datetime": "2020-01-06 11:15:00"},
            {**signal, "signal": "SHORT", "stop_price": "105", "candle_datetime": "2020-01-06 19:15:00"},
        ],
        "daily_signal_scan": [
            {**signal, "signal": "SHORT", "stop_price": "105", "candle_datetime": "2020-01-06 15:15:00",
             "direction": "SHORT", "timeframe": "1D", "signal_time": "2020-01-06 15:15:00",
             "signal_key": "1D|AAACOD|SHORT|2020-01-06T15:15:00"},
            {**signal, "signal": "LONG", "stop_price": "95", "candle_datetime": "2020-01-07 15:15:00"},
        ],
        "open_positions": [
            {"trade_id": "1", "symbol": "AAA", "stock_code": "AAACOD", "direction": "LONG",
             "entry_time": "2020-01-06 10:20:01.123456", "entry_price": "100", "qty": "10",
             "capital_used": "1000", "stop_price": "95", "risk_per_share": "5", "risk_amount": "50",
             "status": "OPEN"},
            {"trade_id": "2", "symbol": "BBB", "stock_code": "BBBCOD", "direction": "SHORT",
             "entry_time": "18:00.8", "entry_price": "200", "qty": "5", "capital_used": "1000",
             "stop_price": "210", "risk_per_share": "10", "risk_amount": "50", "status": "OPEN"},
        ],
        "closed_positions": [
            {"trade_id": "3", "symbol": "AAA", "stock_code": "AAACOD", "direction": "LONG",
             "entry_time": "15:01.2", "exit_time": "42:10.0", "entry_price": "100", "exit_price": "110",
             "qty": "10", "capital_used": "1000", "risk_amount": "50", "pnl": "100", "pnl_pct": "10",
             "r_multiple": "2", "exit_reason": "TARGET"},
            {"trade_id": "4", "symbol": "BBB", "stock_code": "BBBCOD", "direction": "SHORT",
             "entry_time": "11:00.0", "exit_time": "12:00.0", "entry_price": "200", "exit_price": "210",
             "qty": "5", "capital_used": "1000", "risk_amount": "50", "pnl": "-50", "pnl_pct": "-5",
             "r_multiple": "-1.5", "exit_reason": "STOPLOSS"},
        ],
        "daily_equity": [
            {"date": "2020-01-06", "realized_pnl": "0.0", "open_mtm": "25.5", "total_equity": "25.5",
             "open_positions": "2.0", "capital_used": "2000", "portfolio_risk": "100"},
            {"date": "2020-01-07", "realized_pnl": "50.0", "open_mtm": "-80.25", "total_equity": "-30.25",
             "open_positions": "2.0", "capital_used": "2000", "portfolio_risk": "100"},
        ],
    }


def write_fixture(folder: Path, rows: dict[str, list[dict]], drop_column: tuple[str, str] | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, records in rows.items():
        header = [c for c in SPEC[name].columns if (name, c) != drop_column]
        with (folder / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            for record in records:
                writer.writerow([record.get(c, "") for c in header])


def build_synthetic_publication(output_dir: Path) -> dict:
    """Import the synthetic CSVs as a `synthetic_test` generation, then export it for upload."""
    with tempfile.TemporaryDirectory() as tmp:
        write_fixture(Path(tmp), synthetic_rows())
        report = run_import(Path(tmp), output_dir, data_label="synthetic_test")
    return export_generation(output_dir, report["generation_id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and export a synthetic test snapshot for the Drive trial.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Folder outside Git for the test files.")
    args = parser.parse_args(argv)
    try:
        result = build_synthetic_publication(args.output_dir)
    except (ImportFailed, ExportFailed) as exc:
        print(f"SYNTHETIC EXPORT FAILED - nothing to upload.\n{exc}", file=sys.stderr)
        return 1
    print(format_export(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
