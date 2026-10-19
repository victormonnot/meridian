"""Persisted result identity, access, retention and comparison write boundaries."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from meridian import vio_investigation_metrics
from meridian.vio_investigation_metrics import summarize_interval
from meridian.vio_specification import build_specification, specification_sha256
from .investigation import ArtifactUnavailable
from .models import Experiment, Job, Project, RunComparison
from .test_experiments import ExperimentCase


class ComparisonTests(ExperimentCase):
    def setUp(self):
        super().setUp()
        self.experiment = self.create_experiment()
        self.url = f"/api/v1/projects/{self.project['id']}/comparisons"
        # A published scalar result is a transport fixture here. These tests do
        # not claim a new native execution or a measurement for the variant.
        report = Path(__file__).resolve().parents[2] / "results/investigation/hosted-run.json"
        self.result = json.loads(report.read_text())["result"]
        self.baseline = self.make_job(200)
        self.candidate = self.make_job(100)

    def make_job(self, points, **changes):
        spec = build_specification(self.project["dataset_id"], {"num_pts": points})
        fields = dict(owner=self.alpha, project_id=self.project["id"],
                      experiment_id=self.experiment["experiment"]["id"], experiment_name="Synthetic transport fixture",
                      revision_version=1, specification=spec, specification_sha256=specification_sha256(spec),
                      idempotency_key=uuid.uuid4(), state="succeeded", phase="succeeded", attempt_count=1,
                      result=deepcopy(self.result), started_at=timezone.now() - timedelta(seconds=10),
                      finished_at=timezone.now())
        fields.update(changes)
        return Job.objects.create(**fields)

    def create(self, **changes):
        return self.mutate("post", self.url,
                           {"schema_version": 1, "name": "Baseline and variant", "description": "Explicit outcome snapshot",
                            "baseline_job_id": str(self.baseline.pk), "candidate_job_id": str(self.candidate.pk),
                            "idempotency_key": str(uuid.uuid4()), **changes})

    def saved(self, **changes):
        result = self.create(**changes)
        self.assertEqual(result.status_code, 201, result.content)
        return result.json()["comparison"]

    def publish_fixture_pair(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        setting = override_settings(MERIDIAN_JOB_ARTIFACT_ROOT=root)
        setting.enable()
        self.addCleanup(setting.disable)
        for job in (self.baseline, self.candidate):
            spec = job.specification
            first, last = (int(spec["interval"][key]) for key in ("first_camera_timestamp_ns", "last_camera_timestamp_ns"))
            count = spec["interval"]["camera_pairs"]
            frames = []
            for index in range(count):
                present = index > 0 and not (job == self.candidate and index == 2)
                position_error = (100 if index == 2 else 1) if job == self.baseline else 2
                frames.append({"index": index, "timestamp_ns": str(first + (last-first)*index//(count-1)),
                               "estimated": present, "reference_supported": True, "associated": present,
                               "evaluated": present, "position_error_m": position_error if present else None,
                               "orientation_error_deg": 1 if present else None})
            evidence = {"schema_version": 1, "run_state": "complete", "specification_sha256": job.specification_sha256,
                        "dataset_manifest_sha256": spec["dataset"]["prepared_manifest_sha256"],
                        "time": {"origin_timestamp_ns": str(first), "first_timestamp_ns": str(first),
                                 "last_timestamp_ns": str(last), "basis": "recording_sensor_timestamps", "offset_fitted": False},
                        "alignment": {"scale": 1}, "protocol": deepcopy(self.result["evaluation"]["protocol"]),
                        "coordinate_frames": {"quaternion": "Hamilton_xyzw_body_to_world"},
                        "units": {"position": "m", "orientation_error": "deg"}, "frames": frames,
                        "provenance": {"dataset_id": spec["dataset"]["id"],
                                       "reference_sha256": spec["dataset"]["reference_sha256"],
                                       "evaluator_sha256": spec["evaluation"]["evaluator_sha256"],
                                       "interval_scorer_sha256": sha256(Path(vio_investigation_metrics.__file__).read_bytes()).hexdigest(),
                                       "display_downsampling": False,
                                       **{key: "1" * 64 for key in ("builder_sha256", "trajectory_sha256", "imu_sha256", "evaluation_sha256")}}}
            interval = summarize_interval(evidence, str(first), str(last))
            job.result = deepcopy(job.result)
            job.result["evaluation"].update({key: interval[key] for key in
                                              ("metrics", "counts", "coverage_fraction", "evaluated_fraction", "reference_coverage_fraction")})
            job.result["evaluation"]["initialization_delay_s"] = (int(frames[1]["timestamp_ns"])-first)*1e-9
            job.artifacts_available, job.published_attempt = True, uuid.uuid4()
            job.artifacts_expire_at = timezone.now() + timedelta(days=7)
            data = json.dumps(evidence, allow_nan=False).encode()
            job.artifact_manifest = {"schema_version": 1, "files": {"investigation.json": {"sha256": sha256(data).hexdigest(), "size_bytes": len(data)}}}
            directory = root / str(job.pk) / str(job.published_attempt)
            directory.mkdir(parents=True)
            (directory / "investigation.json").write_bytes(data)
            job.save()
        return root

    def test_verified_artifacts_expose_support_loss_instead_of_rewarding_dropped_errors(self):
        self.publish_fixture_pair()
        value = self.saved()
        analysis = value["analysis"]
        # Candidate drops the baseline's hard epoch: its own-support RMSE looks
        # lower, but its error on shared epochs is actually twice the baseline.
        self.assertLess(analysis["candidate"]["own_support"]["metrics"]["ate_rmse_m"],
                        analysis["baseline"]["own_support"]["metrics"]["ate_rmse_m"])
        common = analysis["common_support"]
        self.assertEqual(common["evaluated_camera_samples"], 2819)
        self.assertEqual(common["baseline_metrics"]["ate_rmse_m"], 1)
        self.assertEqual(common["candidate_metrics"]["ate_rmse_m"], 2)
        self.assertEqual(common["delta_candidate_minus_baseline"]["ate_rmse_m"], 1)
        missing = analysis["support_difference"]["evaluated"]["baseline_only"]
        self.assertEqual(missing["sample_count"], 1)
        self.assertEqual(missing["intervals"][0]["first_camera_index"], 2)
        self.assertIn("different_support", [warning["code"] for warning in analysis["warnings"]])
        self.assertFalse(analysis["protocol"]["ranking"])
        self.assertTrue(value["baseline_access"]["investigation_available"])
        self.baseline.artifacts_expire_at = timezone.now() - timedelta(seconds=1)
        self.baseline.save()
        with patch("workspace.comparisons._artifact") as read:
            detail = self.client.get(f"{self.url}/{value['id']}").json()["comparison"]
            read.assert_not_called()
        self.assertEqual(detail["analysis"], analysis)
        self.assertFalse(detail["baseline_access"]["investigation_available"])

    def test_tampered_actual_file_is_rejected_before_saving_any_snapshot(self):
        root = self.publish_fixture_pair()
        path = root / str(self.candidate.pk) / str(self.candidate.published_attempt) / "investigation.json"
        path.write_bytes(path.read_bytes() + b" ")
        self.assert_error(self.create(), 503, "artifact_unavailable")
        self.assertFalse(RunComparison.objects.exists())

    def test_save_keeps_exact_identities_and_immutable_canonical_analysis(self):
        value = self.saved()
        self.assertEqual(value["baseline_job_id"], str(self.baseline.pk))
        self.assertEqual(value["candidate_job_id"], str(self.candidate.pk))
        self.assertEqual(value["analysis"]["baseline"]["specification_sha256"], self.baseline.specification_sha256)
        self.assertEqual(value["analysis"]["candidate"]["parameters"], {"num_pts": 100})
        encoded = json.dumps(value["analysis"], sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        self.assertEqual(value["analysis_sha256"], sha256(encoded).hexdigest())
        detail = self.client.get(f"{self.url}/{value['id']}")
        self.assertEqual(detail.json()["comparison"], value)
        self.assertEqual(detail["Cache-Control"], "no-store")
        self.assertFalse(value["baseline_access"]["investigation_available"])
        self.assertTrue(value["baseline_access"]["exists"])
        item = RunComparison.objects.get(pk=value["id"])
        item.name = "Mutated"
        with self.assertRaises(ValidationError):
            item.save()

    def test_missing_artifacts_preserve_recorded_scores_without_common_support_claim(self):
        value = self.saved()["analysis"]
        self.assertIsNone(value["baseline"]["own_support"])
        self.assertEqual(value["baseline"]["recorded_full_run"], self.result["evaluation"])
        self.assertNotEqual(value["common_support"]["status"], "ok")
        self.assertIsNone(value["common_support"]["baseline_metrics"])
        self.assertIsNone(value["common_support"]["candidate_metrics"])
        self.assertIsNone(value["support_difference"])

    def test_snapshot_survives_source_job_and_experiment_deletion(self):
        value = self.saved()
        deleted = self.mutate("delete", f"/api/v1/projects/{self.project['id']}/jobs/{self.baseline.pk}")
        self.assertEqual(deleted.status_code, 200, deleted.content)
        self.assertEqual(self.mutate("delete", f"/api/v1/projects/{self.project['id']}/experiments/{self.candidate.experiment_id}").status_code, 200)
        detail = self.client.get(f"{self.url}/{value['id']}").json()["comparison"]
        self.assertEqual(detail["analysis"], value["analysis"])
        self.assertEqual(detail["analysis_sha256"], value["analysis_sha256"])
        for role in ("baseline", "candidate"):
            self.assertEqual(detail[role + "_access"], {"exists": False, "investigation_available": False})
        self.assertEqual(self.client.get(self.url).json()["comparisons"][0]["id"], value["id"])

    def test_expiry_is_checked_before_any_file_read(self):
        self.baseline.published_attempt = uuid.uuid4()
        self.baseline.artifacts_available = True
        self.baseline.artifacts_expire_at = timezone.now() - timedelta(seconds=1)
        self.baseline.artifact_manifest = {"schema_version": 1, "files": {"investigation.json": {}}}
        self.baseline.save()
        with patch("workspace.comparisons._artifact") as read:
            value = self.saved()
            read.assert_not_called()
        self.assertEqual(value["analysis"]["baseline"]["investigation"]["unavailable_reason"], "expired")

    def test_list_is_compact_and_project_scoped(self):
        first = self.saved()
        self.saved(name="Another decision")
        values = self.client.get(self.url).json()
        self.assertEqual(values["limits"], {"comparisons": 20})
        self.assertEqual(len(values["comparisons"]), 2)
        self.assertNotIn("analysis", values["comparisons"][0])
        self.assertEqual(values["comparisons"][1]["summary"]["baseline"]["parameters"], {"num_pts": 200})
        project = self.create_project(name="Separate project")
        self.assertEqual(self.client.get(f"/api/v1/projects/{project['id']}/comparisons").json()["comparisons"], [])
        self.assert_error(self.client.get(f"/api/v1/projects/{project['id']}/comparisons/{first['id']}"), 404, "not_found")

    def test_authentication_ownership_and_csrf_precede_evidence_access(self):
        value = self.saved()
        detail = f"{self.url}/{value['id']}"
        peer = Client()
        peer.force_login(self.beta)
        with patch("workspace.comparisons._artifact") as read:
            for route in (self.url, detail):
                self.assert_error(Client().get(route), 401, "authentication_required")
                self.assert_error(peer.get(route), 404, "not_found")
            self.assert_error(self.client.post(self.url, data='{}', content_type="application/json"), 403, "csrf_failed")
            self.assert_error(self.client.delete(detail), 403, "csrf_failed")
            read.assert_not_called()

    def test_foreign_or_other_project_run_is_not_read_or_disclosed(self):
        other = self.create_project(name="Different recording work")
        self.candidate.project_id = other["id"]
        self.candidate.save()
        with patch("workspace.comparisons._artifact") as read:
            self.assert_error(self.create(), 404, "not_found")
            read.assert_not_called()
        self.candidate.project_id, self.candidate.owner = self.project["id"], self.beta
        self.candidate.save()
        self.assert_error(self.create(), 404, "not_found")
        self.assertEqual(RunComparison.objects.count(), 0)

    def test_active_run_cannot_be_frozen_as_a_final_comparison(self):
        for state in ("queued", "running", "evaluating"):
            self.candidate.state = state
            self.candidate.save()
            self.assert_error(self.create(), 409, "active_runs")
        self.assertEqual(RunComparison.objects.count(), 0)

    def test_failed_and_cancelled_runs_remain_visible_without_invented_scores(self):
        for state in ("failed", "cancelled"):
            job = self.make_job(100, state=state, result=None,
                                failure={"code": "initialization_failure", "message": "Synthetic failed fixture."} if state == "failed" else None)
            value = self.saved(candidate_job_id=str(job.pk))["analysis"]["candidate"]
            self.assertEqual(value["state"], state)
            self.assertIsNone(value["recorded_full_run"])
            self.assertIsNone(value["own_support"])

    def test_same_specification_distinct_runs_are_an_explicit_repeat(self):
        repeated = self.make_job(200)
        value = self.saved(candidate_job_id=str(repeated.pk))["analysis"]
        self.assertEqual(value["kind"], "repeated_execution")
        self.assertEqual(value["configuration_changes"], [])
        self.assertNotEqual(value["baseline"]["id"], value["candidate"]["id"])

    def test_idempotent_retry_survives_source_deletion_and_does_not_duplicate(self):
        key = str(uuid.uuid4())
        value = self.saved(idempotency_key=key)
        self.baseline.delete()
        repeated = self.create(idempotency_key=key, baseline_job_id=value["baseline_job_id"])
        self.assertEqual(repeated.status_code, 200, repeated.content)
        self.assertTrue(repeated.json()["reused"])
        self.assertEqual(repeated.json()["comparison"]["id"], value["id"])
        self.assertEqual(RunComparison.objects.count(), 1)
        self.assert_error(self.create(idempotency_key=key, baseline_job_id=value["baseline_job_id"], name="Changed intent"), 409, "idempotency_conflict")

    @override_settings(MERIDIAN_COMPARISON_LIMIT=1)
    def test_quota_is_checked_before_artifact_reads_but_allows_retry_and_delete(self):
        key = str(uuid.uuid4())
        value = self.saved(idempotency_key=key)
        with patch("workspace.comparisons._artifact") as read:
            self.assert_error(self.create(), 409, "comparison_limit")
            self.assertEqual(self.create(idempotency_key=key).status_code, 200)
            read.assert_not_called()
        self.assertEqual(self.mutate("delete", f"{self.url}/{value['id']}").status_code, 200)
        self.assertTrue(Job.objects.filter(pk=self.baseline.pk).exists())
        self.assertEqual(self.create().status_code, 201)

    def test_invalid_request_values_do_not_create_snapshots(self):
        for changes in ({"baseline_job_id": str(self.candidate.pk)}, {"candidate_job_id": "../private"},
                        {"idempotency_key": True}, {"schema_version": True}, {"start_ns": "1"},
                        {"start_ns": 1, "end_ns": 2}, {"start_ns": "2", "end_ns": "1"},
                        {"start_ns": "01", "end_ns": "2"}, {"analysis": {}}, {"name": " "},
                        {"description": "x" * 2001}):
            with self.subTest(changes=changes):
                self.assertEqual(self.create(**changes).status_code, 400)
        self.assertEqual(RunComparison.objects.count(), 0)

    def test_unknown_query_and_update_method_are_rejected(self):
        value = self.saved()
        self.assert_error(self.client.get(self.url, {"owner": self.beta.pk}), 400, "invalid_request")
        detail = f"{self.url}/{value['id']}"
        self.assert_error(self.client.get(detail, {"recompute": "true"}), 400, "invalid_request")
        self.assert_error(self.mutate("patch", detail, {"name": "Overwrite"}), 405, "method_not_allowed")

    def test_changed_specification_is_not_compared_as_a_supported_variant(self):
        self.candidate.specification["dataset"]["reference_sha256"] = "0" * 64
        self.candidate.save()
        self.assert_error(self.create(), 409, "incompatible_runs")
        self.assertFalse(RunComparison.objects.exists())

    def test_corrupted_published_evidence_cannot_fall_back_to_scalar_metadata(self):
        self.baseline.artifacts_available = True
        self.baseline.published_attempt = uuid.uuid4()
        self.baseline.artifacts_expire_at = timezone.now() + timedelta(days=7)
        self.baseline.artifact_manifest = {"schema_version": 1, "files": {"investigation.json": {}}}
        self.baseline.save()
        with patch("workspace.comparisons._artifact", side_effect=ArtifactUnavailable("/private/do-not-disclose")):
            result = self.create()
        self.assert_error(result, 503, "artifact_unavailable")
        self.assertNotIn(b"do-not-disclose", result.content)
        self.assertFalse(RunComparison.objects.exists())

    def test_source_identity_changes_do_not_silently_reinterpret_evidence(self):
        self.baseline.artifacts_available = True
        self.baseline.published_attempt = uuid.uuid4()
        self.baseline.artifacts_expire_at = timezone.now() + timedelta(days=7)
        self.baseline.artifact_manifest = {"schema_version": 1, "files": {"investigation.json": {}}}
        self.baseline.save()
        with patch("workspace.comparisons._artifact", return_value={"provenance": {"interval_scorer_sha256": "0" * 64}}):
            self.assert_error(self.create(), 503, "artifact_unavailable")
        self.assertFalse(RunComparison.objects.exists())

    def test_parent_project_deletion_removes_saved_comparisons(self):
        self.saved()
        result = self.mutate("delete", f"/api/v1/projects/{self.project['id']}")
        self.assertEqual(result.status_code, 200, result.content)
        self.assertFalse(RunComparison.objects.exists())

    def test_database_rejects_identical_source_ids_independent_of_api(self):
        value = self.saved()
        with self.assertRaises(IntegrityError), transaction.atomic():
            RunComparison.objects.filter(pk=value["id"]).update(candidate_job_id=self.baseline.pk)


class ComparisonConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("The comparison write races require PostgreSQL row locks.")
        self.owner = get_user_model().objects.create_user("comparison.race")
        spec = build_specification("tumvi-room1-512-16-v1", {"num_pts": 200})
        self.project = Project.objects.create(owner=self.owner, name="Concurrency fixture", dataset_id=spec["dataset"]["id"])
        experiment = Experiment.objects.create(project=self.project, name="Concurrent snapshots", parameters=spec["parameters"],
                                               specification_sha256=specification_sha256(spec))
        jobs = [Job.objects.create(owner=self.owner, project=self.project, experiment=experiment,
                                    experiment_name=experiment.name, revision_version=1, state="cancelled", phase="cancelled",
                                    specification=spec, specification_sha256=specification_sha256(spec),
                                    idempotency_key=uuid.uuid4()) for _ in range(2)]
        self.url = f"/api/v1/projects/{self.project.pk}/comparisons"
        self.payload = {"schema_version": 1, "name": "Concurrent snapshot", "baseline_job_id": str(jobs[0].pk),
                        "candidate_job_id": str(jobs[1].pk), "idempotency_key": str(uuid.uuid4())}

    def concurrent_posts(self, same_key):
        barrier = threading.Barrier(2)

        def send(index):
            close_old_connections()
            try:
                client = Client()
                client.force_login(self.owner)
                data = dict(self.payload)
                if not same_key:
                    data["idempotency_key"] = str(uuid.uuid4())
                barrier.wait(timeout=5)
                response = client.post(self.url, data=json.dumps(data), content_type="application/json")
                return response.status_code, response.json()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(send, i) for i in range(2)]
            return [future.result(timeout=15) for future in futures]

    def test_simultaneous_retry_creates_one_identity(self):
        results = self.concurrent_posts(same_key=True)
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(len({value["comparison"]["id"] for _, value in results}), 1)
        self.assertEqual(RunComparison.objects.count(), 1)

    @override_settings(MERIDIAN_COMPARISON_LIMIT=1)
    def test_simultaneous_distinct_creations_cannot_exceed_project_quota(self):
        results = self.concurrent_posts(same_key=False)
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assertEqual(next(value["error"]["code"] for status, value in results if status == 409), "comparison_limit")
        self.assertEqual(RunComparison.objects.count(), 1)
