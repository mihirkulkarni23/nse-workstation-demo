"""Drive publication tests: local export, then the read-only Drive reader against an in-memory Drive.

Every snapshot here is built from synthetic fixtures. No network call is made: a fake HTTP session
serves the three Drive v3 endpoints the reader uses, so the real GoogleDrive class is exercised.
"""

import ast
import hashlib
import json
import os
import tempfile
import tomllib
from pathlib import Path
import unittest
from unittest import mock

import requests
from google.auth import exceptions as google_exceptions
from streamlit.testing.v1 import AppTest

import drive_reader
import snapshot_views
from dashboard_data import NAV_OPTIONS, configured_mode
from drive_reader import (
    FILES_URL, FOLDER_MIME, MANIFEST_NAME, READONLY_SCOPE, DriveError, DriveState, GoogleDrive, connect,
    fetch_generation, reader_config,
)
from producer.export_snapshot import ExportFailed, export_generation
from producer.import_csv import run_import
from producer.synthetic import build_synthetic_publication, synthetic_rows, write_fixture

ROOT = Path(__file__).resolve().parents[1]
FOLDER = "folderABCDEFGHIJ"


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, body: bytes = b"", fail_at: int | None = None):
        self.status_code, self.payload, self.body, self.fail_at = status, payload, body, fail_at

    def json(self):
        return self.payload

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self.body), 1000):
            if self.fail_at is not None and start >= self.fail_at:
                raise requests.ConnectionError("reset")
            yield self.body[start:start + 1000]

    def close(self):
        pass


class FakeDriveSession:
    """In-memory stand-in for an authorized session on Drive v3. It only implements GET."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.status, self.auth_error, self.fail_at = 200, False, None
        self.requests: list[tuple[str, dict]] = []

    def put(self, name: str, data: bytes, listed_size: int | None = None) -> str:
        file_id = f"file{len(self.files):012d}"
        self.files[file_id] = {"name": name, "data": data, "size": len(data) if listed_size is None else listed_size}
        return file_id

    def replace(self, file_id: str, data: bytes) -> None:
        self.files[file_id].update(data=data, size=len(data))

    def downloads(self) -> int:
        return sum(1 for _, params in self.requests if params.get("alt") == "media")

    def get(self, url, params=None, stream=False, timeout=None):
        self.requests.append((url, dict(params or {})))
        if self.auth_error:
            raise google_exceptions.RefreshError("invalid_grant")
        if self.status != 200:
            return FakeResponse(self.status)
        if url == FILES_URL:
            return FakeResponse(200, {"files": [{"id": i, "name": f["name"], "size": str(f["size"]),
                                                 "mimeType": "application/octet-stream"}
                                                for i, f in self.files.items()]})
        item = url.rsplit("/", 1)[1]
        if item == FOLDER:
            return FakeResponse(200, {"id": FOLDER, "mimeType": FOLDER_MIME, "trashed": False})
        if item in self.files and params.get("alt") == "media":
            return FakeResponse(200, body=self.files[item]["data"], fail_at=self.fail_at)
        return FakeResponse(404)


def publication_files(result: dict) -> tuple[bytes, bytes, dict]:
    snapshot, manifest = (p.read_bytes() for p in result["files"])
    return snapshot, manifest, result["manifest"]


class PublicationFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.private = cls.tmp / "private"
        cls.first = build_synthetic_publication(cls.private)
        rows = synthetic_rows()
        rows["daily_equity"].append({**rows["daily_equity"][-1], "date": "2020-01-08"})
        write_fixture(cls.tmp / "second", rows)
        second_id = run_import(cls.tmp / "second", cls.private, data_label="synthetic_test")["generation_id"]
        cls.second = export_generation(cls.private, second_id)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def drive_with(self, result: dict, snapshot: bytes | None = None, manifest: bytes | None = None,
                   listed_size: int | None = None) -> FakeDriveSession:
        session = FakeDriveSession()
        self.publish(session, result, snapshot, manifest, listed_size)
        return session

    def publish(self, session, result, snapshot=None, manifest=None, listed_size=None) -> tuple[str, str]:
        real_snapshot, real_manifest, info = publication_files(result)
        snap_id = session.put(info["snapshot_file"]["name"], real_snapshot if snapshot is None else snapshot,
                              listed_size)
        manifest_id = next((i for i, f in session.files.items() if f["name"] == MANIFEST_NAME), None)
        if manifest_id:
            session.replace(manifest_id, real_manifest if manifest is None else manifest)
        else:
            manifest_id = session.put(MANIFEST_NAME, real_manifest if manifest is None else manifest)
        return snap_id, manifest_id

    def check(self, state: DriveState, session: FakeDriveSession) -> DriveState:
        state.refresh(lambda: GoogleDrive(session), FOLDER, force=True)
        return state


class ExportTests(PublicationFixture):
    def test_export_writes_immutable_generation_and_manifest(self):
        info = self.first["manifest"]
        generation = info["generation_id"]
        self.assertEqual([p.name for p in self.first["files"]],
                         [f"nse_snapshot_{generation}.sqlite", "current_manifest.json"])
        snapshot, manifest_bytes, _ = publication_files(self.first)
        self.assertEqual(json.loads(manifest_bytes), info)
        self.assertEqual(info["snapshot_file"]["sha256"], hashlib.sha256(snapshot).hexdigest())
        self.assertEqual(info["snapshot_file"]["size_bytes"], len(snapshot))
        self.assertEqual((info["kind"], info["manifest_schema"], info["snapshot_schema"], info["data_label"]),
                         ("nse_dashboard_snapshot_manifest", 1, 1, "synthetic_test"))
        self.assertEqual({k: v["stored_rows"] for k, v in info["datasets"].items()},
                         {"scanner_30m_signals": 2, "hourly_signal_scan": 3, "daily_signal_scan": 2,
                          "open_positions": 2, "closed_positions": 2, "daily_equity": 2})
        self.assertEqual(info["datasets"]["daily_equity"]["latest_in_file"], "2020-01-07")
        self.assertIn("source_modified_utc", info["datasets"]["daily_equity"])
        text = manifest_bytes.decode()
        for record_value in ("AAACOD", "BBB", "-30.25", "STOPLOSS"):
            self.assertNotIn(record_value, text)

    def test_export_never_overwrites_and_leaves_source_unchanged(self):
        generation = self.first["manifest"]["generation_id"]
        source = self.private / "snapshots" / generation / "dashboard_snapshot.sqlite"
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        exported = self.first["files"][0].read_bytes()
        with self.assertRaisesRegex(ExportFailed, "already exported"):
            export_generation(self.private, generation)
        self.assertEqual(self.first["files"][0].read_bytes(), exported)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.assertFalse(list((self.private / "publish").glob(".incomplete-*")))
        with self.assertRaisesRegex(ExportFailed, "Git working tree"):
            export_generation(ROOT, generation)

    def test_newest_generation_is_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(Path(tmp) / "src", synthetic_rows())
            run_import(Path(tmp) / "src", Path(tmp) / "p", data_label="synthetic_test")
            result = export_generation(Path(tmp) / "p")
            self.assertTrue(result["files"][0].name.startswith("nse_snapshot_"))


class DriveReaderTests(PublicationFixture):
    def test_valid_generation_is_verified_and_activated(self):
        session = self.drive_with(self.first)
        state = self.check(DriveState(), session)
        self.assertIsNone(state.last_error)
        self.assertFalse(state.stale)
        snap = state.active.snapshot
        self.assertEqual(snap["meta"]["generation_id"], self.first["manifest"]["generation_id"])
        self.assertEqual(snap["meta"]["data_label"], "synthetic_test")
        self.assertIn("verified", snap["path"])
        self.assertEqual(len(snap["trades"]), 2)
        self.assertTrue(all(url.startswith(FILES_URL) for url, _ in session.requests))
        listing = next(p for url, p in session.requests if url == FILES_URL)
        self.assertIn(f"'{FOLDER}' in parents and trashed = false", listing["q"])

    def assertUnavailable(self, session: FakeDriveSession, message: str) -> None:
        state = self.check(DriveState(), session)
        self.assertIsNone(state.active)
        self.assertRegex(state.last_error, message)

    def test_missing_or_partial_uploads_are_not_activated(self):
        self.assertUnavailable(FakeDriveSession(), "No current_manifest.json")
        session = FakeDriveSession()
        session.put(MANIFEST_NAME, publication_files(self.first)[1])
        self.assertUnavailable(session, "not in the folder")
        snapshot = publication_files(self.first)[0]
        self.assertUnavailable(self.drive_with(self.first, snapshot=snapshot[:5000]), "bytes in Drive but the manifest")
        self.assertUnavailable(self.drive_with(self.first, snapshot=snapshot[:5000], listed_size=len(snapshot)),
                               "stopped after 5,000")
        self.assertUnavailable(self.drive_with(self.first, manifest=publication_files(self.first)[1][:200]),
                               "not valid JSON")
        interrupted = self.drive_with(self.first)
        interrupted.fail_at = 4000
        self.assertUnavailable(interrupted, "Download interrupted")

    def test_wrong_checksum_is_rejected(self):
        snapshot = bytearray(publication_files(self.first)[0])
        snapshot[-1] ^= 0xFF
        self.assertUnavailable(self.drive_with(self.first, snapshot=bytes(snapshot)), "SHA-256")
        manifest = json.loads(publication_files(self.first)[1])
        manifest["snapshot_file"]["sha256"] = "0" * 64
        self.assertUnavailable(self.drive_with(self.first, manifest=json.dumps(manifest).encode()), "SHA-256")

    def test_manifest_that_disagrees_with_snapshot_is_rejected(self):
        manifest = json.loads(publication_files(self.first)[1])
        manifest["datasets"]["closed_positions"]["stored_rows"] = 99
        self.assertUnavailable(self.drive_with(self.first, manifest=json.dumps(manifest).encode()), "row counts")
        for field, value, message in (("kind", "other", "not an NSE snapshot manifest"),
                                      ("snapshot_schema", 2, "cannot read"),
                                      ("generation_id", "../../x", "generation_id")):
            broken = json.loads(publication_files(self.first)[1])
            broken[field] = value
            self.assertUnavailable(self.drive_with(self.first, manifest=json.dumps(broken).encode()), message)

    def test_duplicate_candidates_never_silently_win(self):
        session = self.drive_with(self.first)
        session.put(MANIFEST_NAME, publication_files(self.first)[1])
        self.assertUnavailable(session, "2 files named current_manifest.json")
        session = self.drive_with(self.first)
        session.put(self.first["manifest"]["snapshot_file"]["name"], publication_files(self.first)[0])
        self.assertUnavailable(session, "2 files named nse_snapshot_")

    def test_revoked_access_keeps_previous_generation_marked_stale(self):
        session = self.drive_with(self.first)
        state = self.check(DriveState(), session)
        generation = state.active.manifest["generation_id"]
        for status, message in ((404, "Viewer access"), (403, "HTTP 403"), (401, "HTTP 401"), (500, "HTTP 500")):
            session.status = status
            self.check(state, session)
            self.assertTrue(state.stale, status)
            self.assertEqual(state.active.manifest["generation_id"], generation)
            self.assertRegex(state.last_error, message)
        session.status, session.auth_error = 200, True
        self.check(state, session)
        self.assertRegex(state.last_error, "refused the reader's service-account credentials")
        session.auth_error = False
        self.check(state, session)
        self.assertFalse(state.stale)

    def test_bad_newer_generation_keeps_previous_until_fixed(self):
        session = self.drive_with(self.first)
        state = self.check(DriveState(), session)
        first_id = state.active.manifest["generation_id"]
        good = publication_files(self.second)[0]
        snap_id, _ = self.publish(session, self.second, snapshot=good[:-1] + bytes([good[-1] ^ 1]))
        self.check(state, session)
        self.assertTrue(state.stale)
        self.assertEqual(state.active.manifest["generation_id"], first_id)
        self.assertIn(self.second["manifest"]["generation_id"], state.last_error)
        session.replace(snap_id, good)
        self.check(state, session)
        self.assertFalse(state.stale)
        self.assertEqual(state.active.manifest["generation_id"], self.second["manifest"]["generation_id"])
        self.assertEqual(len(state.active.snapshot["pnl"]), 3)

    def test_unchanged_manifest_does_not_download_again(self):
        session = self.drive_with(self.first)
        state = self.check(DriveState(), session)
        downloads = session.downloads()
        self.check(state, session)
        self.assertEqual(session.downloads(), downloads + 1)  # the manifest only

    def test_refresh_waits_for_the_interval_unless_forced(self):
        session = self.drive_with(self.first)
        state = self.check(DriveState(), session)
        opened = mock.Mock(return_value=GoogleDrive(session))
        state.refresh(opened, FOLDER)
        opened.assert_not_called()
        state.refresh(opened, FOLDER, force=True)
        opened.assert_called_once()

    def test_restart_without_cached_snapshot_downloads_again_or_shows_unavailable(self):
        staging = set(Path(tempfile.gettempdir()).glob("nse-drive-*"))
        session = self.drive_with(self.first)
        self.check(DriveState(), session)
        # A restart is a new, empty process state; nothing was kept on disk to fall back on.
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("nse-drive-*")), staging)
        restarted = self.check(DriveState(), session)
        self.assertIsNotNone(restarted.active)
        self.assertEqual(session.downloads(), 4)  # manifest + snapshot, twice
        session.status = 404
        self.assertUnavailable(session, "Viewer access")


class ConfigTests(unittest.TestCase):
    ACCOUNT = {"type": "service_account", "project_id": "nse-test", "private_key_id": "abc123",
               "private_key": "-----BEGIN PRIVATE KEY-----\nSECRETVALUE\n-----END PRIVATE KEY-----\n",
               "client_email": "dashboard-reader@nse-test.iam.gserviceaccount.com",
               "token_uri": "https://oauth2.googleapis.com/token"}

    def test_reader_config_validates_without_echoing_values(self):
        self.assertEqual(reader_config({"drive_reader": {"folder_id": FOLDER, "service_account": self.ACCOUNT}}),
                         (FOLDER, self.ACCOUNT))
        for secrets, message in (({}, "no \\[drive_reader\\]"),
                                 ({"drive_reader": {"folder_id": FOLDER, "service_account": {}}}, "lacks type"),
                                 ({"drive_reader": {"folder_id": "bad id'", "service_account": self.ACCOUNT}},
                                  "not a Drive folder ID")):
            with self.assertRaisesRegex(ValueError, message):
                reader_config(secrets)
        try:
            reader_config({"drive_reader": {"folder_id": "x", "service_account": self.ACCOUNT}})
        except ValueError as exc:
            self.assertNotIn("SECRETVALUE", str(exc))

    def test_secrets_template_is_placeholders_only(self):
        template = tomllib.loads((ROOT / "docs" / "streamlit-secrets.template.toml").read_text(encoding="utf-8"))
        self.assertEqual(configured_mode(template), ("drive_snapshot", None))
        with self.assertRaisesRegex(ValueError, "placeholder"):
            reader_config(template)
        self.assertNotIn("PRIVATE KEY-----\nMII", json.dumps(template))

    def test_connect_requests_only_the_readonly_scope(self):
        with mock.patch("google.oauth2.service_account.Credentials.from_service_account_info") as build:
            connect(self.ACCOUNT)
        self.assertEqual(build.call_args.kwargs["scopes"], [READONLY_SCOPE])
        self.assertEqual(READONLY_SCOPE, "https://www.googleapis.com/auth/drive.readonly")
        with self.assertRaisesRegex(DriveError, "could not be read"):
            connect(self.ACCOUNT)

    def test_reader_makes_only_get_requests(self):
        tree = ast.parse((ROOT / "drive_reader.py").read_text(encoding="utf-8"))
        calls = {ast.unparse(n.func).split(".")[-1] for n in ast.walk(tree) if isinstance(n, ast.Call)}
        self.assertFalse(calls & {"post", "put", "patch", "delete", "request", "write_text", "write_bytes"})
        scopes = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and "googleapis.com/auth/" in n.value}
        self.assertEqual(scopes, {READONLY_SCOPE})


class DriveAppTests(PublicationFixture):
    SECRETS = {"folder_id": FOLDER, "service_account": ConfigTests.ACCOUNT}

    def setUp(self):
        snapshot_views._drive_state.clear()
        patcher = mock.patch.dict(os.environ, {"NSE_DATA_MODE": "drive_snapshot"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(snapshot_views._drive_state.clear)

    def start(self, session: FakeDriveSession | None, secrets: dict | None = None) -> AppTest:
        patcher = mock.patch("snapshot_views.connect", return_value=GoogleDrive(session))
        patcher.start()
        self.addCleanup(patcher.stop)
        app = AppTest.from_file(str(ROOT / "Ldashboard_V9.py"), default_timeout=60)
        if secrets is not None:
            app.secrets["drive_reader"] = secrets
        return app.run()

    def assertNoFigures(self, app: AppTest, message: str) -> None:
        self.assertFalse(app.exception)
        self.assertTrue(any(message in e.value for e in app.error), [e.value for e in app.error])
        self.assertEqual((len(app.metric), len(app.dataframe)), (0, 0))
        self.assertFalse(any("DEMO DATA" in w.value for w in app.warning))

    def test_unconfigured_secrets_show_error_not_demo(self):
        self.assertNoFigures(self.start(FakeDriveSession()), "DATA SOURCE NOT CONFIGURED")
        self.assertNoFigures(self.start(FakeDriveSession(), {"folder_id": "REPLACE_WITH_FOLDER_ID",
                                                             "service_account": ConfigTests.ACCOUNT}),
                             "placeholder")

    def test_nine_views_from_drive_then_stale_on_revoked_access(self):
        session = self.drive_with(self.first)
        app = self.start(session, self.SECRETS)
        self.assertFalse(app.exception)
        self.assertTrue(any("SYNTHETIC TEST SNAPSHOT" in w.value and "Google Drive" in w.value for w in app.warning))
        metrics = {m.label: m.value for m in app.metric[:6]}
        self.assertEqual((metrics["Source status"], metrics["Recorded closed trades"]), ("SNAPSHOT", "2"))
        for name in NAV_OPTIONS:
            app.radio(key="dashboard_view").set_value(name).run()
            self.assertFalse(app.exception, name)
        session.status = 404
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any(w.value.startswith("STALE") and "Viewer access" in w.value for w in app.warning))
        self.assertEqual({m.label: m.value for m in app.metric[:6]}["Source status"], "STALE")

    def test_cold_start_with_failed_download_shows_unavailable(self):
        session = self.drive_with(self.first)
        session.status = 403
        app = self.start(session, self.SECRETS)
        self.assertNoFigures(app, "DRIVE SNAPSHOT UNAVAILABLE")
        session.status = 200
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual({m.label: m.value for m in app.metric[:6]}["Source status"], "SNAPSHOT")


if __name__ == "__main__":
    unittest.main()
