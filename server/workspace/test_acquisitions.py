"""Upload authorization, crash recovery, atomic publication and quota evidence."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from . import acquisition_queue as queue, acquisition_storage as storage
from .job_queue import claim as claim_vio, queue_lock
from .models import Acquisition, ComputeQueue, Experiment, Job, Project, UploadChunk
from .test_postgres import ConcurrentDatabaseWorkers

EXAMPLES = Path(__file__).resolve().parents[2] / "examples/sensor-contracts"
OPTIONS = {"imu_stream_id": None, "include_reference": True}


class AcquisitionSetup:
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings_override = override_settings(
            MERIDIAN_UPLOADS_ENABLED=True, MERIDIAN_ACQUISITION_ROOT=self.root,
            MERIDIAN_ACQUISITION_UPLOAD_BYTES=1024 * 1024,
            MERIDIAN_ACQUISITION_UNPACKED_BYTES=1024 * 1024,
            MERIDIAN_ACQUISITION_PREPARED_BYTES=1024 * 1024,
            MERIDIAN_ACQUISITION_ACCOUNT_BYTES=8 * 1024 * 1024,
            MERIDIAN_ACQUISITION_GLOBAL_BYTES=16 * 1024 * 1024,
            MERIDIAN_ACQUISITION_CHUNK_BYTES=16,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.owner = get_user_model().objects.create_user("upload.owner")
        self.other = get_user_model().objects.create_user("upload.other")
        self.project = Project.objects.create(owner=self.owner, name="Private input", dataset_id="tumvi-room1-512-16-v1")
        self.other_project = Project.objects.create(owner=self.other, name="Other", dataset_id="tumvi-room1-512-16-v1")
        self.base = f"/api/v1/projects/{self.project.pk}/acquisitions"
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.owner)
        self.token = self.client.get("/api/v1/session").json()["csrf_token"]
        self.other_client = Client(enforce_csrf_checks=True)
        self.other_client.force_login(self.other)
        self.other_token = self.other_client.get("/api/v1/session").json()["csrf_token"]
        ComputeQueue.objects.get_or_create(pk=1)

    def send(self, method, path, data=None, *, client=None, token=None):
        return getattr(client or self.client, method)(path, data=json.dumps(data or {"schema_version": 1}),
            content_type="application/json", HTTP_X_CSRFTOKEN=token or self.token)

    def create(self, content=b"abcdefghijklmnopqrstuvwxyz", **changes):
        data = {"schema_version": 1, "name": "Private camera and IMU", "filename": "recording.tar",
                "size_bytes": len(content), "idempotency_key": str(uuid.uuid4()), **changes}
        result = self.send("post", self.base, data)
        self.assertEqual(result.status_code, 201, result.content)
        value = result.json()["acquisition"]
        return Acquisition.objects.get(pk=value["id"]), content, data

    def put(self, value, content, offset=0, **headers):
        return self.client.put(f"{self.base}/{value.pk}/content", data=content,
            content_type="application/octet-stream", HTTP_X_CSRFTOKEN=self.token,
            HTTP_UPLOAD_OFFSET=str(offset), HTTP_X_CHUNK_SHA256=sha256(content).hexdigest(), **headers)

    def upload(self, value, content):
        for offset in range(0, len(content), 16):
            result = self.put(value, content[offset:offset + 16], offset)
            self.assertEqual(result.status_code, 200, result.content)
        result = self.send("post", f"{self.base}/{value.pk}/complete")
        self.assertEqual(result.status_code, 200, result.content)
        value.refresh_from_db()
        return value

    def publication(self, value):
        """A small handoff with authentic linked contracts, independent of decoder dependencies."""
        acquisition = json.loads((EXAMPLES / "imu-snapshots.json").read_text())
        from meridian.sensor_contract import contract_sha256
        original_path = acquisition["files"][0]["path"]
        original = (EXAMPLES / original_path).read_bytes()
        # Fixture preparation uses exact integer us->ns conversion, unchanged measurements.
        lines = original.decode().splitlines()
        prepared_lines = [lines[0]]
        for line in lines[1:]:
            row = line.split(",")
            row[2] = str(int(row[2]) * 1000)
            prepared_lines.append(",".join(row))
        prepared_csv = ("\n".join(prepared_lines) + "\n").encode()
        prepared = {"format": "meridian-sensor-contract", "schema_version": 1, "kind": "prepared_input",
            "id": "hosted-fixture", "acquisition": {"id": acquisition["id"], "sha256": contract_sha256(acquisition)},
            "calibration": None, "clock_mappings": [], "method": {"name": "test-fixture", "version": "1"},
            "selections": [{"stream_id": "imu0", "segment_id": item["id"], "first_tick": item["first_tick"], "last_tick": item["last_tick"]}
                           for item in acquisition["streams"][0]["segments"]],
            "operations": [], "files": [{"path": original_path, "role": "sensor", "size_bytes": len(prepared_csv), "sha256": sha256(prepared_csv).hexdigest()}],
            "outputs": [{"stream_id": "imu0", "index_path": original_path, "frame_id": "imu", "clock_id": "imu-clock", "timestamp_unit": "ns"}],
            "target_profile": "imu_snapshot_candidate_v1"}
        report = {"execution_readiness": "not_assessed", "reference_availability": "absent"}
        contents = {"acquisition/acquisition.json": storage.canonical(acquisition),
                    "acquisition/" + original_path: original, "prepared/prepared-input.json": storage.canonical(prepared),
                    "prepared/" + original_path: prepared_csv, "report.json": storage.canonical(report)}
        staging = self.root / "test-staging"
        staging.mkdir()
        manifest = {"schema_version": 1, "files": {}}
        for name, data in contents.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            manifest["files"][name] = {"size_bytes": len(data), "sha256": sha256(data).hexdigest()}
        outcome = {"state": "ready", "descriptor": acquisition, "result": report,
                   "manifest": manifest, "sha256": sha256(storage.source_path(value.pk).read_bytes()).hexdigest()}
        return staging, outcome


class AcquisitionAPITests(AcquisitionSetup, TestCase):
    def test_creation_reserves_peak_storage_and_is_idempotent(self):
        value, content, data = self.create()
        self.assertEqual(value.reserved_bytes, len(content) + 2 * 1024**2)
        self.assertEqual(value.reserved_files, 60_000)
        self.assertEqual(storage.source_path(value.pk).read_bytes(), b"")
        repeat = self.send("post", self.base, data)
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json()["acquisition"]["id"], str(value.pk))
        self.assertEqual(self.send("post", self.base, {**data, "name": "Different"}).status_code, 409)
        self.assertEqual(Acquisition.objects.count(), 1)
        listing = self.client.get(self.base).json()
        self.assertEqual(listing["storage"]["reserved_bytes"], value.reserved_bytes)
        self.assertTrue(self.client.get("/api/v1/session").json()["capabilities"]["uploads"])

    def test_byte_and_inode_quotas_and_disabled_uploads(self):
        self.create()
        data = {"schema_version": 1, "name": "Next", "filename": "new.zip", "size_bytes": 10, "idempotency_key": str(uuid.uuid4())}
        with override_settings(MERIDIAN_ACQUISITION_ACCOUNT_BYTES=2 * 1024**2):
            self.assertEqual(self.send("post", self.base, data).json()["error"]["code"], "storage_quota")
        Acquisition.objects.update(reserved_files=120000)
        self.assertEqual(self.send("post", self.base, data).json()["error"]["code"], "storage_quota")
        with override_settings(MERIDIAN_UPLOADS_ENABLED=False):
            self.assertEqual(self.send("post", self.base, data).status_code, 503)

    def test_creation_rejects_invalid_types_filenames_and_duplicate_json(self):
        data = {"schema_version": 1, "name": "Next", "filename": "new.zip", "size_bytes": 10, "idempotency_key": str(uuid.uuid4())}
        for change in ({"size_bytes": True}, {"size_bytes": 0}, {"size_bytes": 2**40}, {"filename": "../data.zip"},
                       {"filename": "data.bin"}, {"include_reference": "false"}, {"imu_stream_id": "../imu"}, {"imu_stream_id": "0imu"}, {"schema_version": True}):
            with self.subTest(change=change):
                self.assertEqual(self.send("post", self.base, {**data, **change}).status_code, 400)
        response = self.client.post(self.base, data='{"schema_version":1,"schema_version":1}', content_type="application/json", HTTP_X_CSRFTOKEN=self.token)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Acquisition.objects.exists())

    def test_refresh_resume_chunk_replay_and_crash_tail_recovery(self):
        value, content, _ = self.create()
        first = self.put(value, content[:16])
        self.assertEqual(first.json()["acquisition"]["bytes_received"], 16)
        self.assertEqual(self.put(value, content[:16]).status_code, 200)
        self.assertEqual(value.chunks.count(), 1)
        self.assertEqual(self.put(value, b"changed bytes!!!").status_code, 409)
        with storage.source_path(value.pk).open("ab") as stream:
            stream.write(b"unacknowledged crash tail")
        refreshed = Client(enforce_csrf_checks=True)
        refreshed.cookies = deepcopy(self.client.cookies)
        data = refreshed.get(f"{self.base}/{value.pk}/chunks").json()
        self.assertEqual(data["acquisition"]["bytes_received"], 16)
        self.assertEqual(data["chunks"], [{"offset": 0, "size_bytes": 16, "sha256": sha256(content[:16]).hexdigest()}])
        self.assertEqual(self.put(value, content[16:], 16).status_code, 200)
        self.assertEqual(storage.source_path(value.pk).read_bytes(), content)
        self.assertEqual(self.send("post", f"{self.base}/{value.pk}/complete").json()["acquisition"]["state"], "validating")
        self.assertEqual(self.send("post", f"{self.base}/{value.pk}/complete").status_code, 200)

    def test_binary_request_bypasses_only_ordinary_json_body_bound(self):
        with override_settings(MERIDIAN_ACQUISITION_CHUNK_BYTES=16384):
            value, content, _ = self.create(b"x" * 16384)
            self.assertEqual(self.put(value, content).status_code, 200)
        huge = self.client.post(self.base, data=" " * 9000, content_type="application/json", HTTP_X_CSRFTOKEN=self.token)
        self.assertEqual(huge.status_code, 400)

    def test_invalid_and_out_of_order_chunks_cannot_change_committed_bytes(self):
        value, content, _ = self.create()
        for chunk, offset in ((content[:15], 0), (content[16:], 16), (content[:16], 1), (content, 0)):
            self.assertEqual(self.put(value, chunk, offset).status_code, 400)
        bad = self.client.put(f"{self.base}/{value.pk}/content", content[:16], content_type="application/octet-stream",
                             HTTP_X_CSRFTOKEN=self.token, HTTP_UPLOAD_OFFSET="0", HTTP_X_CHUNK_SHA256="0" * 64)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(storage.source_path(value.pk).read_bytes(), b"")
        self.assertEqual(self.send("post", f"{self.base}/{value.pk}/complete").status_code, 409)

    def test_all_routes_require_owner_project_binding_and_csrf(self):
        value, content, _ = self.create()
        for suffix in ("", "/chunks", "/descriptor", "/report", "/original", "/files?path=report.json"):
            self.assertEqual(self.other_client.get(f"{self.base}/{value.pk}{suffix}").status_code, 404)
            self.assertEqual(Client().get(f"{self.base}/{value.pk}{suffix}").status_code, 401)
        for action in ("complete", "cancel"):
            self.assertEqual(self.send("post", f"{self.base}/{value.pk}/{action}", client=self.other_client, token=self.other_token).status_code, 404)
        for client in (self.client, self.other_client):
            result = client.put(f"{self.base}/{value.pk}/content", content[:16], content_type="application/octet-stream")
            self.assertEqual(result.status_code, 403)
        self.assertEqual(self.send("delete", f"{self.base}/{value.pk}", client=self.other_client, token=self.other_token).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/projects/{self.other_project.pk}/acquisitions/{value.pk}").status_code, 404)

    def test_cancel_and_expiry_clean_bytes_and_release_reservations(self):
        value, content, _ = self.create()
        self.put(value, content[:16])
        self.assertEqual(self.send("delete", f"{self.base}/{value.pk}").status_code, 409)
        self.assertEqual(self.send("delete", f"/api/v1/projects/{self.project.pk}").status_code, 409)
        cancelled = self.send("post", f"{self.base}/{value.pk}/cancel")
        self.assertEqual(cancelled.json()["acquisition"]["state"], "cancelled")
        value.refresh_from_db()
        self.assertEqual(value.reserved_bytes, 0)
        self.assertFalse(storage.directory(value.pk).exists())
        self.assertEqual(self.send("post", f"{self.base}/{value.pk}/cancel").status_code, 200)
        self.assertEqual(self.send("delete", f"{self.base}/{value.pk}").status_code, 200)
        next_value, _, _ = self.create()
        Acquisition.objects.filter(pk=next_value.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(queue.cleanup_abandoned(), 1)
        next_value.refresh_from_db()
        self.assertEqual(next_value.failure["code"], "expired_upload")
        self.assertEqual(next_value.reserved_bytes, 0)

    def test_cleanup_failure_never_releases_reserved_capacity(self):
        value, _, _ = self.create()
        with patch.object(storage, "cleanup", side_effect=OSError("disk failure")):
            self.assertEqual(self.send("post", f"{self.base}/{value.pk}/cancel").status_code, 503)
        value.refresh_from_db()
        self.assertEqual(value.state, "cancelled")
        self.assertGreater(value.reserved_bytes, 0)
        queue.cleanup_abandoned()
        value.refresh_from_db()
        self.assertEqual(value.reserved_bytes, 0)

    def test_queued_cancel_is_terminal_and_active_cancel_waits_for_worker(self):
        value, content, _ = self.create()
        self.upload(value, content)
        claimed, attempt = queue.claim_import(uuid.uuid4())
        self.assertEqual(claimed.pk, value.pk)
        result = self.send("post", f"{self.base}/{value.pk}/cancel")
        self.assertEqual(result.json()["acquisition"]["state"], "validating")
        self.assertEqual(queue.heartbeat_import(value.pk, attempt, "preparing"), "cancel")
        self.assertEqual(self.send("delete", f"{self.base}/{value.pk}").status_code, 409)
        self.assertTrue(queue.finish_import(value.pk, attempt, {"state": "cancelled"}))
        value.refresh_from_db()
        self.assertEqual(value.reserved_bytes, 0)
        self.assertEqual(value.state, "cancelled")

    def test_stale_workers_cannot_renew_publish_or_release_an_unreconciled_slot(self):
        value, content, _ = self.create()
        self.upload(value, content)
        _, attempt = queue.claim_import(uuid.uuid4())
        Acquisition.objects.filter(pk=value.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(queue.heartbeat_import(value.pk, attempt, "validating"), "lost")
        self.assertFalse(queue.finish_import(value.pk, attempt, {"state": "cancelled"}))
        self.assertIsNone(queue.claim_import(uuid.uuid4()))
        self.assertEqual(queue.expired_imports(), [(value.pk, attempt)])
        self.assertTrue(queue.recover_import(value.pk, attempt))
        _, next_attempt = queue.claim_import(uuid.uuid4())
        self.assertNotEqual(next_attempt, attempt)
        self.assertFalse(queue.finish_import(value.pk, attempt, {"state": "cancelled"}))
        Acquisition.objects.filter(pk=value.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertTrue(queue.recover_import(value.pk, next_attempt))
        value.refresh_from_db()
        self.assertEqual(value.state, "rejected")
        self.assertEqual(value.reserved_bytes, 0)

    def test_ready_requires_complete_verified_publication_and_preserves_deleted_metadata(self):
        value, content, _ = self.create()
        self.upload(value, content)
        _, attempt = queue.claim_import(uuid.uuid4())
        staging, outcome = self.publication(value)
        with self.assertRaises(OSError):
            queue.finish_import(value.pk, attempt, outcome)
        self.assertTrue(storage.publish_import(value.pk, attempt, staging, outcome))
        # Files exist durably but remain private until the DB publication pointer commits.
        self.assertEqual(self.client.get(f"{self.base}/{value.pk}/original").status_code, 410)
        self.assertTrue(queue.finish_import(value.pk, attempt, outcome))
        value.refresh_from_db()
        self.assertEqual(value.state, "ready")
        self.assertEqual(value.reserved_bytes, value.retained_bytes)
        self.assertLess(value.retained_bytes, 20000)
        download = self.client.get(f"{self.base}/{value.pk}/original")
        self.assertEqual(b"".join(download.streaming_content), content)
        self.assertEqual(download["X-Content-SHA256"], sha256(content).hexdigest())
        self.assertEqual(self.client.get(f"{self.base}/{value.pk}/files?path=../../other").status_code, 400)
        self.assertEqual(self.client.get(f"{self.base}/{value.pk}/files?path=unknown").status_code, 404)
        self.assertEqual(self.send("delete", f"{self.base}/{value.pk}").status_code, 200)
        value.refresh_from_db()
        self.assertEqual(value.state, "ready")
        self.assertIsNotNone(value.descriptor)
        self.assertEqual(value.reserved_bytes, 0)
        self.assertEqual(self.client.get(f"{self.base}/{value.pk}").json()["acquisition"]["availability"], "deleted")
        self.assertEqual(self.client.get(f"{self.base}/{value.pk}/original").status_code, 410)

    def test_publication_refuses_corruption_symlinks_and_descriptor_substitution(self):
        value, content, _ = self.create()
        self.upload(value, content)
        _, attempt = queue.claim_import(uuid.uuid4())
        staging, outcome = self.publication(value)
        bad = deepcopy(outcome)
        bad["descriptor"]["id"] = "substituted"
        with self.assertRaises(ValueError):
            storage.publish_import(value.pk, attempt, staging, bad)
        report = staging / "report.json"
        original = report.read_bytes()
        report.write_bytes(original.replace(b"absent", b"present"))
        with self.assertRaises(ValueError):
            storage.publish_import(value.pk, attempt, staging, outcome)
        report.unlink()
        report.symlink_to(staging / "acquisition/acquisition.json")
        with self.assertRaises(OSError):
            storage.publish_import(value.pk, attempt, staging, outcome)
        self.assertFalse((storage.directory(value.pk) / "published" / str(attempt)).exists())

    def test_cancel_during_verified_copy_prevents_ready_publication(self):
        value, content, _ = self.create()
        self.upload(value, content)
        _, attempt = queue.claim_import(uuid.uuid4())
        staging, outcome = self.publication(value)
        self.assertTrue(storage.publish_import(value.pk, attempt, staging, outcome))
        self.send("post", f"{self.base}/{value.pk}/cancel")
        self.assertTrue(queue.finish_import(value.pk, attempt, outcome))
        value.refresh_from_db()
        self.assertEqual(value.state, "cancelled")
        self.assertFalse(value.files_available)
        self.assertFalse(storage.directory(value.pk).exists())


    def test_hosted_storage_bounds_json_escaped_path_bytes(self):
        with self.assertRaisesRegex(ValueError, "JSON-serialized"):
            storage.safe_name("acquisition/originals/" + '"' * 130)
        self.assertEqual(storage.safe_name("acquisition/" + "\U0001f680" * 50), "acquisition/" + "\U0001f680" * 50)

    def foreign_running_job(self):
        experiment = Experiment.objects.create(project=self.other_project, name="Foreign VIO", parameters={}, specification_sha256="0" * 64)
        return Job.objects.create(owner=self.other, project=self.other_project, experiment=experiment,
            experiment_name=experiment.name, revision_version=1, specification={}, specification_sha256="0" * 64,
            idempotency_key=uuid.uuid4(), state="running")

    def test_recursive_cleanup_waits_for_foreign_active_compute(self):
        value, content, _ = self.create()
        self.upload(value, content)
        _, attempt = queue.claim_import(uuid.uuid4())
        staging, outcome = self.publication(value)
        storage.publish_import(value.pk, attempt, staging, outcome)
        queue.finish_import(value.pk, attempt, outcome)
        self.foreign_running_job()
        result = self.send("delete", f"{self.base}/{value.pk}")
        self.assertEqual((result.status_code, result.json()["error"]["code"]), (409, "cleanup_busy"))
        result = self.send("delete", f"/api/v1/projects/{self.project.pk}")
        self.assertEqual((result.status_code, result.json()["error"]["code"]), (409, "cleanup_busy"))
        self.assertTrue(storage.source_path(value.pk).exists())
        value.refresh_from_db()
        self.assertTrue(value.files_available)

    def test_receiving_cancel_defers_cleanup_and_keeps_reservation_during_foreign_compute(self):
        value, content, _ = self.create()
        self.put(value, content[:16])
        job = self.foreign_running_job()
        result = self.send("post", f"{self.base}/{value.pk}/cancel").json()["acquisition"]
        self.assertEqual((result["state"], result["phase"]), ("cancelled", "cleanup_pending"))
        self.assertGreater(result["reserved_bytes"], 0)
        self.assertEqual(queue.cleanup_abandoned(), 0)
        self.assertTrue(storage.source_path(value.pk).exists())
        job.state = "failed"
        job.save()
        self.assertEqual(queue.cleanup_abandoned(), 1)
        value.refresh_from_db()
        self.assertEqual((value.reserved_bytes, value.phase), (0, "complete"))
        self.assertFalse(storage.source_path(value.pk).exists())

    def test_host_storage_floor_applies_before_admission_and_each_chunk(self):
        from types import SimpleNamespace
        floor = 10 * 1024**3
        with patch.object(storage.shutil, "disk_usage", return_value=SimpleNamespace(free=floor)):
            result = self.send("post", self.base, {"schema_version": 1, "name": "No space", "filename": "source.zip",
                "size_bytes": 16, "idempotency_key": str(uuid.uuid4())})
            self.assertEqual((result.status_code, result.json()["error"]["code"]), (409, "storage_capacity"))
        self.assertFalse(Acquisition.objects.exists())
        value, content, _ = self.create()
        with patch.object(storage.shutil, "disk_usage", return_value=SimpleNamespace(free=floor + 15)):
            self.assertEqual(self.put(value, content[:16]).status_code, 503)
        value.refresh_from_db()
        self.assertEqual(value.bytes_received, 0)
        self.assertEqual(storage.source_path(value.pk).read_bytes(), b"")

    def test_idle_cleanup_recovers_orphan_creation_without_touching_valid_uploads(self):
        value, _, _ = self.create()
        orphan = uuid.uuid4()
        storage.provision(orphan)
        self.assertEqual(queue.cleanup_abandoned(), 1)
        self.assertFalse(storage.directory(orphan).exists())
        self.assertTrue(storage.directory(value.pk).exists())


@skipUnless(connection.vendor == "postgresql", "Requires independent PostgreSQL row locks")
class PostgreSQLAcquisitionConcurrencyTests(AcquisitionSetup, ConcurrentDatabaseWorkers, TransactionTestCase):
    def test_two_uploads_cannot_reserve_the_same_last_global_capacity(self):
        cookies = deepcopy(self.client.cookies)
        reservation = 10 + 2 * 1024**2
        def create(index):
            client = Client(enforce_csrf_checks=True)
            client.cookies = deepcopy(cookies)
            return client.post(self.base, json.dumps({"schema_version": 1, "name": f"Input {index}", "filename": "source.tar",
                "size_bytes": 10, "idempotency_key": str(uuid.uuid4())}), content_type="application/json", HTTP_X_CSRFTOKEN=self.token)
        with override_settings(MERIDIAN_ACQUISITION_GLOBAL_BYTES=reservation):
            with self.workers(create) as (futures, pids, start):
                with transaction.atomic():
                    queue_lock()
                    start.wait(timeout=5)
                    self.wait_for_both_database_locks(pids, futures)
                results = [future.result(timeout=12) for future in futures]
        self.assertEqual(sorted(item.status_code for item in results), [201, 409])
        self.assertEqual(Acquisition.objects.count(), 1)
        self.assertEqual(queue.usage()["reserved_bytes"], reservation)

    def test_concurrent_retries_append_one_chunk_once(self):
        value, content, _ = self.create()
        cookies = deepcopy(self.client.cookies)
        def upload(_index):
            client = Client(enforce_csrf_checks=True)
            client.cookies = deepcopy(cookies)
            return client.put(f"{self.base}/{value.pk}/content", content[:16], content_type="application/octet-stream",
                HTTP_X_CSRFTOKEN=self.token, HTTP_UPLOAD_OFFSET="0", HTTP_X_CHUNK_SHA256=sha256(content[:16]).hexdigest())
        with self.workers(upload) as (futures, pids, start):
            with transaction.atomic():
                queue_lock()
                start.wait(timeout=5)
                self.wait_for_both_database_locks(pids, futures)
            results = [future.result(timeout=12) for future in futures]
        self.assertEqual([item.status_code for item in results], [200, 200])
        self.assertEqual(storage.source_path(value.pk).read_bytes(), content[:16])
        self.assertEqual(UploadChunk.objects.count(), 1)
        value.refresh_from_db()
        self.assertEqual(value.bytes_received, 16)

    def test_two_worker_claims_acquire_one_shared_slot(self):
        value, content, _ = self.create()
        self.upload(value, content)
        second, second_content, _ = self.create()
        self.upload(second, second_content)
        with self.workers(lambda _: queue.claim_import(uuid.uuid4())) as (futures, pids, start):
            with transaction.atomic():
                queue_lock()
                start.wait(timeout=5)
                self.wait_for_both_database_locks(pids, futures)
            results = [future.result(timeout=12) for future in futures]
        self.assertEqual(sum(item is not None for item in results), 1)
        with override_settings(MERIDIAN_EXECUTION_ENABLED=True):
            self.assertIsNone(claim_vio(uuid.uuid4()))

    def test_vio_and_import_claims_share_one_compute_slot(self):
        value, content, _ = self.create()
        self.upload(value, content)
        experiment = Experiment.objects.create(project=self.project, name="Existing VIO", parameters={}, specification_sha256="0" * 64)
        Job.objects.create(owner=self.owner, project=self.project, experiment=experiment, experiment_name=experiment.name,
                           revision_version=1, specification={}, specification_sha256="0" * 64, idempotency_key=uuid.uuid4())
        def claim(index):
            return queue.claim_import(uuid.uuid4()) if index else claim_vio(uuid.uuid4())
        with override_settings(MERIDIAN_EXECUTION_ENABLED=True):
            with self.workers(claim) as (futures, pids, start):
                with transaction.atomic():
                    queue_lock()
                    start.wait(timeout=5)
                    self.wait_for_both_database_locks(pids, futures)
                results = [future.result(timeout=12) for future in futures]
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(Job.objects.filter(state="running").count()
                         + Acquisition.objects.filter(current_attempt__isnull=False).count(), 1)

    def test_project_delete_races_upload_creation_without_orphan_files_or_reservations(self):
        cookies = deepcopy(self.client.cookies)
        def change(index):
            client = Client(enforce_csrf_checks=True)
            client.cookies = deepcopy(cookies)
            if index:
                return client.delete(f"/api/v1/projects/{self.project.pk}", HTTP_X_CSRFTOKEN=self.token)
            return client.post(self.base, json.dumps({"schema_version": 1, "name": "New", "filename": "source.tar", "size_bytes": 10,
                "idempotency_key": str(uuid.uuid4())}), content_type="application/json", HTTP_X_CSRFTOKEN=self.token)
        with self.workers(change) as (futures, pids, start):
            with transaction.atomic():
                queue_lock()
                start.wait(timeout=5)
                self.wait_for_both_database_locks(pids, futures)
            created, deleted = [future.result(timeout=12) for future in futures]
        self.assertIn((created.status_code, deleted.status_code), {(201, 409), (404, 200)})
        self.assertEqual(len(list(self.root.iterdir())), Acquisition.objects.count())
        self.assertEqual(Project.objects.filter(pk=self.project.pk).exists(), Acquisition.objects.exists())

    def test_cancellation_serializes_with_chunk_append_before_releasing_storage(self):
        value, content, _ = self.create()
        cookies = deepcopy(self.client.cookies)
        def change(index):
            client = Client(enforce_csrf_checks=True)
            client.cookies = deepcopy(cookies)
            if index:
                return client.post(f"{self.base}/{value.pk}/cancel", json.dumps({"schema_version": 1}),
                    content_type="application/json", HTTP_X_CSRFTOKEN=self.token)
            return client.put(f"{self.base}/{value.pk}/content", content[:16], content_type="application/octet-stream",
                HTTP_X_CSRFTOKEN=self.token, HTTP_UPLOAD_OFFSET="0", HTTP_X_CHUNK_SHA256=sha256(content[:16]).hexdigest())
        with self.workers(change) as (futures, pids, start):
            with transaction.atomic():
                queue_lock()
                start.wait(timeout=5)
                self.wait_for_both_database_locks(pids, futures)
            uploaded, cancelled = [future.result(timeout=12) for future in futures]
        self.assertIn(uploaded.status_code, {200, 409})
        self.assertEqual(cancelled.status_code, 200)
        value.refresh_from_db()
        self.assertEqual((value.state, value.reserved_bytes), ("cancelled", 0))
        self.assertFalse(storage.directory(value.pk).exists())
