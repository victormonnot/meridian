"""Fenced import orchestration, cancellation and conservative recovery."""
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from meridian.vio_job_control import AttemptCancelled, AttemptLeaseLost
from . import acquisition_worker as worker


class ImportWorkerTests(SimpleTestCase):
    def test_capacity_reserves_publication_bytes_above_host_free_space_floor(self):
        durable, scratch = Path("/fixture/retained"), Path("/fixture/scratch")
        with override_settings(MERIDIAN_ACQUISITION_ROOT=durable, MERIDIAN_IMPORT_SCRATCH_ROOT=scratch), \
             patch.object(Path, "read_text", return_value="MemAvailable: 3000000 kB\n"):
            for free, expected in ((16 * 1024**3 - 1, False), (16 * 1024**3, True)):
                with patch.object(worker.shutil, "disk_usage", side_effect=lambda path:
                    SimpleNamespace(free=free if path == durable else 6 * 1024**3)):
                    assert worker.capacity_available() is expected

    def test_publish_and_finish_follow_confirmed_child_cleanup(self):
        acquisition = SimpleNamespace(pk=uuid4(), options={"imu_stream_id": None, "include_reference": True})
        attempt = uuid4()
        events = []
        outcome = {"state": "ready", "sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with override_settings(MERIDIAN_IMPORT_SCRATCH_ROOT=root, MERIDIAN_IMPORT_IMAGE="sha256:" + "b" * 64), \
                 patch.object(worker.queue, "claim_import", return_value=(acquisition, attempt)), \
                 patch.object(worker, "LeaseGuard"), \
                 patch.object(worker.storage, "source_path", return_value=root / "original"), \
                 patch.object(worker, "execute_import", side_effect=lambda *a, **kw: events.append("execute") or outcome), \
                 patch.object(worker, "cleanup_import", side_effect=lambda *a: events.append("stopped") or True), \
                 patch.object(worker.storage, "publish_import", side_effect=lambda *a, **kw: events.append("published") or True), \
                 patch.object(worker.queue, "finish_import", side_effect=lambda *a: events.append("finished") or True):
                assert worker.run_one(uuid4(), threading.Event()) is True
            assert events == ["execute", "stopped", "published", "finished"]
            assert list(root.iterdir()) == []

    def test_failed_child_cleanup_keeps_attempt_occupied_and_scratch_retained(self):
        acquisition = SimpleNamespace(pk=uuid4(), options={"imu_stream_id": None, "include_reference": True})
        attempt = uuid4()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with override_settings(MERIDIAN_IMPORT_SCRATCH_ROOT=root, MERIDIAN_IMPORT_IMAGE="sha256:" + "b" * 64), \
                 patch.object(worker.queue, "claim_import", return_value=(acquisition, attempt)), \
                 patch.object(worker, "LeaseGuard"), \
                 patch.object(worker.storage, "source_path", return_value=root / "original"), \
                 patch.object(worker, "execute_import", return_value={"state": "ready"}), \
                 patch.object(worker, "cleanup_import", return_value=False), \
                 patch.object(worker.storage, "publish_import") as publish, \
                 patch.object(worker.queue, "finish_import") as finish:
                worker.run_one(uuid4(), threading.Event())
            publish.assert_not_called()
            finish.assert_not_called()
            assert (root / str(attempt)).is_dir()

    def test_rejected_package_uses_supported_failure_code(self):
        acquisition = SimpleNamespace(pk=uuid4(), options={"imu_stream_id": None, "include_reference": True})
        attempt = uuid4()
        with tempfile.TemporaryDirectory() as temporary:
            with override_settings(MERIDIAN_IMPORT_SCRATCH_ROOT=Path(temporary), MERIDIAN_IMPORT_IMAGE="sha256:" + "b" * 64), \
                 patch.object(worker.queue, "claim_import", return_value=(acquisition, attempt)), \
                 patch.object(worker, "LeaseGuard"), \
                 patch.object(worker.storage, "source_path", return_value=Path(temporary) / "original"), \
                 patch.object(worker, "execute_import", side_effect=worker.ImportFailure("invalid_package")), \
                 patch.object(worker, "cleanup_import", return_value=True), \
                 patch.object(worker.queue, "finish_import") as finish:
                worker.run_one(uuid4(), threading.Event())
            assert finish.call_args.args[2]["failure"]["code"] == "invalid_input"

    def test_expired_attempt_is_not_recovered_when_scratch_cleanup_fails(self):
        acquisition, attempt = uuid4(), uuid4()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / str(attempt)).mkdir()
            with override_settings(MERIDIAN_IMPORT_SCRATCH_ROOT=root), \
                 patch.object(worker.queue, "expired_imports", return_value=[(acquisition, attempt)]), \
                 patch.object(worker, "cleanup_import", return_value=True), \
                 patch.object(worker.shutil, "rmtree", side_effect=OSError("busy")), \
                 patch.object(worker.queue, "recover_import") as recover:
                with self.assertRaises(OSError):
                    worker.reconcile()
                recover.assert_not_called()

    def test_guard_enforces_local_lease_expiry_and_total_attempt_deadline(self):
        guard = worker.LeaseGuard(uuid4(), uuid4(), threading.Event())
        with patch.object(worker.queue, "heartbeat_import", return_value="continue"):
            guard._renew()
        guard.checkpoint()
        guard.attempt_deadline = time.monotonic() - 1
        with self.assertRaises(worker.ImportFailure) as caught:
            guard.checkpoint("publishing")
        assert caught.exception.code == "timeout"
        guard.deadline = time.monotonic() - 1
        with self.assertRaises(AttemptLeaseLost):
            guard.checkpoint()

    def test_guard_cancellation_does_not_depend_on_progress_output(self):
        guard = worker.LeaseGuard(uuid4(), uuid4(), threading.Event())
        with patch.object(worker.queue, "heartbeat_import", return_value="cancel"):
            guard._renew()
        with self.assertRaises(AttemptCancelled):
            guard.checkpoint()
