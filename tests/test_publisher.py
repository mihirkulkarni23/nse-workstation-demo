"""Laptop publisher and transfer-package tests. Every source file here is synthetic."""

import configparser
import hashlib
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path
import unittest

from producer.import_csv import DATASETS
from producer.publish import (
    LOCK_NAME, POINTER_NAME, Config, PublishFailed, check_sources_ready, load_config, publish, self_test,
)
from producer.synthetic import synthetic_rows, write_fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "publisher"))
import build_package  # noqa: E402


def hashes(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob("*.csv"))}


class PublisherTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "trading-app" / "data"
        write_fixture(self.source, synthetic_rows())
        self.settle()
        self.config = Config({ds.name: self.source / ds.file_name for ds in DATASETS}, self.tmp / "publisher-output",
                             60, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def settle(self, age: float = 3600) -> None:
        when = time.time() - age
        for path in self.source.glob("*.csv"):
            os.utime(path, (when, when))

    def run_publish(self) -> dict:
        return publish(self.config, data_label="synthetic_test", sleep=lambda _: None)


class ConfigTests(PublisherTestCase):
    def write_config(self, drop: tuple[str, str] | None = None, checks: str | None = None) -> Path:
        parser = configparser.ConfigParser(interpolation=None)
        parser["sources"] = {ds.name: f'"{self.source / ds.file_name}"' for ds in DATASETS}
        parser["output"] = {"folder": str(self.tmp / "publisher-output")}
        if drop:
            parser.remove_option(*drop)
        if checks is not None:
            parser["checks"] = {"settle_seconds": checks}
        path = self.tmp / "publisher_config.ini"
        with path.open("w", encoding="utf-8") as fh:
            parser.write(fh)
        return path

    def test_example_config_lists_the_six_sources_and_an_output_folder(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(ROOT / "publisher" / "publisher_config.example.ini", encoding="utf-8")
        self.assertEqual(set(parser["sources"]), {ds.name for ds in DATASETS})
        self.assertTrue(parser["output"]["folder"])
        config = load_config(ROOT / "publisher" / "publisher_config.example.ini")
        self.assertTrue(all("PATH\\TO" in str(p) for p in config.sources.values()))

    def test_quoted_paths_are_accepted_and_missing_keys_named(self):
        config = load_config(self.write_config())
        self.assertEqual(config.sources["daily_equity"], self.source / "daily_equity.csv")
        self.assertEqual((config.settle_seconds, config.recheck_seconds), (60, 5))
        with self.assertRaisesRegex(PublishFailed, r"\[sources\] open_positions"):
            load_config(self.write_config(drop=("sources", "open_positions")))
        with self.assertRaisesRegex(PublishFailed, "whole numbers"):
            load_config(self.write_config(checks="soon"))
        with self.assertRaisesRegex(PublishFailed, "not found"):
            load_config(self.tmp / "absent.ini")


class ReadinessTests(PublisherTestCase):
    def ready(self, **kwargs):
        check_sources_ready(self.config.sources, 60, 0, sleep=kwargs.get("sleep", lambda _: None))

    def test_settled_complete_files_are_accepted(self):
        self.ready()

    def test_recent_empty_partial_and_missing_files_are_refused(self):
        self.settle(age=5)
        with self.assertRaisesRegex(PublishFailed, "may still be being written"):
            self.ready()
        self.settle()
        (self.source / "open_positions.csv").write_bytes(b"")
        self.settle()
        with self.assertRaisesRegex(PublishFailed, "open_positions.csv is empty"):
            self.ready()
        write_fixture(self.source, synthetic_rows())
        path = self.source / "daily_equity.csv"
        path.write_bytes(path.read_bytes().rstrip(b"\r\n")[:-3])
        self.settle()
        with self.assertRaisesRegex(PublishFailed, "daily_equity.csv ends part-way through a line"):
            self.ready()
        path.unlink()
        with self.assertRaisesRegex(PublishFailed, "file not found"):
            self.ready()

    def test_file_changing_between_looks_is_refused(self):
        def trading_app_writes(_):
            with (self.source / "hourly_signal_scan.csv").open("ab") as fh:
                fh.write(b"\n")
        with self.assertRaisesRegex(PublishFailed, "changed while being checked: hourly_signal_scan.csv"):
            self.ready(sleep=trading_app_writes)


class PublishTests(PublisherTestCase):
    def test_publish_creates_snapshot_manifest_and_pointer_without_touching_sources(self):
        before = hashes(self.source)
        result = self.run_publish()
        self.assertEqual(hashes(self.source), before)
        folder = result["export"]["folder"]
        generation = result["report"]["generation_id"]
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["current_manifest.json", f"nse_snapshot_{generation}.sqlite"])
        manifest = json.loads((folder / "current_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["cross_file_consistency"]["status"], "unverified")
        self.assertIn("completion marker", manifest["cross_file_consistency"]["reason"])
        pointer = (self.config.output_dir / POINTER_NAME).read_text(encoding="utf-8")
        self.assertIn(generation, pointer)
        self.assertIn("UNVERIFIED", pointer)
        self.assertFalse((self.config.output_dir / LOCK_NAME).exists())
        self.assertEqual(sorted(p.name for p in self.source.iterdir()), sorted(before))

    def test_failure_retains_previous_publication(self):
        first = self.run_publish()
        pointer = (self.config.output_dir / POINTER_NAME).read_bytes()
        published = {p.name: p.read_bytes() for p in first["export"]["folder"].iterdir()}
        write_fixture(self.source, synthetic_rows(), drop_column=("closed_positions", "pnl"))
        self.settle()
        with self.assertRaisesRegex(PublishFailed, "missing columns"):
            self.run_publish()
        self.settle(age=1)
        with self.assertRaisesRegex(PublishFailed, "may still be being written"):
            self.run_publish()
        self.assertEqual((self.config.output_dir / POINTER_NAME).read_bytes(), pointer)
        self.assertEqual({p.name: p.read_bytes() for p in first["export"]["folder"].iterdir()}, published)
        self.assertEqual([p.name for p in (self.config.output_dir / "publish").iterdir()],
                         [first["report"]["generation_id"]])
        self.assertFalse(list(self.config.output_dir.rglob(".incomplete-*")))
        self.assertFalse((self.config.output_dir / LOCK_NAME).exists())

    def test_output_folder_must_be_separate_from_trading_app(self):
        for output in (self.source, self.source / "published", self.tmp / "trading-app", self.tmp):
            self.config.output_dir = output
            with self.assertRaisesRegex(PublishFailed, "overlaps"):
                self.run_publish()
        self.config.output_dir = ROOT / "should-not-exist"
        with self.assertRaisesRegex(PublishFailed, "Git working tree"):
            self.run_publish()
        self.assertFalse((ROOT / "should-not-exist").exists())

    def test_concurrent_run_is_refused(self):
        self.config.output_dir.mkdir(parents=True)
        (self.config.output_dir / LOCK_NAME).write_text("")
        with self.assertRaisesRegex(PublishFailed, "in progress"):
            self.run_publish()
        self.assertFalse((self.config.output_dir / "publish").exists())

    def test_self_test_passes_and_leaves_nothing(self):
        leftovers = set(Path(tempfile.gettempdir()).glob("nse-publisher-check-*"))
        self.assertIn("INSTALLATION CHECK PASSED", self_test())
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("nse-publisher-check-*")), leftovers)


class PackageTests(unittest.TestCase):
    def test_package_contains_only_code_docs_and_launchers(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = build_package.build(Path(tmp))
            with zipfile.ZipFile(archive) as zf:
                names = sorted(n.split("/", 1)[1] for n in zf.namelist())
            self.assertEqual(names, sorted([*build_package.FILES, "PACKAGE_CONTENTS.txt"]))
            self.assertFalse([n for n in names if Path(n).suffix in {".csv", ".sqlite", ".db", ".json", ".toml",
                                                                    ".zip", ".log"}])
            self.assertNotIn("publisher_config.ini", names)
            folder = Path(tmp) / build_package.PACKAGE
            for path in folder.rglob("*"):
                if path.is_file():
                    text = path.read_text(encoding="utf-8")
                    for word in build_package.FORBIDDEN_TEXT:
                        self.assertNotIn(word, text, path.name)
            launcher = (folder / "Run NSE snapshot publisher.cmd").read_bytes()
            self.assertIn(b"\r\n", launcher)
            self.assertIn(b"python -m producer.publish --config publisher_config.ini", launcher)
            with self.assertRaisesRegex(build_package.PackageFailed, "already exists"):
                build_package.build(Path(tmp))
        with self.assertRaisesRegex(build_package.PackageFailed, "Git working tree"):
            build_package.build(ROOT / "dist")


if __name__ == "__main__":
    unittest.main()
