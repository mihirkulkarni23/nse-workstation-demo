"""Safe local export of one snapshot generation for manual upload to a private Google Drive folder.

Copies an imported generation with SQLite's backup API into publish/<generation_id>/, checks the
copy (integrity, schema, row reconciliation), and writes current_manifest.json describing it: schema,
generation, file size and SHA-256, row counts, source times and quality totals. The source generation
is opened read-only and never modified. Nothing is uploaded; no record values go into the manifest.

    python -m producer.export_snapshot --private-dir <private folder> [--generation <generation_id>]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from producer.import_csv import (
    BUILDERS, CROSS_FILE_REASON, DATASETS, SCHEMA_VERSION, SNAPSHOT_NAME, _inside_git_worktree, _sha256_file,
)

MANIFEST_NAME = "current_manifest.json"
MANIFEST_KIND = "nse_dashboard_snapshot_manifest"
MANIFEST_SCHEMA = 1
GENERATION_NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
DATASET_TABLES = {ds.name: BUILDERS[ds.kind][0] for ds in DATASETS}


class ExportFailed(Exception):
    """The export was abandoned; no publish folder for this generation was left behind."""


def snapshot_file_name(generation_id: str) -> str:
    return f"nse_snapshot_{generation_id}.sqlite"


def newest_generation(private_dir: Path) -> str:
    snapshots = Path(private_dir) / "snapshots"
    found = sorted(d.name for d in snapshots.iterdir() if d.is_dir() and GENERATION_NAME.match(d.name)
                   and (d / SNAPSHOT_NAME).is_file()) if snapshots.is_dir() else []
    if not found:
        raise ExportFailed(f"No completed snapshot generation in {snapshots}")
    return found[-1]


def export_generation(private_dir: Path, generation_id: str | None = None) -> dict:
    """Export one generation to publish/<generation_id>/. Returns the manifest plus local paths."""
    private_dir = Path(private_dir).resolve()
    if _inside_git_worktree(private_dir):
        raise ExportFailed(f"Private folder is inside a Git working tree; refusing: {private_dir}")
    generation_id = generation_id or newest_generation(private_dir)
    if not GENERATION_NAME.match(generation_id):
        raise ExportFailed(f"Not a generation id: {generation_id!r}")
    source = private_dir / "snapshots" / generation_id / SNAPSHOT_NAME
    if not source.is_file():
        raise ExportFailed(f"Generation not found: {source}")

    publish = private_dir / "publish"
    final_dir, work_dir = publish / generation_id, publish / f".incomplete-{generation_id}"
    if final_dir.exists() or work_dir.exists():
        raise ExportFailed(f"Generation {generation_id} was already exported to {final_dir}. Exports are "
                           "immutable; upload those files or import a new generation.")
    work_dir.mkdir(parents=True)
    try:
        target = work_dir / snapshot_file_name(generation_id)
        src = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        manifest = describe_snapshot(target, generation_id)
        (work_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if describe_snapshot(target, generation_id)["snapshot_file"] != manifest["snapshot_file"]:
            raise ExportFailed("Exported file changed while writing the manifest.")
        work_dir.rename(final_dir)
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return {"manifest": manifest, "folder": final_dir,
            "files": [final_dir / snapshot_file_name(generation_id), final_dir / MANIFEST_NAME]}


def describe_snapshot(path: Path, generation_id: str) -> dict:
    """Validate an exported snapshot file and build its manifest. Counts and metadata only."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ExportFailed("SQLite integrity check failed on the exported copy.")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ExportFailed("Exported copy has an unexpected schema version.")
        meta = {k: json.loads(v) for k, v in conn.execute("SELECT key, value_json FROM publication_meta")}
        if (meta.get("schema_version"), meta.get("mode"), meta.get("generation_id")) != (
                SCHEMA_VERSION, "uploaded_snapshot", generation_id):
            raise ExportFailed("Snapshot metadata does not match the requested generation.")
        datasets, problems = {}, []
        for name, file_name, data_rows, stored, held, sha_before, sha_after, modified in conn.execute(
                "SELECT dataset, file_name, data_rows, stored_rows, quarantined_rows, sha256, "
                "sha256_after_import, modified_utc FROM source_files ORDER BY dataset"):
            present = conn.execute(f"SELECT COUNT(*) FROM {DATASET_TABLES[name]} WHERE dataset = ?",
                                   (name,)).fetchone()[0]
            if stored + held != data_rows or present != stored or sha_before != sha_after:
                problems.append(name)
            datasets[name] = {"file_name": file_name, "source_rows": data_rows, "stored_rows": stored,
                              "quarantined_rows": held, "source_sha256": sha_before,
                              "source_modified_utc": modified,
                              "latest_in_file": meta["time_frontiers"].get(name, {}).get("latest_in_file")}
        if problems or set(datasets) != set(DATASET_TABLES):
            raise ExportFailed(f"Row reconciliation failed for: {', '.join(problems) or 'missing datasets'}")
        quality = {severity: {"checks": checks, "affected_rows": rows} for severity, checks, rows in conn.execute(
            "SELECT severity, COUNT(*), SUM(affected_rows) FROM data_quality GROUP BY severity ORDER BY severity")}
        factor_rows = conn.execute("SELECT COUNT(*) FROM factor_research").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise ExportFailed(f"Exported copy is not a readable snapshot ({exc}).") from exc
    finally:
        conn.close()
    return {
        "kind": MANIFEST_KIND, "manifest_schema": MANIFEST_SCHEMA, "snapshot_schema": SCHEMA_VERSION,
        "mode": "uploaded_snapshot", "generation_id": generation_id,
        "data_label": meta.get("data_label", "supplied_csv"),
        "generation_created_at_utc": meta["created_at_utc"],
        "exported_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "snapshot_file": {"name": snapshot_file_name(generation_id), "size_bytes": path.stat().st_size,
                          "sha256": _sha256_file(path)},
        "datasets": datasets, "factor_research_rows": factor_rows, "quality": quality,
        "capabilities": {k: v.get("status") for k, v in meta["capabilities"].items()},
        "timezone_status": "unverified",
        "cross_file_consistency": meta.get("cross_file_consistency",
                                           {"status": "unverified", "reason": CROSS_FILE_REASON}),
    }


def format_export(result: dict) -> str:
    manifest = result["manifest"]
    label = " (SYNTHETIC TEST DATA)" if manifest["data_label"] == "synthetic_test" else ""
    lines = [f"Exported generation {manifest['generation_id']}{label}", f"Folder: {result['folder']}", "",
             "Upload these two files to the private Drive folder, in this order:"]
    for step, path in enumerate(result["files"], start=1):
        lines.append(f"  {step}. {path.name}  ({path.stat().st_size:,} bytes)")
    lines += ["", f"Snapshot SHA-256: {manifest['snapshot_file']['sha256']}",
              "Wait until the first upload has finished before uploading the manifest. When Drive asks about "
              f"an existing {MANIFEST_NAME}, choose 'Replace existing file'."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export one snapshot generation for manual Drive upload.")
    parser.add_argument("--private-dir", required=True, type=Path, help="Private folder holding snapshots/.")
    parser.add_argument("--generation", help="Generation id to export (default: the newest).")
    args = parser.parse_args(argv)
    try:
        result = export_generation(args.private_dir, args.generation)
    except (ExportFailed, OSError) as exc:
        print(f"EXPORT FAILED - nothing to upload.\n{exc}", file=sys.stderr)
        return 1
    print(format_export(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
