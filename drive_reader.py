"""Read-only Google Drive consumer for manually uploaded snapshot generations.

Each check lists one private folder, downloads current_manifest.json and the snapshot file it names,
verifies size and SHA-256, then opens the file read-only and validates it with snapshot_reader before
activating it. Only the drive.readonly scope is requested and only GET requests are made. Downloads go
to a temporary folder that is deleted once the snapshot is in memory, so nothing relies on the cloud
disk persisting: after a restart the current generation is downloaded and verified again.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import requests
from google.auth import exceptions as google_exceptions

from snapshot_reader import GENERATION_NAME, SCHEMA_VERSION, SnapshotError, load_uploaded_snapshot

READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
MANIFEST_NAME = "current_manifest.json"
MANIFEST_KIND = "nse_dashboard_snapshot_manifest"
MAX_MANIFEST_BYTES = 1_000_000
MAX_SNAPSHOT_BYTES = 2_000_000_000
CHECK_INTERVAL_SECONDS = 300
TIMEOUT = (10, 120)
DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ACCOUNT_KEYS = ("type", "project_id", "private_key_id", "private_key", "client_email", "token_uri")
PLACEHOLDER = "REPLACE"


class DriveError(Exception):
    """One Drive check failed. Callers keep any generation validated earlier and never show demo data."""


def reader_config(secrets) -> tuple[str, dict]:
    """Return (folder_id, service-account info) from Streamlit Secrets. Messages name keys, never values."""
    try:
        section = secrets.get("drive_reader")
    except FileNotFoundError:
        section = None
    if not section:
        raise ValueError("Streamlit Secrets have no [drive_reader] section.")
    folder_id = str(section.get("folder_id", ""))
    account = dict(section.get("service_account") or {})
    missing = [key for key in ACCOUNT_KEYS if not account.get(key)]
    if missing:
        raise ValueError(f"[drive_reader.service_account] lacks {', '.join(missing)}.")
    if PLACEHOLDER in folder_id or any(PLACEHOLDER in str(account[key]) for key in ACCOUNT_KEYS):
        raise ValueError("Streamlit Secrets still contain REPLACE placeholder values.")
    if not DRIVE_ID.match(folder_id):
        raise ValueError("[drive_reader] folder_id is not a Drive folder ID.")
    if account["type"] != "service_account":
        raise ValueError("[drive_reader.service_account] type must be service_account.")
    return folder_id, account


class GoogleDrive:
    """Minimal Drive v3 reader: folder metadata, child listing and file download. GET requests only."""

    def __init__(self, session):
        self.session = session

    def _get(self, url: str, params: dict, stream: bool = False):
        try:
            response = self.session.get(url, params=params, stream=stream, timeout=TIMEOUT)
        except google_exceptions.RefreshError:
            raise DriveError("Google refused the reader's service-account credentials. The key may have "
                             "been deleted or disabled, or Secrets hold the wrong key.") from None
        except (google_exceptions.GoogleAuthError, requests.RequestException) as exc:
            raise DriveError(f"Could not reach Google Drive ({type(exc).__name__}).") from None
        if response.status_code == 200:
            return response
        response.close()
        if response.status_code == 401:
            raise DriveError("Google rejected the reader's credentials (HTTP 401).")
        if response.status_code in (403, 404):
            raise DriveError(f"The reader cannot see this Drive item (HTTP {response.status_code}). Viewer access "
                             "may have been removed, the item moved, or the folder ID is wrong.")
        raise DriveError(f"Google Drive request failed (HTTP {response.status_code}).")

    def _json(self, url: str, params: dict) -> dict:
        try:
            return self._get(url, params).json()
        except ValueError:
            raise DriveError("Google Drive returned an unreadable response.") from None

    def folder(self, folder_id: str) -> dict:
        return self._json(f"{FILES_URL}/{folder_id}", {"fields": "id,mimeType,trashed", "supportsAllDrives": "true"})

    def children(self, folder_id: str) -> list[dict]:
        files, token = [], None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed = false", "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name,size,mimeType)",
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            page = self._json(FILES_URL, params)
            files += page.get("files", [])
            token = page.get("nextPageToken")
            if not token:
                return files

    def download(self, file_id: str) -> Iterator[bytes]:
        response = self._get(f"{FILES_URL}/{file_id}", {"alt": "media", "supportsAllDrives": "true"}, stream=True)
        try:
            yield from response.iter_content(chunk_size=1 << 20)
        except requests.RequestException as exc:
            raise DriveError(f"Download interrupted ({type(exc).__name__}).") from None
        finally:
            response.close()


def connect(account: dict) -> GoogleDrive:
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    try:
        credentials = service_account.Credentials.from_service_account_info(account, scopes=[READONLY_SCOPE])
    except (ValueError, KeyError, TypeError):
        raise DriveError("The service-account key in Streamlit Secrets could not be read; check private_key "
                         "was copied completely.") from None
    return GoogleDrive(AuthorizedSession(credentials))


def _single(files: list[dict], name: str) -> dict | None:
    matches = [f for f in files if f.get("name") == name]
    if len(matches) > 1:
        raise DriveError(f"{len(matches)} files named {name} are in the Drive folder; refusing to choose between "
                         "them. Keep one and move the others to Bin.")
    if matches and not DRIVE_ID.match(str(matches[0].get("id", ""))):
        raise DriveError(f"Drive returned an unexpected file ID for {name}.")
    return matches[0] if matches else None


def parse_manifest(raw: bytes) -> dict:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DriveError(f"{MANIFEST_NAME} is not valid JSON (incomplete upload or wrong file).") from None
    if not isinstance(manifest, dict) or manifest.get("kind") != MANIFEST_KIND or manifest.get("manifest_schema") != 1:
        raise DriveError(f"{MANIFEST_NAME} is not an NSE snapshot manifest (manifest schema 1).")
    if manifest.get("snapshot_schema") != SCHEMA_VERSION or manifest.get("mode") != "uploaded_snapshot":
        raise DriveError(f"The manifest describes a snapshot this app cannot read (expected schema {SCHEMA_VERSION}).")
    generation = manifest.get("generation_id")
    if not isinstance(generation, str) or not GENERATION_NAME.match(generation):
        raise DriveError("The manifest has no valid generation_id.")
    entry = manifest.get("snapshot_file")
    if not (isinstance(entry, dict) and entry.get("name") == f"nse_snapshot_{generation}.sqlite"
            and type(entry.get("size_bytes")) is int and 0 < entry["size_bytes"] <= MAX_SNAPSHOT_BYTES
            and isinstance(entry.get("sha256"), str) and SHA256.match(entry["sha256"])):
        raise DriveError("The manifest's snapshot_file entry is missing or inconsistent.")
    datasets = manifest.get("datasets")
    if not (isinstance(datasets, dict) and datasets and all(
            isinstance(d, dict) and type(d.get("stored_rows")) is int for d in datasets.values())):
        raise DriveError("The manifest's dataset counts are missing.")
    if not isinstance(manifest.get("generation_created_at_utc"), str) or not isinstance(manifest.get("data_label"), str):
        raise DriveError("The manifest lacks its creation time or data label.")
    return manifest


def _download_manifest(drive: GoogleDrive, file_id: str) -> bytes:
    chunks, data = drive.download(file_id), b""
    try:
        for chunk in chunks:
            data += chunk
            if len(data) > MAX_MANIFEST_BYTES:
                raise DriveError(f"{MANIFEST_NAME} is too large to be a manifest.")
    finally:
        chunks.close()
    return data


def _download_verified(drive: GoogleDrive, file_id: str, target: Path, size: int, sha256: str) -> None:
    chunks, digest, received = drive.download(file_id), hashlib.sha256(), 0
    try:
        with target.open("wb") as fh:
            for chunk in chunks:
                received += len(chunk)
                if received > size:
                    raise DriveError("the snapshot download is larger than its manifest says.")
                digest.update(chunk)
                fh.write(chunk)
    finally:
        chunks.close()
    if received != size:
        raise DriveError(f"the snapshot download stopped after {received:,} of {size:,} bytes.")
    if digest.hexdigest() != sha256:
        raise DriveError("the snapshot's SHA-256 checksum does not match the manifest. The file is corrupt, "
                         "incomplete, or not the one the manifest describes.")


def fetch_generation(drive: GoogleDrive, folder_id: str, active_generation: str | None = None) -> tuple[dict, dict | None]:
    """Return (manifest, snapshot). Snapshot is None when the manifest still names the active generation."""
    folder = drive.folder(folder_id)
    if folder.get("mimeType") != FOLDER_MIME or folder.get("trashed"):
        raise DriveError("The configured Drive folder ID is not an available folder.")
    files = drive.children(folder_id)
    entry = _single(files, MANIFEST_NAME)
    if entry is None:
        raise DriveError(f"No {MANIFEST_NAME} in the Drive folder yet (not uploaded, or still uploading).")
    manifest = parse_manifest(_download_manifest(drive, entry["id"]))
    generation = manifest["generation_id"]
    if generation == active_generation:
        return manifest, None

    try:
        snapshot = _download_generation(drive, files, manifest)
    except DriveError as exc:
        raise DriveError(f"Generation {generation} was not activated: {exc}") from None
    return manifest, snapshot


def _download_generation(drive: GoogleDrive, files: list[dict], manifest: dict) -> dict:
    expected = manifest["snapshot_file"]
    snapshot_file = _single(files, expected["name"])
    if snapshot_file is None:
        raise DriveError(f"the manifest names {expected['name']}, which is not in the folder (upload missing, "
                         "still in progress, or uploaded after the manifest).")
    if str(snapshot_file.get("size")) != str(expected["size_bytes"]):
        raise DriveError(f"{expected['name']} is {snapshot_file.get('size', 'unknown')} bytes in Drive but the "
                         f"manifest says {expected['size_bytes']:,} (incomplete upload?).")
    with tempfile.TemporaryDirectory(prefix="nse-drive-") as staging:
        target = Path(staging) / expected["name"]
        _download_verified(drive, snapshot_file["id"], target, expected["size_bytes"], expected["sha256"])
        try:
            snapshot = load_uploaded_snapshot(target)
        except SnapshotError as exc:
            raise DriveError(f"it downloaded but failed validation: {exc}") from None
    _check_matches(manifest, snapshot)
    snapshot["path"] = f"Google Drive file {expected['name']} (SHA-256 {expected['sha256'][:12]}…, verified)"
    return snapshot


def _check_matches(manifest: dict, snapshot: dict) -> None:
    meta, recorded = snapshot["meta"], snapshot["sources"].set_index("dataset")
    problems = []
    if meta["generation_id"] != manifest["generation_id"]:
        problems.append("generation id")
    if meta["created_at_utc"] != manifest["generation_created_at_utc"]:
        problems.append("creation time")
    if meta.get("data_label", "supplied_csv") != manifest["data_label"]:
        problems.append("data label")
    if set(recorded.index) != set(manifest["datasets"]) or any(
            int(recorded.loc[name].stored_rows) != counts["stored_rows"]
            for name, counts in manifest["datasets"].items() if name in recorded.index):
        problems.append("row counts")
    if problems:
        raise DriveError(f"its snapshot contents do not match the manifest ({', '.join(problems)}).")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass
class Activation:
    manifest: dict
    snapshot: dict
    activated_at: datetime


@dataclass
class DriveState:
    """Process-wide Drive status, in memory only. Holds at most one validated generation."""

    active: Activation | None = None
    last_check: datetime | None = None
    last_error: str | None = None
    _next_check: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def stale(self) -> bool:
        return self.active is not None and self.last_error is not None

    def refresh(self, open_drive: Callable[[], GoogleDrive], folder_id: str, force: bool = False) -> None:
        """Check Drive if due. A failure keeps the active generation and records why it is stale."""
        if not force and time.monotonic() < self._next_check:
            return
        # Viewers with a generation to show never wait for another viewer's download.
        if not self._lock.acquire(blocking=self.active is None):
            return
        try:
            if not force and time.monotonic() < self._next_check:
                return
            active = self.active.manifest["generation_id"] if self.active else None
            try:
                manifest, snapshot = fetch_generation(open_drive(), folder_id, active)
                if snapshot is not None:
                    self.active = Activation(manifest, snapshot, _now())
                self.last_error = None
            except DriveError as exc:
                self.last_error = str(exc)
            self.last_check = _now()
            self._next_check = time.monotonic() + CHECK_INTERVAL_SECONDS
        finally:
            self._lock.release()
