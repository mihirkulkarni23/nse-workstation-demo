"""Importer tests. Every fixture here is synthetic; no real application data is used."""

import ast
import hashlib
import json
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path
import unittest
from unittest import mock

from producer import import_csv
from producer.import_csv import ImportFailed, parse_research_context, parse_timestamp, run_import
from producer.synthetic import RESEARCH, write_fixture
from producer.synthetic import synthetic_rows as clean_rows

ROOT = Path(__file__).resolve().parents[1]


def synthetic_rows(marker: Path) -> dict[str, list[dict]]:
    """The shared synthetic fixture, with a hostile research payload that must never execute."""
    rows = clean_rows()
    rows["scanner_30m_signals"][1]["research_context"] = f"__import__('pathlib').Path(r'{marker}').write_text('x')"
    return rows


def source_hashes(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob("*.csv"))}


class ImporterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source, self.output = self.tmp / "source", self.tmp / "private"
        self.marker = self.tmp / "eval_was_executed.txt"
        write_fixture(self.source, synthetic_rows(self.marker))

    def tearDown(self):
        self._tmp.cleanup()

    def query(self, report: dict, sql: str, params=()) -> list[tuple]:
        uri = Path(report["snapshot_path"]).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def generations(self) -> list[Path]:
        snapshots = self.output / "snapshots"
        return sorted(snapshots.iterdir()) if snapshots.exists() else []


class SuccessfulImportTests(ImporterTestCase):
    def setUp(self):
        super().setUp()
        self.before = source_hashes(self.source)
        self.report = run_import(self.source, self.output)

    def flags(self, table: str, dataset: str, row: int) -> list[str]:
        return json.loads(self.query(self.report, f"SELECT quality_flags_json FROM {table} "
                                     "WHERE dataset = ? AND source_row = ?", (dataset, row))[0][0])

    def test_counts_reconcile_and_sources_are_unchanged(self):
        expected = {"scanner_30m_signals": 2, "hourly_signal_scan": 3, "daily_signal_scan": 2,
                    "open_positions": 2, "closed_positions": 2, "daily_equity": 2}
        for name, count in expected.items():
            d = self.report["datasets"][name]
            self.assertEqual((d["source_rows"], d["stored_rows"], d["quarantined_rows"]), (count, count, 0))
            self.assertTrue(d["reconciled"] and d["source_unchanged"])
            self.assertEqual(d["sha256_before"], self.before[f"{name}.csv"])
        self.assertEqual(source_hashes(self.source), self.before)
        stored = dict(self.query(self.report, "SELECT file_name, sha256 FROM source_files"))
        self.assertEqual(stored, self.before)

    def test_generation_is_versioned_and_complete(self):
        [generation] = self.generations()
        self.assertEqual(generation.name, self.report["generation_id"])
        self.assertTrue((generation / "validation_report.json").is_file())
        self.assertTrue((generation / "validation_report.txt").is_file())
        meta = {k: json.loads(v) for k, v in self.query(self.report, "SELECT key, value_json FROM publication_meta")}
        self.assertEqual(meta["schema_version"], 1)
        self.assertEqual(meta["mode"], "uploaded_snapshot")
        self.assertEqual(meta["record_origin"], "application_recorded")
        self.assertEqual(meta["timezone_policy"]["status"], "unverified")
        self.assertIsNone(meta["strategy_processed_through"])
        self.assertEqual(self.query(self.report, "PRAGMA user_version"), [(1,)])
        for name in ("quotes", "candles", "broker_book", "original_strategy", "backtests", "reviews"):
            self.assertEqual(meta["capabilities"][name]["status"], "unavailable", name)
        self.assertEqual(meta["time_frontiers"]["hourly_signal_scan"]["latest_in_file"], "2020-01-06T19:15:00")

    def test_rows_keep_provenance_and_raw_strings(self):
        rows = self.query(self.report, "SELECT dataset, source_row, record_origin, raw_record_json FROM app_signals "
                                       "ORDER BY dataset, source_row")
        self.assertEqual(len(rows), 7)
        for dataset, row, origin, raw in rows:
            self.assertEqual(origin, "application_recorded")
            self.assertEqual(json.loads(raw)["stock_code"],
                             synthetic_rows(self.marker)[dataset][row - 1]["stock_code"])

    def test_time_fragments_stay_null_and_no_date_is_invented(self):
        rows = self.query(self.report, "SELECT trade_id, entry_time_raw, entry_time, entry_time_status "
                                       "FROM app_open_positions ORDER BY source_row")
        self.assertEqual(rows[0][2:], ("2020-01-06T10:20:01.123456", "parsed_naive"))
        self.assertEqual(rows[1][1:], ("18:00.8", None, "incomplete_fragment"))
        closed = self.query(self.report, "SELECT entry_time_raw, entry_time, exit_time, entry_time_status, "
                                         "exit_time_status FROM app_closed_trades")
        for entry_raw, entry, exit_, entry_status, exit_status in closed:
            self.assertTrue(entry_raw)
            self.assertEqual((entry, exit_, entry_status, exit_status),
                             (None, None, "incomplete_fragment", "incomplete_fragment"))
        today = date.today().isoformat()
        for (value,) in self.query(self.report, "SELECT candle_time FROM app_signals UNION ALL SELECT signal_time "
                                                "FROM app_signals UNION ALL SELECT entry_time FROM app_open_positions"):
            self.assertFalse(value and value.startswith(today))
        self.assertIn("timestamp_incomplete_fragment:entry_time", self.flags("app_open_positions", "open_positions", 2))

    def test_30m_aliases_normalized_with_provenance_and_absent_fields_stay_null(self):
        rsi, adx, atr, di_plus, stoch_k, provenance = self.query(
            self.report, "SELECT rsi, adx, atr, di_plus, stoch_k, field_provenance_json FROM app_signals "
                         "WHERE dataset = 'scanner_30m_signals' AND source_row = 1")[0]
        self.assertEqual((rsi, adx, atr, di_plus, stoch_k), (55.5, 21.0, 2.5, None, None))
        provenance = json.loads(provenance)
        self.assertEqual((provenance["rsi"], provenance["adx"], provenance["atr"]), ("rsi", "adx", "atr"))
        self.assertIsNone(provenance["di_plus"])
        self.assertIn("indicator_alias_used:rsi", self.flags("app_signals", "scanner_30m_signals", 1))
        blank = {r[0] for r in self.query(self.report, "SELECT field FROM data_quality WHERE dataset = "
                                                       "'scanner_30m_signals' AND check_code = 'column_entirely_blank'")}
        self.assertTrue({"entry_rsi", "di_plus_14", "stoch_k", "historical_matches", "scan_date"} <= blank)
        self.assertEqual(self.query(self.report, "SELECT stoch_k, field_provenance_json FROM app_signals WHERE "
                                                 "dataset = 'hourly_signal_scan' AND source_row = 1")[0][0], 40.0)

    def test_daily_blanks_are_derived_and_labelled(self):
        rows = self.query(self.report, "SELECT direction, direction_basis, timeframe, timeframe_basis, "
                                       "signal_time, signal_time_basis, candle_time FROM app_signals "
                                       "WHERE dataset = 'daily_signal_scan' ORDER BY source_row")
        self.assertEqual(rows[0][:6], ("SHORT", "direction_column", "1D", "recorded", "2020-01-06T15:15:00",
                                       "recorded"))
        self.assertEqual(rows[1], ("LONG", "signal_column", "1D", "dataset_identity", "2020-01-07T15:15:00",
                                   "derived_from_candle_time", "2020-01-07T15:15:00"))
        hourly = self.query(self.report, "SELECT DISTINCT signal_time, signal_time_basis FROM app_signals "
                                         "WHERE dataset = 'hourly_signal_scan'")
        self.assertEqual(hourly, [(None, "not_supplied")])

    def test_negative_stop_is_preserved_and_flagged_not_repaired(self):
        stop, status = self.query(self.report, "SELECT stop_price, stop_status FROM app_signals "
                                               "WHERE dataset = 'hourly_signal_scan' AND source_row = 2")[0]
        self.assertEqual((stop, status), (-4.5, "invalid_non_positive"))
        self.assertEqual(self.query(self.report, "SELECT affected_rows FROM data_quality WHERE check_code = "
                                                 "'stop_non_positive'"), [(1,)])
        outside = self.query(self.report, "SELECT affected_rows, sample_source_rows_json FROM data_quality "
                                          "WHERE check_code = 'hourly_outside_original_loader_window'")
        self.assertEqual(outside, [(1, "[3]")])

    def test_research_is_parsed_safely_and_hostile_payload_is_never_executed(self):
        rows = self.query(self.report, "SELECT research_context_status, research_context_json FROM app_signals "
                                       "WHERE dataset = 'scanner_30m_signals' ORDER BY source_row")
        self.assertEqual(rows[0][0], "parsed_factor_buckets")
        self.assertEqual(set(json.loads(rows[0][1])), {"ADX", "RSI", "STOCH", "VOLUME", "DMI"})
        self.assertEqual(rows[1], ("invalid", None))
        self.assertFalse(self.marker.exists())
        factors = self.query(self.report, "SELECT DISTINCT research_kind FROM factor_research")
        self.assertEqual(factors, [("factor_bucket_summary",)])
        self.assertEqual(self.query(self.report, "SELECT COUNT(*) FROM factor_research"), [(5,)])

    def test_closed_trade_comparisons_and_recorded_pnl_semantics(self):
        rows = self.query(self.report, "SELECT pnl_recorded, pnl_calculated, r_multiple_recorded, "
                                       "r_multiple_calculated, book_id FROM app_closed_trades ORDER BY source_row")
        self.assertEqual(rows[0], (100.0, 100.0, 2.0, 2.0, "application_recorded"))
        self.assertEqual(rows[1][:4], (-50.0, -50.0, -1.5, -1.0))
        self.assertIn("r_multiple_recalc_mismatch", self.flags("app_closed_trades", "closed_positions", 2))
        self.assertNotIn("pnl_recalc_mismatch", self.flags("app_closed_trades", "closed_positions", 2))
        equity = self.query(self.report, "SELECT recorded_cumulative_pnl, identity_holds FROM recorded_pnl_history "
                                         "ORDER BY source_row")
        self.assertEqual(equity, [(25.5, 1), (-30.25, 1)])
        columns = {r[1] for r in self.query(self.report, "PRAGMA table_info(recorded_pnl_history)")}
        self.assertNotIn("total_equity", columns)

    def test_blank_values_are_null_not_zero(self):
        rows = self.query(self.report, "SELECT signal_score, confidence_score, risk_amount FROM app_signals "
                                       "WHERE dataset = 'hourly_signal_scan'")
        self.assertTrue(all(v is None for row in rows for v in row))
        self.assertEqual(self.query(self.report, "SELECT entry_dmi_spread, mapping_status FROM app_open_positions "
                                                 "JOIN instruments USING (stock_code) WHERE trade_id = '1'"),
                         [(None, "observed_one_to_one")])


class FailClosedTests(ImporterTestCase):
    def test_missing_column_fails_without_creating_a_generation(self):
        write_fixture(self.source, synthetic_rows(self.marker), drop_column=("open_positions", "entry_time"))
        with self.assertRaisesRegex(ImportFailed, "open_positions.csv: missing columns"):
            run_import(self.source, self.output)
        self.assertEqual(self.generations(), [])

    def test_missing_file_fails(self):
        (self.source / "daily_equity.csv").unlink()
        with self.assertRaisesRegex(ImportFailed, "daily_equity.csv"):
            run_import(self.source, self.output)

    def test_ragged_row_is_quarantined_and_counts_still_reconcile(self):
        with (self.source / "closed_positions.csv").open("a", newline="", encoding="utf-8") as fh:
            fh.write("99,ZZZ,ZZZCOD\r\n")
        report = run_import(self.source, self.output)
        d = report["datasets"]["closed_positions"]
        self.assertEqual((d["source_rows"], d["stored_rows"], d["quarantined_rows"], d["reconciled"]),
                         (3, 2, 1, True))
        self.assertEqual(self.query(report, "SELECT source_row, reason FROM quarantined_rows"), [(3, "ragged_row")])

    def test_output_inside_git_worktree_is_refused(self):
        target = ROOT / "should_not_be_created"
        with self.assertRaisesRegex(ImportFailed, "inside a Git working tree"):
            run_import(self.source, target)
        self.assertFalse(target.exists())

    def test_source_change_during_import_discards_generation(self):
        with mock.patch.object(import_csv, "_sha256_file", return_value="0" * 64):
            with self.assertRaisesRegex(ImportFailed, "Source changed while importing"):
                run_import(self.source, self.output)
        self.assertEqual(self.generations(), [])


class ParserTests(unittest.TestCase):
    def test_timestamps_are_strict(self):
        self.assertEqual(parse_timestamp("2020-01-06 10:15:00"), ("2020-01-06T10:15:00", "parsed_naive"))
        self.assertEqual(parse_timestamp("18:00.8"), (None, "incomplete_fragment"))
        self.assertEqual(parse_timestamp("10:15"), (None, "incomplete_fragment"))
        self.assertEqual(parse_timestamp("06/01/2020"), (None, "unparseable"))
        self.assertEqual(parse_timestamp("  "), (None, "missing"))

    def test_research_parser_rejects_unsafe_or_malformed_payloads(self):
        self.assertIsNotNone(parse_research_context(RESEARCH))
        for payload in ("__import__('os').getcwd()", "{'ADX': {'factor': 'ADX'}}", "[1, 2]", "{}",
                        RESEARCH.replace("10", "-10", 1), RESEARCH.replace("60.0", "float('nan')", 1),
                        "{" * 5000, "{'A': " + "1" * (import_csv.MAX_RESEARCH_CHARS + 1) + "}"):
            self.assertIsNone(parse_research_context(payload), payload[:40])


class ImportGraphTests(unittest.TestCase):
    def test_hosted_app_does_not_import_producer(self):
        for filename in ("Ldashboard_V9.py", "dashboard_data.py", "snapshot_reader.py", "snapshot_views.py",
                         "drive_reader.py"):
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            self.assertFalse([m for m in modules if m.split(".")[0] == "producer"], filename)

    def test_producer_does_not_import_original_trading_code(self):
        for path in (ROOT / "producer").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            modules = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            self.assertFalse({m for m in modules if m.startswith("L") or m in {"signal_engine", "strategy_config"}},
                             path.name)
            # Standard library only: the producer must not share the cloud viewer's dependencies.
            third_party = {m.split(".")[0] for m in modules} - set(sys.stdlib_module_names) - {"producer", "__future__"}
            self.assertFalse(third_party, path.name)


if __name__ == "__main__":
    unittest.main()
