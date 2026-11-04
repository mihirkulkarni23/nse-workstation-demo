"""Laptop publisher: one manual run imports the six configured CSVs and exports a new publication.

Reads the trading application's CSV files without modifying them. Refuses files that are empty,
recently changed, still changing or end part-way through a line. Builds a new immutable generation
and exports nse_snapshot_<generation>.sqlite plus current_manifest.json for manual upload to the
private Drive folder. Any failure leaves earlier publications and LATEST_PUBLICATION.txt untouched.
Cross-file consistency is always reported as unverified: there is no upstream completion marker.

    python -m producer.publish --config publisher_config.ini
    python -m producer.publish --self-test
"""

from __future__ import annotations

import argparse
import configparser
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from producer.export_snapshot import MANIFEST_NAME, ExportFailed, export_generation, format_export
from producer.import_csv import (
    CROSS_FILE_REASON, DATASETS, ImportFailed, _inside_git_worktree, format_summary, run_import_files,
)

POINTER_NAME = "LATEST_PUBLICATION.txt"
LOCK_NAME = ".publisher.lock"


class PublishFailed(Exception):
    """Nothing new was published; the previous publication is unchanged."""


@dataclass
class Config:
    sources: dict[str, Path]
    output_dir: Path
    settle_seconds: int = 60
    recheck_seconds: int = 5


def _config_path(value: str) -> Path:
    # Windows "Copy as path" wraps paths in quotes; accept them.
    return Path(value.strip().strip('"').strip("'")).expanduser()


def load_config(path: Path) -> Config:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        found = parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise PublishFailed(f"{path} could not be read as a config file ({type(exc).__name__}).") from None
    if not found:
        raise PublishFailed(f"Config file not found: {path}")
    missing = [f"[sources] {ds.name}" for ds in DATASETS if not parser.get("sources", ds.name, fallback="").strip()]
    if not parser.get("output", "folder", fallback="").strip():
        missing.append("[output] folder")
    if missing:
        raise PublishFailed(f"{path.name} is missing: {', '.join(missing)}")
    try:
        settle = parser.getint("checks", "settle_seconds", fallback=60)
        recheck = parser.getint("checks", "recheck_seconds", fallback=5)
    except ValueError:
        raise PublishFailed("[checks] settle_seconds and recheck_seconds must be whole numbers.") from None
    if settle < 0 or recheck < 0:
        raise PublishFailed("[checks] values cannot be negative.")
    return Config({ds.name: _config_path(parser["sources"][ds.name]) for ds in DATASETS},
                  _config_path(parser["output"]["folder"]), settle, recheck)


def check_output_location(config: Config) -> None:
    output = config.output_dir.resolve()
    if _inside_git_worktree(output):
        raise PublishFailed(f"The output folder is inside a Git working tree; choose another folder: {output}")
    for path in config.sources.values():
        folder = path.resolve().parent
        if output == folder or folder in output.parents or output in folder.parents:
            raise PublishFailed(f"The output folder overlaps the folder holding {path.name}. Use a new folder that "
                                "is separate from the trading application's folders.")


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


def check_sources_ready(sources: dict[str, Path], settle_seconds: int, recheck_seconds: int,
                        clock=time.time, sleep=time.sleep) -> None:
    """Refuse partial or changing files: empty, modified within settle_seconds, cut off mid-line, or
    changed between two looks recheck_seconds apart. The importer also re-hashes every file afterwards."""
    problems, before = [], {}
    for name, path in sources.items():
        if not path.is_file():
            problems.append(f"{name}: file not found: {path}")
            continue
        stat = path.stat()
        age = clock() - stat.st_mtime
        if stat.st_size == 0:
            problems.append(f"{path.name} is empty (a partial copy, or still being written).")
        elif age < settle_seconds:
            problems.append(f"{path.name} changed {max(0, int(age))} seconds ago and may still be being written.")
        elif not _ends_with_newline(path):
            problems.append(f"{path.name} ends part-way through a line (a partial write or copy).")
        before[name] = (stat.st_size, stat.st_mtime_ns)
    if problems:
        raise PublishFailed("Source files are not ready:\n  - " + "\n  - ".join(problems) + f"\nWait until the "
                            f"trading application has finished writing (at least {settle_seconds} seconds), then "
                            "run again.")
    sleep(recheck_seconds)
    changed = [sources[name].name for name, signature in before.items() if _signature(sources[name]) != signature]
    if changed:
        raise PublishFailed(f"Source files changed while being checked: {', '.join(changed)}. Wait until the "
                            "trading application has finished writing, then run again.")


def _write_pointer(output_dir: Path, report: dict, export: dict) -> None:
    files = export["files"]
    text = "\n".join([
        "Latest valid publication (only updated when a run succeeds)", "",
        f"Generation: {report['generation_id']}", f"Imported (UTC): {report['created_at_utc']}",
        f"Folder: {export['folder']}", "",
        "Upload these files to the private Google Drive folder, in this order:",
        f"  1. {files[0].name}   (wait for 'Upload complete')",
        f"  2. {files[1].name}   (choose 'Replace existing file')", "",
        f"Cross-file consistency: UNVERIFIED. {CROSS_FILE_REASON}", ""])
    temporary = output_dir / f"{POINTER_NAME}.tmp"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, output_dir / POINTER_NAME)


def publish(config: Config, data_label: str = "supplied_csv", clock=time.time, sleep=time.sleep) -> dict:
    check_output_location(config)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    lock = config.output_dir / LOCK_NAME
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        raise PublishFailed(f"Another publisher run seems to be in progress ({lock}). If no other publisher "
                            "window is open, delete that file and run again.") from None
    try:
        check_sources_ready(config.sources, config.settle_seconds, config.recheck_seconds, clock, sleep)
        report = run_import_files(config.sources, config.output_dir, data_label)
        export = export_generation(config.output_dir, report["generation_id"])
        _write_pointer(config.output_dir, report, export)
    except (ImportFailed, ExportFailed) as exc:
        raise PublishFailed(str(exc)) from None
    finally:
        lock.unlink(missing_ok=True)
    return {"report": report, "export": export}


def format_result(result: dict) -> str:
    return "\n".join([
        format_summary(result["report"]), format_export(result["export"]),
        f"Cross-file consistency: UNVERIFIED. {CROSS_FILE_REASON}",
        f"Details of this run: {POINTER_NAME} in the output folder.", "", "PUBLISH COMPLETE", ""])


def self_test() -> str:
    """Run the whole pipeline on made-up records in a temporary folder, then delete it."""
    from producer.synthetic import synthetic_rows, write_fixture

    with tempfile.TemporaryDirectory(prefix="nse-publisher-check-") as tmp:
        source = Path(tmp) / "source"
        write_fixture(source, synthetic_rows())
        settled = time.time() - 3600
        for path in source.iterdir():
            os.utime(path, (settled, settled))
        config = Config({ds.name: source / ds.file_name for ds in DATASETS}, Path(tmp) / "output", 60, 1)
        result = publish(config, data_label="synthetic_test")
        names = sorted(p.name for p in result["export"]["folder"].iterdir())
        if MANIFEST_NAME not in names or not (Path(tmp) / "output" / POINTER_NAME).is_file():
            raise PublishFailed("The installation check did not produce the expected files.")
        rows = sum(d["stored_rows"] for d in result["export"]["manifest"]["datasets"].values())
    return (f"INSTALLATION CHECK PASSED. Python {sys.version.split()[0]} imported and exported {rows} made-up "
            "records in a temporary folder, which has been deleted. Your own files were not read.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the six configured CSVs and export a new publication.")
    parser.add_argument("--config", type=Path, default=Path("publisher_config.ini"))
    parser.add_argument("--self-test", action="store_true", help="Check the installation with made-up records.")
    parser.add_argument("--open-folder", action="store_true", help="Open the new publication folder afterwards.")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            print(self_test())
            return 0
        result = publish(load_config(args.config))
    except (PublishFailed, OSError) as exc:
        print(f"PUBLISH FAILED - nothing new to upload. Earlier publications are unchanged.\n{exc}", file=sys.stderr)
        return 1
    print(format_result(result))
    if args.open_folder and hasattr(os, "startfile"):
        os.startfile(result["export"]["folder"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
