"""Private immutable selections, fenced publication and retained dependency evidence."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import Client, TestCase, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from meridian.sensor_contract import contract_sha256
from . import acquisition_queue, acquisition_storage
from . import input_revision_queue as queue, input_revision_storage as storage
from . import input_revision_worker as worker
from .job_queue import claim as claim_vio, queue_lock
from .models import Acquisition, Experiment, Job, InputRevision, SensorArtifact
from .test_acquisitions import AcquisitionSetup, EXAMPLES
from .test_postgres import ConcurrentDatabaseWorkers


def identity(document):
    return {"id": document["id"], "sha256": contract_sha256(document)}


class RevisionSetup(AcquisitionSetup):
    def setUp(self):
        super().setUp()
        self.source = self.ready_source()
        self.artifacts = f"/api/v1/projects/{self.project.pk}/sensor-artifacts"
        self.revisions = f"{self.base}/{self.source.pk}/input-revisions"

    def ready_source(self, descriptor=None, *, owner=None, project=None):
        descriptor = descriptor or json.loads((EXAMPLES / "stereo-no-reference.json").read_text())
        value = Acquisition.objects.create(owner=owner or self.owner, project=project or self.project,
            name="Published source", filename="source.tar", size_bytes=7, bytes_received=7,
            sha256=sha256(b"archive").hexdigest(), idempotency_key=uuid.uuid4(), request_sha256="a" * 64,
            options={}, budget={"chunk_bytes": 16}, state="ready", phase="complete", descriptor=descriptor,
            result={}, files_available=True, published_attempt=uuid.uuid4(), expires_at=timezone.now())
        parent = acquisition_storage.directory(value.pk)
        publication = parent / "published" / str(value.published_attempt)
        publication.mkdir(parents=True)
        (parent / "original").write_bytes(b"archive")
        contents = {"acquisition/acquisition.json": acquisition_storage.canonical(descriptor)}
        contents.update({"acquisition/" + entry["path"]: (EXAMPLES / entry["path"]).read_bytes() for entry in descriptor["files"]})
        for name, content in contents.items():
            target = publication / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        value.manifest = {"schema_version": 1, "files": {name: {"size_bytes": len(data), "sha256": sha256(data).hexdigest()}
                                                          for name, data in contents.items()}}
        marker = acquisition_storage.canonical(value.manifest) + b"\n"
        (publication / "publication-manifest.json").write_bytes(marker)
        value.reserved_bytes = value.retained_bytes = 7 + len(marker) + sum(map(len, contents.values()))
        value.reserved_files = value.retained_files = len(contents) + 2
        value.save()
        return value

    def import_artifact(self, document=None, *, bindings=None, **changes):
        document = document or json.loads((EXAMPLES / "calibration.json").read_text())
        body = {"schema_version": 1, "name": "Supplied fixture", "idempotency_key": str(uuid.uuid4()),
                "descriptor_text": json.dumps(document, indent=2) + "\n",
                "acquisition_ids": bindings or [str(self.source.pk)], **changes}
        response = self.send("post", self.artifacts, body)
        self.assertEqual(response.status_code, 201, response.content)
        return SensorArtifact.objects.get(pk=response.json()["artifact"]["id"]), body

    def create_revision(self, artifact=None, **changes):
        artifact = artifact or self.import_artifact()[0]
        body = {"schema_version": 1, "name": "Selected supplied calibration", "idempotency_key": str(uuid.uuid4()),
                "calibration_id": str(artifact.pk), "clock_mapping_ids": [], "apply_imu_intrinsics": False,
                "imu_target_frame": None, "imu_stream_id": None, "include_reference": True, **changes}
        response = self.send("post", self.revisions, body)
        self.assertEqual(response.status_code, 201, response.content)
        return InputRevision.objects.get(pk=response.json()["revision"]["id"]), body

    def publication(self, value):
        """Independent unchanged-observation fixture for a calibration-selection-only revision."""
        self.assertFalse(value.plan["operations"])
        source = value.acquisition.descriptor
        prepared = {"format": "meridian-sensor-contract", "schema_version": 1, "kind": "prepared_input",
            "id": value.request["prepared_id"], "acquisition": identity(source),
            "calibration": identity(json.loads(value.request["calibration"])), "clock_mappings": [],
            "method": value.plan["method"], "selections": [
                {"stream_id": stream["id"], "segment_id": segment["id"], "first_tick": segment["first_tick"], "last_tick": segment["last_tick"]}
                for stream in source["streams"] for segment in stream["segments"]],
            "operations": [], "outputs": value.plan["outputs"], "target_profile": "stereo_imu_candidate_v1", "files": []}
        contents = {item["path"]: (EXAMPLES / item["path"]).read_bytes() for item in source["files"]}
        contents["evidence/calibration.json"] = value.request["calibration"].encode()
        for name, data in contents.items():
            prepared["files"].append({"path": name, "role": "evidence" if name.startswith("evidence/") else "sensor",
                                      "sha256": sha256(data).hexdigest(), "size_bytes": len(data)})
        report = {"status": "accepted", "acquisition": identity(source), "prepared_input": identity(prepared),
            "method": value.plan["method"], "execution_readiness": "not_assessed", "calibration_validity": "not_assessed",
            "application": {"request_sha256": value.plan["request_sha256"], "application_status": "completed"}}
        contents.update({"prepared-input.json": acquisition_storage.canonical(prepared),
                         "source-acquisition.json": acquisition_storage.canonical(source),
                         "preparation-report.json": acquisition_storage.canonical(report)})
        staging = self.root / ("staging-" + str(uuid.uuid4()))
        staging.mkdir()
        for name, content in contents.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        outcome = {"state": "ready", "prepared_input": prepared, "result": report,
                   "manifest": {"schema_version": 1, "files": {name: {"size_bytes": len(data), "sha256": sha256(data).hexdigest()}
                                                                  for name, data in contents.items()}}}
        return staging, outcome

    def finish(self, value):
        claimed, attempt = queue.claim_revision(uuid.uuid4())
        self.assertEqual(claimed.pk, value.pk)
        staging, outcome = self.publication(claimed)
        storage.publish(claimed, attempt, staging, outcome, checkpoint=lambda *_: None)
        self.assertTrue(queue.finish_revision(value.pk, attempt, outcome))
        value.refresh_from_db()
        return value, outcome


class SensorArtifactTests(RevisionSetup, TestCase):
    def test_raw_identity_canonical_identity_and_import_are_distinct_from_application(self):
        artifact, body = self.import_artifact()
        self.assertEqual(artifact.descriptor_text, body["descriptor_text"])
        self.assertEqual(artifact.contract_sha256, contract_sha256(artifact.contract))
        download = self.client.get(f"{self.artifacts}/{artifact.pk}/contract")
        self.assertEqual(download.content, body["descriptor_text"].encode())
        self.assertEqual(download["X-Content-SHA256"], artifact.descriptor_sha256)
        self.assertIn("no-store", download["Cache-Control"])
        self.assertFalse(InputRevision.objects.exists())
        self.assertEqual(self.send("post", self.artifacts, body).status_code, 200)
        self.assertEqual(self.send("post", self.artifacts, {**body, "name": "Changed"}).status_code, 409)

    def test_owner_project_binding_and_csrf_precede_descriptor_access(self):
        artifact, body = self.import_artifact()
        for suffix in ("", "/contract"):
            path = f"{self.artifacts}/{artifact.pk}{suffix}"
            self.assertEqual(Client().get(path).status_code, 401)
            self.assertEqual(self.other_client.get(path).status_code, 404)
            self.assertEqual(self.client.get(path.replace(str(self.project.pk), str(self.other_project.pk))).status_code, 404)
        self.assertEqual(self.client.post(self.artifacts, data=json.dumps(body), content_type="application/json").status_code, 403)
        foreign = self.ready_source(owner=self.other, project=self.other_project)
        self.assertEqual(self.send("post", self.artifacts, {**body, "idempotency_key": str(uuid.uuid4()),
            "acquisition_ids": [str(foreign.pk)]}).status_code, 404)

    def test_invalid_raw_json_types_contracts_and_bounds_leave_no_artifact(self):
        document = json.loads((EXAMPLES / "calibration.json").read_text())
        body = {"schema_version": 1, "name": "Calibration", "idempotency_key": str(uuid.uuid4()),
                "descriptor_text": json.dumps(document), "acquisition_ids": [str(self.source.pk)]}
        for change in ({"schema_version": True}, {"descriptor_text": {}}, {"descriptor_text": " " * 65537},
                       {"descriptor_text": '{"kind":"calibration","kind":"clock_mapping"}'},
                       {"descriptor_text": json.dumps(document).replace('4.0', 'NaN', 1)},
                       {"acquisition_ids": []}, {"acquisition_ids": [str(self.source.pk)] * 2}):
            with self.subTest(change=tuple(change)):
                self.assertEqual(self.send("post", self.artifacts, {**body, **change}).status_code, 400)
        text = json.dumps(body).encode("utf-16")
        self.assertEqual(self.client.post(self.artifacts, data=text, content_type="application/json", HTTP_X_CSRFTOKEN=self.token).status_code, 400)
        self.assertFalse(SensorArtifact.objects.exists())

    def test_artifact_identity_and_bound_acquisition_hash_mismatch_are_rejected(self):
        document = json.loads((EXAMPLES / "calibration.json").read_text())
        document["acquisition"]["sha256"] = "f" * 64
        body = {"schema_version": 1, "name": "Bad", "idempotency_key": str(uuid.uuid4()),
                "descriptor_text": json.dumps(document), "acquisition_ids": [str(self.source.pk)]}
        self.assertEqual(self.send("post", self.artifacts, body).status_code, 400)
        artifact, _ = self.import_artifact()
        artifact.name = "Mutation"
        with self.assertRaises(ValidationError):
            artifact.save()
        changed = deepcopy(artifact.contract)
        changed["cameras"][0]["intrinsics"][0] = 4  # Numerically equal, canonically different.
        SensorArtifact.objects.filter(pk=artifact.pk).update(contract=changed)
        self.assertEqual(self.send("post", self.revisions + "/assess", {"schema_version": 1,
            "calibration_id": str(artifact.pk)}).status_code, 400)

    def test_reported_and_estimated_are_uploaded_claims(self):
        document = json.loads((EXAMPLES / "calibration.json").read_text())
        document["state"] = "estimated"
        validation = deepcopy(self.source.descriptor)
        validation["id"] = "independent-validation"
        other = self.ready_source(validation)
        document["validation"] = {"status": "reported", "acquisitions": [identity(validation)],
            "report": {"path": "evidence/report.json", "sha256": "a" * 64, "size_bytes": 123}}
        artifact, _ = self.import_artifact(document, bindings=[str(self.source.pk), str(other.pk)])
        evidence = self.client.get(f"{self.artifacts}/{artifact.pk}").json()["artifact"]["evidence"]
        self.assertEqual(evidence["origin"], "uploaded")
        self.assertEqual(evidence["physical_validity"], "not_assessed")
        self.assertEqual(evidence["report_payload_verification"], "not_performed")


class InputRevisionAPITests(RevisionSetup, TestCase):
    def test_assessment_is_payload_free_and_creation_freezes_selection(self):
        artifact, _ = self.import_artifact()
        before = deepcopy(self.source.descriptor)
        result = self.send("post", self.revisions + "/assess", {"schema_version": 1, "calibration_id": str(artifact.pk)})
        self.assertEqual(result.status_code, 200, result.content)
        self.assertTrue(result.json()["eligible"])
        self.assertEqual(result.json()["application_status"], "planned")
        self.assertEqual(result.json()["plan"]["operations"], [])
        self.assertFalse(InputRevision.objects.exists())
        revision, body = self.create_revision(artifact)
        self.assertEqual(revision.state, "queued")
        self.assertEqual(revision.reserved_bytes, 1024**2)
        self.assertEqual(revision.request["calibration"], artifact.descriptor_text)
        self.assertEqual(revision.source_identity, identity(before))
        self.source.refresh_from_db()
        self.assertEqual(self.source.descriptor, before)
        self.assertEqual(self.send("post", self.revisions, body).status_code, 200)
        self.assertEqual(self.send("post", self.revisions, {**body, "include_reference": False}).status_code, 409)

    def test_archiving_preserves_queued_request_and_idempotent_submission(self):
        artifact, _ = self.import_artifact()
        revision, body = self.create_revision(artifact)
        self.assertEqual(self.send("delete", f"{self.artifacts}/{artifact.pk}").status_code, 200)
        self.assertEqual(self.send("post", self.revisions, body).status_code, 200)
        self.assertEqual(self.send("post", self.revisions + "/assess", {"schema_version": 1,
            "calibration_id": str(artifact.pk)}).status_code, 404)
        self.assertTrue(queue.claim_revision(uuid.uuid4()))
        revision.refresh_from_db()
        self.assertEqual(json.loads(revision.request["calibration"]), artifact.contract)

    def test_owner_and_project_binding_on_all_revision_reads_and_mutations(self):
        value, _ = self.create_revision()
        root = f"{self.revisions}/{value.pk}"
        for suffix in ("", "/contract", "/report", "/files?path=prepared-input.json"):
            self.assertEqual(Client().get(root + suffix).status_code, 401)
            self.assertEqual(self.other_client.get(root + suffix).status_code, 404)
            self.assertEqual(self.client.get((root + suffix).replace(str(self.project.pk), str(self.other_project.pk))).status_code, 404)
        for method, suffix in (("post", "/cancel"), ("delete", "/files")):
            self.assertEqual(self.send(method, root + suffix, client=self.other_client, token=self.other_token).status_code, 404)
        self.assertEqual(self.client.post(root + "/cancel", data='{"schema_version":1}', content_type="application/json").status_code, 403)

    def test_selection_errors_do_not_reserve_storage_or_start_work(self):
        artifact, _ = self.import_artifact()
        base = {"schema_version": 1, "calibration_id": str(artifact.pk)}
        for change in ({"apply_imu_intrinsics": True}, {"imu_target_frame": "cam1"}, {"include_reference": "yes"},
                       {"imu_stream_id": "missing"}, {"clock_mapping_ids": [str(artifact.pk)]},
                       {"clock_mapping_ids": [str(uuid.uuid4())] * 17}, {"runtime_yaml": "untrusted"}):
            self.assertEqual(self.send("post", self.revisions + "/assess", {**base, **change}).status_code, 400)
        self.assertFalse(InputRevision.objects.exists())
        self.assertFalse(acquisition_queue.compute_active())

    def test_source_deletion_and_project_deletion_block_queued_dependencies(self):
        value, _ = self.create_revision()
        self.assertEqual(self.send("delete", f"{self.base}/{self.source.pk}").json()["error"]["code"], "dependent_preparation")
        self.assertEqual(self.send("delete", f"/api/v1/projects/{self.project.pk}").json()["error"]["code"], "active_preparations")
        self.assertEqual(self.send("post", f"{self.revisions}/{value.pk}/cancel").status_code, 200)
        self.assertEqual(self.send("delete", f"{self.base}/{self.source.pk}").status_code, 200)
        self.source.refresh_from_db()
        self.assertFalse(self.source.files_available)
        self.assertIsNotNone(self.source.descriptor)

    def test_ready_revision_is_self_contained_after_source_deletion(self):
        value, _ = self.create_revision()
        value, outcome = self.finish(value)
        self.assertEqual(self.send("delete", f"{self.base}/{self.source.pk}").status_code, 200)
        root = f"{self.revisions}/{value.pk}"
        for name, expected in outcome["manifest"]["files"].items():
            response = self.client.get(root + "/files", {"path": name})
            self.assertEqual(response.status_code, 200, name)
            content = b"".join(response.streaming_content)
            self.assertEqual(sha256(content).hexdigest(), expected["sha256"])
        self.assertEqual(self.client.get(root + "/contract").json()["prepared_input"], outcome["prepared_input"])
        self.assertEqual(self.client.get(root + "/files", {"path": "../original"}).status_code, 400)
        self.assertEqual(self.client.get(root + "/files", {"path": "original"}).status_code, 404)
        self.assertEqual(self.send("delete", root + "/files").status_code, 200)
        value.refresh_from_db()
        self.assertFalse(value.files_available)
        self.assertEqual(value.reserved_bytes, 0)
        self.assertEqual(value.prepared_input, outcome["prepared_input"])
        self.assertEqual(self.client.get(root + "/files", {"path": "prepared-input.json"}).status_code, 410)

    def test_shared_storage_quota_counts_input_revisions_for_both_admissions(self):
        value, _ = self.create_revision()
        with override_settings(MERIDIAN_ACQUISITION_ACCOUNT_BYTES=value.reserved_bytes + self.source.reserved_bytes):
            artifact, _ = self.import_artifact()
            rejected = self.send("post", self.revisions, {"schema_version": 1, "name": "Next", "idempotency_key": str(uuid.uuid4()),
                                                       "calibration_id": str(artifact.pk)})
            self.assertEqual(rejected.json()["error"]["code"], "storage_quota")
            rejected = self.send("post", self.base, {"schema_version": 1, "name": "Archive", "filename": "a.tar",
                "size_bytes": 1, "idempotency_key": str(uuid.uuid4())})
            self.assertEqual(rejected.json()["error"]["code"], "storage_quota")
        InputRevision.objects.filter(pk=value.pk).update(reserved_files=120000)
        self.assertGreater(acquisition_queue.usage(self.owner)["reserved_files"], 120000)

    def test_missing_or_linked_revision_file_fails_closed(self):
        value, _ = self.create_revision()
        value, _ = self.finish(value)
        path = storage.directory(value.pk) / "published" / str(value.published_attempt) / "prepared-input.json"
        path.unlink()
        url = f"{self.revisions}/{value.pk}/files?path=prepared-input.json"
        self.assertEqual(self.client.get(url).status_code, 503)
        path.symlink_to(self.root / "secret")
        (self.root / "secret").write_text("secret")
        self.assertEqual(self.client.get(url).status_code, 503)

    def test_project_deletion_removes_revision_files_and_archived_artifacts(self):
        artifact, _ = self.import_artifact()
        value, _ = self.create_revision(artifact)
        self.finish(value)
        self.send("delete", f"{self.artifacts}/{artifact.pk}")
        self.assertEqual(self.send("delete", f"/api/v1/projects/{self.project.pk}").status_code, 200)
        self.assertFalse(InputRevision.objects.filter(pk=value.pk).exists())
        self.assertFalse(SensorArtifact.objects.filter(pk=artifact.pk).exists())
        self.assertFalse(storage.directory(value.pk).exists())
        self.assertFalse(acquisition_storage.directory(self.source.pk).exists())


class InputRevisionLifecycleTests(RevisionSetup, TestCase):
    def test_active_revision_blocks_import_and_vio_and_fences_late_finish(self):
        value, _ = self.create_revision()
        claimed, attempt = queue.claim_revision(uuid.uuid4())
        self.assertTrue(acquisition_queue.compute_active())
        self.assertIsNone(acquisition_queue.claim_import(uuid.uuid4()))
        with override_settings(MERIDIAN_EXECUTION_ENABLED=True):
            self.assertIsNone(claim_vio(uuid.uuid4()))
        self.assertEqual(queue.heartbeat_revision(value.pk, uuid.uuid4(), "preparing"), "lost")
        self.assertFalse(queue.finish_revision(value.pk, uuid.uuid4(), {"state": "ready"}))
        InputRevision.objects.filter(pk=value.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(queue.heartbeat_revision(value.pk, attempt, "publishing"), "lost")
        self.assertFalse(queue.finish_revision(value.pk, attempt, {"state": "cancelled"}))
        self.assertTrue(acquisition_queue.compute_active())
        self.assertTrue(queue.recover_revision(value.pk, attempt))
        value.refresh_from_db()
        self.assertEqual(value.state, "queued")
        self.assertGreater(value.reserved_bytes, 0)

    def test_retry_is_bounded_and_active_cancel_waits_for_confirmed_cleanup(self):
        value, _ = self.create_revision()
        for number in (1, 2):
            _, attempt = queue.claim_revision(uuid.uuid4())
            InputRevision.objects.filter(pk=value.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
            self.assertTrue(queue.recover_revision(value.pk, attempt))
        value.refresh_from_db()
        self.assertEqual(value.state, "rejected")
        self.assertEqual(value.attempt_count, 2)
        self.assertEqual(value.reserved_bytes, 0)
        other, _ = self.create_revision()
        _, attempt = queue.claim_revision(uuid.uuid4())
        self.send("post", f"{self.revisions}/{other.pk}/cancel")
        other.refresh_from_db()
        self.assertEqual(other.state, "preparing")
        self.assertGreater(other.reserved_bytes, 0)
        self.assertEqual(queue.heartbeat_revision(other.pk, attempt, "preparing"), "cancel")
        self.assertTrue(queue.finish_revision(other.pk, attempt, {"state": "cancelled"}))

    def test_pending_cancel_defers_cleanup_and_source_deletion_does_not_release_active_quota(self):
        first, _ = self.create_revision()
        queue.claim_revision(uuid.uuid4())
        second, _ = self.create_revision()
        self.send("post", f"{self.revisions}/{second.pk}/cancel")
        second.refresh_from_db()
        self.assertEqual(second.phase, "cleanup_pending")
        self.assertGreater(second.reserved_bytes, 0)
        queue.cleanup_abandoned()
        second.refresh_from_db()
        self.assertGreater(second.reserved_bytes, 0)
        first.refresh_from_db()
        queue.finish_revision(first.pk, first.current_attempt, {"state": "cancelled"})
        queue.cleanup_abandoned()
        second.refresh_from_db()
        self.assertEqual(second.reserved_bytes, 0)

    def test_publication_rejects_missing_calibration_in_selection_only_result(self):
        value, _ = self.create_revision()
        value, attempt = queue.claim_revision(uuid.uuid4())
        staging, outcome = self.publication(value)
        outcome["prepared_input"]["calibration"] = None
        with self.assertRaises(ValueError):
            storage.publish(value, attempt, staging, outcome, checkpoint=lambda *_: None)
        self.assertFalse((storage.directory(value.pk) / "published" / str(attempt)).exists())

    def test_publication_checks_inventory_hashes_reports_and_canonical_numeric_identity(self):
        value, _ = self.create_revision()
        value, attempt = queue.claim_revision(uuid.uuid4())
        for mutation in ("report", "payload", "float", "duplicate", "orphan"):
            staging, outcome = self.publication(value)
            if mutation == "report":
                outcome["result"]["application"]["request_sha256"] = "a" * 64
            elif mutation == "payload":
                (staging / "sensors/imu0/data.csv").write_bytes(b"changed")
            elif mutation == "float":
                path = staging / "evidence/calibration.json"
                path.write_text(path.read_text().replace("4.0", "4", 1))
            elif mutation == "duplicate":
                path = staging / "prepared-input.json"
                path.write_text(path.read_text().replace('{', '{"schema_version":1,', 1))
            else:
                outcome["manifest"]["files"]["orphan.txt"] = {"size_bytes": 0, "sha256": sha256(b"").hexdigest()}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                storage.publish(value, attempt, staging, outcome, checkpoint=lambda *_: None)
            self.assertFalse((storage.directory(value.pk) / "published" / str(attempt)).exists())

    def test_atomic_publication_survives_source_immutability_and_separate_versions(self):
        artifact, _ = self.import_artifact()
        original = acquisition_storage.directory(self.source.pk) / "published" / str(self.source.published_attempt)
        before = {p.relative_to(original): p.read_bytes() for p in original.rglob("*") if p.is_file()}
        first, _ = self.create_revision(artifact)
        first, _ = self.finish(first)
        first_files = {p.relative_to(storage.directory(first.pk)): p.read_bytes()
                       for p in storage.directory(first.pk).rglob("*") if p.is_file()}
        changed = deepcopy(artifact.contract)
        changed["cameras"][0]["intrinsics"][0] += .1
        second_artifact, _ = self.import_artifact(changed)
        second, _ = self.create_revision(second_artifact)
        self.finish(second)
        self.assertNotEqual(first.request["prepared_id"], second.request["prepared_id"])
        self.assertEqual(before, {p.relative_to(original): p.read_bytes() for p in original.rglob("*") if p.is_file()})
        self.assertEqual(first_files, {p.relative_to(storage.directory(first.pk)): p.read_bytes()
                                     for p in storage.directory(first.pk).rglob("*") if p.is_file()})

    def test_revision_gc_preserves_valid_and_active_revisions(self):
        value, _ = self.create_revision()
        storage.root(create=True)
        orphan = uuid.uuid4()
        storage.directory(orphan).mkdir()
        queue.claim_revision(uuid.uuid4())
        queue.cleanup_abandoned()
        self.assertTrue(storage.directory(orphan).exists())
        value.refresh_from_db()
        queue.finish_revision(value.pk, value.current_attempt, {"state": "cancelled"})
        queue.cleanup_abandoned()
        self.assertFalse(storage.directory(orphan).exists())
        self.assertTrue(InputRevision.objects.filter(pk=value.pk).exists())


class RevisionWorkerTests(SimpleTestCase):
    def test_publish_waits_for_confirmed_child_cleanup(self):
        import tempfile
        value, attempt = SimpleNamespace(pk=uuid.uuid4(), request={}), uuid.uuid4()
        events = []
        with tempfile.TemporaryDirectory() as temp, override_settings(MERIDIAN_IMPORT_SCRATCH_ROOT=Path(temp)), \
             patch.object(worker.queue, "claim_revision", return_value=(value, attempt)), \
             patch.object(worker, "LeaseGuard"), patch.object(worker.storage, "source_path", return_value=Path(temp)), \
             patch.object(worker, "execute_revision", side_effect=lambda *a, **k: events.append("execute") or {}), \
             patch.object(worker, "cleanup_revision", side_effect=lambda *a: events.append("stopped") or True), \
             patch.object(worker.storage, "publish", side_effect=lambda *a, **k: events.append("published")), \
             patch.object(worker.queue, "finish_revision", side_effect=lambda *a: events.append("finished")):
            self.assertTrue(worker.run_one(uuid.uuid4(), threading.Event()))
        self.assertEqual(events, ["execute", "stopped", "published", "finished"])

    def test_failed_cleanup_retains_attempt_and_scratch(self):
        import tempfile
        value, attempt = SimpleNamespace(pk=uuid.uuid4(), request={}), uuid.uuid4()
        with tempfile.TemporaryDirectory() as temp, override_settings(MERIDIAN_IMPORT_SCRATCH_ROOT=Path(temp)), \
             patch.object(worker.queue, "claim_revision", return_value=(value, attempt)), \
             patch.object(worker, "LeaseGuard"), patch.object(worker.storage, "source_path", return_value=Path(temp)), \
             patch.object(worker, "execute_revision", return_value={}), patch.object(worker, "cleanup_revision", return_value=False), \
             patch.object(worker.storage, "publish") as publish, patch.object(worker.queue, "finish_revision") as finish:
            worker.run_one(uuid.uuid4(), threading.Event())
            publish.assert_not_called()
            finish.assert_not_called()
            self.assertTrue((Path(temp) / str(attempt)).is_dir())


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL row locks")
class RevisionConcurrencyTests(RevisionSetup, ConcurrentDatabaseWorkers, TransactionTestCase):
    def race(self, operation):
        with self.workers(operation) as (futures, pids, start):
            with transaction.atomic():
                queue_lock()
                start.wait(timeout=5)
                self.wait_for_both_database_locks(pids, futures)
            return [future.result(timeout=12) for future in futures]

    def test_upload_and_revision_cannot_reserve_same_last_global_capacity(self):
        artifact, _ = self.import_artifact()
        cookies = deepcopy(self.client.cookies)
        def create(index):
            client = Client(enforce_csrf_checks=True)
            client.cookies = deepcopy(cookies)
            body = {"schema_version": 1, "name": "Race", "idempotency_key": str(uuid.uuid4())}
            body.update({"calibration_id": str(artifact.pk)} if index else {"filename": "archive.tar", "size_bytes": 10})
            return self.send("post", self.revisions if index else self.base, body, client=client)
        with override_settings(MERIDIAN_ACQUISITION_GLOBAL_BYTES=self.source.reserved_bytes + 2 * 1024**2 + 10):
            result = self.race(create)
        self.assertEqual(sorted(item.status_code for item in result), [201, 409])
        self.assertEqual(InputRevision.objects.count() + Acquisition.objects.count(), 2)

    def test_vio_and_revision_claims_acquire_one_compute_slot(self):
        self.create_revision()
        experiment = Experiment.objects.create(project=self.project, name="Existing VIO", parameters={}, specification_sha256="a" * 64)
        Job.objects.create(owner=self.owner, project=self.project, experiment=experiment, experiment_name=experiment.name,
            revision_version=1, specification={}, specification_sha256="a" * 64, idempotency_key=uuid.uuid4())
        with override_settings(MERIDIAN_EXECUTION_ENABLED=True):
            result = self.race(lambda index: queue.claim_revision(uuid.uuid4()) if index else claim_vio(uuid.uuid4()))
        self.assertEqual(sum(item is not None for item in result), 1)
        self.assertTrue(acquisition_queue.compute_active())

    def test_source_deletion_and_revision_submission_serialize_dependency(self):
        artifact, _ = self.import_artifact()
        cookies = deepcopy(self.client.cookies)
        def operation(index):
            client = Client(enforce_csrf_checks=True)
            client.cookies = deepcopy(cookies)
            if index:
                return self.send("post", self.revisions, {"schema_version": 1, "name": "Race", "idempotency_key": str(uuid.uuid4()),
                    "calibration_id": str(artifact.pk)}, client=client)
            return self.send("delete", f"{self.base}/{self.source.pk}", client=client)
        result = self.race(operation)
        self.source.refresh_from_db()
        if InputRevision.objects.exists():
            self.assertEqual([item.status_code for item in result], [409, 201])
            self.assertTrue(self.source.files_available)
        else:
            self.assertEqual([item.status_code for item in result], [200, 409])
            self.assertFalse(self.source.files_available)
