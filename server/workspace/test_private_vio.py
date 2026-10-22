"""Owned private-input snapshots, admission and retained dependency boundaries."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import patch
import uuid

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from meridian.sensor_contract import contract_sha256
from meridian import vio_private_specification as private_profile
from meridian import vio_investigation_metrics
from meridian.vio_private_inputs import prepare_private_inputs
from meridian.vio_specification import build_specification, specification_sha256 as legacy_sha256
from . import acquisition_storage, input_revision_storage, private_inputs
from .execution_profiles import specification_sha256, validate_specification
from .models import ExperimentRevision, InputRevision, Job, SensorArtifact
from .test_input_revisions import RevisionSetup, identity
from .test_acquisitions import EXAMPLES


class PrivateSetup(RevisionSetup):
    def ready_source(self, descriptor=None, **kwargs):
        if descriptor is None:
            descriptor = json.loads((EXAMPLES / "stereo-no-reference.json").read_text())
            descriptor["streams"][-1]["clock_id"] = "camera-clock"
            descriptor["streams"][-1]["processing"]["calibration"] = "applied"
        return super().ready_source(descriptor, **kwargs)

    def import_artifact(self, document=None, **kwargs):
        if document is None:
            document = json.loads((EXAMPLES / "calibration.json").read_text())
            document["acquisition"] = identity(self.source.descriptor)
            document["applicability"]["imu_intrinsics"] = "already_applied"
            document["imu"] = {"sensor_id": "imu0", "frame_id": "imu", "gyro_bias_rad_s": [0, 0, 0],
                "accel_bias_m_s2": [0, 0, 0], "gyro_scale": [1, 1, 1], "accel_scale": [1, 1, 1],
                "noise": {"model": "continuous_white_noise_and_bias_diffusion", "gyro_density_rad_s_per_sqrt_hz": 0.001,
                    "accel_density_m_s2_per_sqrt_hz": 0.01, "gyro_bias_diffusion_rad_s_per_sqrt_s": 0.0001,
                    "accel_bias_diffusion_m_s2_per_sqrt_s": 0.001, "assumptions": "Synthetic metadata; no physical accuracy claim."}}
        return super().import_artifact(document, **kwargs)

    def setUp(self):
        super().setUp()
        self.prepared, _ = self.finish(self.create_revision()[0])
        self.inputs = f"/api/v1/projects/{self.project.pk}/execution-inputs"
        self.experiments = f"/api/v1/projects/{self.project.pk}/experiments"
        self.jobs = f"/api/v1/projects/{self.project.pk}/jobs"

    def assess(self, **changes):
        return self.send("post", self.inputs + "/assess", {"schema_version": 1,
            "input_revision_id": str(self.prepared.pk), "parameters": {"num_pts": 200}, "interval": None, **changes})

    def experiment(self, **changes):
        result = self.send("post", self.experiments, {"schema_version": 1, "name": "Owned stereo experiment",
            "profile_id": private_profile.PROFILE_ID, "input_revision_id": str(self.prepared.pk),
            "interval": None, "parameters": {"num_pts": 200}, **changes})
        self.assertEqual(result.status_code, 201, result.content)
        return result.json()

    def submit(self, experiment=None, **changes):
        experiment = experiment or self.experiment()
        return self.send("post", self.jobs, {"schema_version": 1,
            "experiment_id": experiment["experiment"]["id"], "revision_version": experiment["revision"]["version"],
            "idempotency_key": str(uuid.uuid4()), **changes})


@override_settings(MERIDIAN_EXECUTION_ENABLED=True)
class PrivateAdmissionTests(PrivateSetup, TestCase):
    def test_compact_catalog_and_assessment_bind_exact_publication_without_claiming_accuracy(self):
        listing = self.client.get(self.inputs).json()
        self.assertEqual(listing["profile"]["id"], private_profile.PROFILE_ID)
        self.assertEqual(listing["inputs"][0]["prepared_input"], identity(self.prepared.prepared_input))
        self.assertNotIn("files", listing["inputs"][0])
        result = self.assess()
        self.assertEqual(result.status_code, 200, result.content)
        data = result.json()
        self.assertTrue(data["eligible"], data)
        spec = data["specification"]
        validate_specification(spec)
        self.assertEqual(data["selection"]["interval"], spec["interval"])
        self.assertEqual(spec["input"]["publication_manifest_sha256"],
                         sha256(acquisition_storage.canonical(self.prepared.manifest) + b"\n").hexdigest())
        self.assertEqual(spec["reference"]["unavailable_reason"], "reference_absent")
        self.assertEqual(spec["policy"]["calibration"], "fixed_supplied_parameters_no_online_refinement")
        self.assertFalse(Job.objects.exists())

    def test_catalog_assessment_and_snapshot_ownership_and_csrf(self):
        self.assertEqual(Client().get(self.inputs).status_code, 401)
        self.assertEqual(self.other_client.get(self.inputs).status_code, 404)
        self.assertEqual(self.client.post(self.inputs + "/assess", {}, content_type="application/json").status_code, 403)
        foreign = f"/api/v1/projects/{self.other_project.pk}/execution-inputs/assess"
        body = {"schema_version": 1, "input_revision_id": str(self.prepared.pk)}
        self.assertEqual(self.send("post", foreign, body, client=self.other_client, token=self.other_token).status_code, 404)
        for value in (True, None, [], "../private", str(uuid.uuid4())):
            with self.subTest(value=value):
                self.assertIn(self.assess(input_revision_id=value).status_code, (400, 404))

    def test_snapshot_history_and_job_bindings_survive_parameter_edits(self):
        initial = self.experiment()
        saved = deepcopy(initial["revision"])
        edit = self.send("patch", self.experiments + "/" + initial["experiment"]["id"], {
            "schema_version": 1, "base_version": 1, "name": "More features", "parameters": {"num_pts": 400},
            "profile_id": private_profile.PROFILE_ID, **initial["revision"]["input_selection"]})
        self.assertEqual(edit.status_code, 200, edit.content)
        self.assertEqual(initial["profile"]["id"], private_profile.PROFILE_ID)
        submitted = self.submit(initial)
        self.assertEqual(submitted.status_code, 201, submitted.content)
        job = Job.objects.get()
        self.assertEqual(job.input_revision_id, self.prepared.pk)
        self.assertEqual(job.specification, saved["specification"])
        self.assertEqual(job.specification_sha256, saved["specification_sha256"])
        historical = self.client.get(self.experiments + "/" + initial["experiment"]["id"] + "/revisions/1")
        self.assertEqual(historical.json()["revision"], saved)

    def test_archiving_parameters_does_not_retarget_existing_prepared_inputs(self):
        experiment = self.experiment()
        artifact = SensorArtifact.objects.get()
        self.assertEqual(self.send("delete", self.artifacts + "/" + str(artifact.pk)).status_code, 200)
        self.assertEqual(self.submit(experiment).status_code, 201)

    def test_original_payload_deletion_preserves_self_contained_prepared_execution(self):
        deleted = self.send("delete", self.base + "/" + str(self.source.pk))
        self.assertEqual(deleted.status_code, 200, deleted.content)
        self.assertTrue(self.assess().json()["eligible"])
        self.assertEqual(self.submit().status_code, 201)

    def test_dependent_queued_job_protects_prepared_files_then_cancel_releases_dependency(self):
        experiment = self.experiment()
        submitted = self.submit(experiment)
        self.assertEqual(submitted.status_code, 201, submitted.content)
        files = self.revisions + "/" + str(self.prepared.pk) + "/files"
        blocked = self.send("delete", files)
        self.assertEqual((blocked.status_code, blocked.json()["error"]["code"]), (409, "active_jobs"))
        job = submitted.json()["job"]
        self.assertEqual(self.send("post", self.jobs + "/" + job["id"] + "/cancel").status_code, 200)
        self.assertEqual(self.send("delete", files).status_code, 200)
        self.assertFalse(self.assess().json()["eligible"])
        self.assertEqual(self.submit(experiment).status_code, 409)
        self.assertEqual(ExperimentRevision.objects.get().specification, experiment["revision"]["specification"])

    def test_changed_or_missing_retained_metadata_cannot_enter_queue(self):
        experiment = self.experiment()
        root = private_inputs.publication(self.prepared)
        path = root / "prepared-input.json"
        original = path.read_bytes()
        for content in (original + b" ", b'{"schema_version":1,"schema_version":1}'):
            path.write_bytes(content)
            result = self.submit(experiment)
            self.assertEqual((result.status_code, result.json()["error"]["code"]), (409, "inputs_unavailable"))
            self.assertFalse(Job.objects.exists())
        path.unlink()
        path.symlink_to(root / "source-acquisition.json")
        self.assertFalse(self.assess().json()["eligible"])

    def test_invalid_intervals_and_unsupported_measurements_are_explicit(self):
        valid = self.assess().json()["selection"]["interval"]
        for interval in ({**valid, "segment_id": "other"}, {**valid, "first_ns": 1},
                         {**valid, "first_ns": valid["last_ns"]}, {**valid, "last_ns": "9223372036854775808"}):
            with self.subTest(interval=interval):
                data = self.assess(interval=interval).json()
                self.assertFalse(data["eligible"])
                self.assertTrue(data["reasons"])
        self.assertFalse(self.assess(parameters={"num_pts": True}).json()["eligible"])

    def test_private_exports_and_comparisons_refuse_before_using_room1_sources(self):
        first = self.submit().json()["job"]
        self.send("post", self.jobs + "/" + first["id"] + "/cancel")
        second = self.submit().json()["job"]
        self.send("post", self.jobs + "/" + second["id"] + "/cancel")
        self.assertFalse(self.client.get(self.jobs + "/" + first["id"]).json()["job"]["export_available"])
        export = self.client.get(self.jobs + "/" + first["id"] + "/export?schema_version=1")
        self.assertEqual((export.status_code, export.json()["error"]["code"]), (409, "private_export_unavailable"))
        comparison = self.send("post", f"/api/v1/projects/{self.project.pk}/comparisons", {
            "schema_version": 1, "name": "Unsupported private comparison", "idempotency_key": str(uuid.uuid4()),
            "baseline_job_id": first["id"], "candidate_job_id": second["id"]})
        self.assertEqual((comparison.status_code, comparison.json()["error"]["code"]), (409, "private_comparison_unavailable"))

    def test_historical_profile_remains_exact_and_cannot_receive_private_fields_implicitly(self):
        for points in (100, 200, 400):
            spec = build_specification(self.project.dataset_id, {"num_pts": points})
            self.assertEqual(specification_sha256(spec), legacy_sha256(spec))
        result = self.send("post", self.experiments, {"schema_version": 1, "name": "Invalid mix",
            "dataset_id": self.project.dataset_id, "parameters": {"num_pts": 200},
            "input_revision_id": str(self.prepared.pk), "interval": None})
        self.assertEqual(result.status_code, 400)

    def test_hash_verified_csv_timeline_is_required_before_submission(self):
        experiment = self.experiment()
        path = private_inputs.publication(self.prepared) / "sensors/imu0/data.csv"
        path.write_bytes(path.read_bytes() + b"\n")
        self.assertFalse(self.assess().json()["eligible"])
        self.assertEqual(self.submit(experiment).status_code, 409)
        self.assertFalse(Job.objects.exists())

    def test_worker_dispatches_exact_owned_publication_and_retains_dependency_until_stopped(self):
        from . import job_worker
        submitted = self.submit().json()["job"]
        calls = []
        expected = private_inputs.publication(self.prepared)
        def execute(spec, prepared, staging, **kwargs):
            calls.append((spec, prepared))
            self.assertEqual(prepared, expected)
            self.assertEqual(self.send("delete", self.revisions + "/" + str(self.prepared.pk) + "/files").status_code, 409)
            return {"state": "cancelled", "failure": {"code": "cancelled", "message": "Cancelled"},
                    "result": None, "manifest": {"schema_version": 1, "files": {}}}
        def legacy(*args, **kwargs):
            self.fail("A private job must never dispatch to the room1 adapter.")
        scratch, retained = self.root / "job-scratch", self.root / "job-artifacts"
        scratch.mkdir(); retained.mkdir()
        with override_settings(MERIDIAN_JOB_SCRATCH_ROOT=scratch, MERIDIAN_JOB_ARTIFACT_ROOT=retained), \
                patch("workspace.job_worker.LeaseGuard"), patch.dict("sys.modules", {
                    "workspace.job_execution": SimpleNamespace(execute_attempt=legacy, cleanup_attempt=lambda _: True),
                    "workspace.private_job_execution": SimpleNamespace(execute_private_attempt=execute)}):
            self.assertTrue(job_worker.run_one(uuid.uuid4(), threading.Event()))
        job = Job.objects.get(pk=submitted["id"])
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.prepared.files_available)
        self.assertEqual(list(scratch.iterdir()), [])


@override_settings(MERIDIAN_EXECUTION_ENABLED=True)
class PrivateInvestigationTests(PrivateSetup, TestCase):
    def setUp(self):
        super().setUp()
        submitted = self.submit().json()["job"]
        self.job = Job.objects.get(pk=submitted["id"])
        self.job.state, self.job.phase = "succeeded", "succeeded"
        self.job.published_attempt = uuid.uuid4()
        self.job.artifacts_available = True
        self.job.artifacts_expire_at = timezone.now() + timedelta(days=7)
        self.job.artifact_manifest = {"schema_version": 1, "files": {}}
        self.job.save()
        artifacts = self.root / "job-artifacts"
        artifacts.mkdir()
        settings = override_settings(MERIDIAN_JOB_ARTIFACT_ROOT=artifacts)
        settings.enable(); self.addCleanup(settings.disable)
        self.directory = artifacts / str(self.job.pk) / str(self.job.published_attempt)
        self.directory.mkdir(parents=True)
        spec = self.job.specification
        handoff = prepare_private_inputs(spec, private_inputs.publication(self.prepared), self.root / "sensor-staging")
        self.index = handoff["image_index"]
        first, last = handoff["camera_timestamps_ns"]
        self.evidence = {"schema_version": 2, "specification_sha256": self.job.specification_sha256,
            "run_state": "complete", "input": spec["input"] | {"profile_id": spec["profile_id"],
                "clock_id": spec["clock_id"], "imu_frame_id": spec["imu"]["frame_id"], "processing": spec["processing"]},
            "time": {"origin_timestamp_ns": first, "first_timestamp_ns": first, "last_timestamp_ns": last},
            "alignment": None, "protocol": {"alignment": "global_se3"},
            "evaluation": {"status": "unavailable", "reason": "reference_absent"},
            "provenance": {"interval_scorer_sha256": sha256(Path(vio_investigation_metrics.__file__).read_bytes()).hexdigest()},
            "frames": [{"index": i, "timestamp_ns": stamp, "estimated": True, "reference_supported": False,
                "associated": False, "evaluated": False, "position_error_m": None, "orientation_error_deg": None}
                for i, stamp in enumerate(handoff["camera_timestamps_ns"])]}
        self.publish("investigation.json", self.evidence)
        self.publish("image-index.json", self.index)
        self.url = self.jobs + "/" + str(self.job.pk) + "/investigation"

    def publish(self, name, document):
        content = json.dumps(document).encode()
        (self.directory / name).write_bytes(content)
        self.job.artifact_manifest["files"][name] = {"sha256": sha256(content).hexdigest(), "size_bytes": len(content)}
        self.job.save(update_fields=["artifact_manifest"])

    def test_owned_image_and_numerical_evidence_have_independent_retention(self):
        image_url = self.url + "/images/cam0/0"
        image = self.client.get(image_url)
        self.assertEqual(image.status_code, 200, image.content)
        self.assertEqual(sha256(image.content).hexdigest(), self.index["cameras"]["cam0"][0]["sha256"])
        self.assertEqual(image["Cache-Control"], "no-store")
        self.assertEqual(self.other_client.get(image_url).status_code, 404)
        first, last = self.evidence["time"]["first_timestamp_ns"], self.evidence["time"]["last_timestamp_ns"]
        metrics = self.url + f"/metrics?start_ns={first}&end_ns={last}"
        before = self.client.get(metrics)
        self.assertEqual(before.status_code, 200, before.content)
        self.assertIsNone(before.json()["interval"]["metrics"])
        deleted = self.send("delete", self.revisions + "/" + str(self.prepared.pk) + "/files")
        self.assertEqual(deleted.status_code, 200, deleted.content)
        self.assertEqual(self.client.get(image_url).status_code, 410)
        self.assertEqual(self.client.get(self.url).json()["investigation"], self.evidence)
        self.assertEqual(self.client.get(metrics).content, before.content)
        self.assertEqual(self.client.get(self.jobs + "/" + str(self.job.pk)).json()["job"]["input_availability"], "unavailable")

    def test_camera_reads_refuse_changed_bytes_and_out_of_range_or_foreign_index(self):
        image_url = self.url + "/images/cam0/0"
        entry = self.index["cameras"]["cam0"][0]
        path = private_inputs.publication(self.prepared) / entry["relative_path"]
        content = path.read_bytes()
        path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        self.assertEqual(self.client.get(image_url).status_code, 503)
        path.write_bytes(content)
        self.assertEqual(self.client.get(self.url + "/images/cam0/999").status_code, 404)
        self.index["input"]["input_revision_id"] = str(uuid.uuid4())
        self.publish("image-index.json", self.index)
        self.assertEqual(self.client.get(image_url).status_code, 503)

    def test_unicode_package_path_is_safe_and_not_confused_with_api_routes(self):
        entry = self.index["cameras"]["cam0"][0]
        root = private_inputs.publication(self.prepared)
        old = root / entry["relative_path"]
        name = "sensors/cam0/images/caméra privée.png"
        old.rename(root / name)
        self.prepared.manifest["files"][name] = self.prepared.manifest["files"].pop(entry["relative_path"])
        self.prepared.save(update_fields=["manifest"])
        entry["relative_path"] = name
        self.publish("image-index.json", self.index)
        self.assertEqual(self.client.get(self.url + "/images/cam0/0").status_code, 200)
        (root / name).unlink()
        (root / name).symlink_to(root / "sensors/cam1/images/0.png")
        self.assertEqual(self.client.get(self.url + "/images/cam0/0").status_code, 503)
