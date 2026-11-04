"""Assemble the transfer package for the friend's laptop: code, config example, launchers, README.

Copies an explicit list of files only, so no data, generated snapshot, config with real paths or
credential can be included. Refuses to write inside a Git working tree or over an existing package.

    python publisher/build_package.py --output-dir <folder outside Git>
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "NSE-Snapshot-Publisher"
FILES = {  # package path: repository path
    "README.txt": "publisher/README.txt",
    "publisher_config.example.ini": "publisher/publisher_config.example.ini",
    "Run NSE snapshot publisher.cmd": "publisher/Run NSE snapshot publisher.cmd",
    "Check installation.cmd": "publisher/Check installation.cmd",
    "producer/__init__.py": "producer/__init__.py",
    "producer/import_csv.py": "producer/import_csv.py",
    "producer/export_snapshot.py": "producer/export_snapshot.py",
    "producer/publish.py": "producer/publish.py",
    "producer/synthetic.py": "producer/synthetic.py",
}
FORBIDDEN_TEXT = ("PRIVATE KEY", "gserviceaccount.com", "client_secret", "refresh_token", "api_secret",
                  "session_token")
CRLF_SUFFIXES = {".cmd", ".txt", ".ini"}


class PackageFailed(Exception):
    pass


def _commit() -> str:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout

    try:
        commit = git("rev-parse", "--short", "HEAD").strip()
        dirty = git("status", "--porcelain", "--", *FILES.values()).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit} plus uncommitted changes" if dirty else commit


def build(output_dir: Path) -> Path:
    output_dir = Path(output_dir).resolve()
    if any((p / ".git").exists() for p in (output_dir, *output_dir.parents)):
        raise PackageFailed(f"Output folder is inside a Git working tree; choose another: {output_dir}")
    folder, archive = output_dir / PACKAGE, output_dir / f"{PACKAGE}.zip"
    if folder.exists() or archive.exists():
        raise PackageFailed(f"{folder} or {archive.name} already exists; move it away or choose another folder.")
    folder.mkdir(parents=True)
    contents = []
    try:
        for target, source in FILES.items():
            text = (ROOT / source).read_text(encoding="utf-8")
            found = [word for word in FORBIDDEN_TEXT if word in text]
            if found:
                raise PackageFailed(f"{source} contains {found}; refusing to package it.")
            if Path(target).suffix in CRLF_SUFFIXES:
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            destination = folder / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(text.encode("utf-8"))
            contents.append(f"{hashlib.sha256(destination.read_bytes()).hexdigest()}  {target}")
        (folder / "PACKAGE_CONTENTS.txt").write_bytes(
            ("NSE snapshot publisher package. No data or credentials are included.\r\n"
             f"Built from repository commit {_commit()}. SHA-256 of each file:\r\n"
             + "\r\n".join(contents) + "\r\n").encode("utf-8"))
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    zf.write(path, Path(PACKAGE) / path.relative_to(folder))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        archive.unlink(missing_ok=True)
        raise
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the laptop publisher transfer package.")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        archive = build(args.output_dir)
    except PackageFailed as exc:
        print(f"PACKAGE NOT BUILT\n{exc}", file=sys.stderr)
        return 1
    print(f"Package folder: {archive.with_suffix('')}\nZip to send: {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
