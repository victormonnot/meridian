"""Submission identity, ownership, quotas, leases, cancellation and publication."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import time
from unittest.mock import patch
import uuid

from django.test import Client, override_settings
from django.utils import timezone

from . import job_queue as queue
from meridian.vio_job_control import AttemptLeaseLost
from .job_worker import LeaseGuard, canonical, collect_artifacts, publish_files, run_one
from .models import Job, JobAttempt
from .test_experiments import ExperimentCase


@override_settings(MERIDIAN_EXECUTION_ENABLED=True)
class JobTests(ExperimentCase):
    def setUp(self):
        super().setUp()
        self.experiment = self.create_experiment()
        self.jobs_url = f"/api/v1/projects/{self.project['id']}/jobs"

    def submit(self, **changes):
        return self.mutate("post", self.jobs_url,
                           {"schema_version": 1, "experiment_id": self.experiment["experiment"]["id"],
                            "revision_version": 1, "idempotency_key": str(uuid.uuid4()), **changes})

    def queued(self):
        result = self.submit()
        self.assertEqual(result.status_code, 201, result.content)
        return Job.objects.get(pk=result.json()["job"]["id"])

    def outcome(self, state="succeeded", code=None):
        return {"state": state, "failure": queue.failure(code) if code else None,
                "result": {"qualification": {"passed": False}} if state == "succeeded" else None,
                "manifest": {"schema_version": 1, "files": {"status.json": {"size_bytes": 2, "sha256": "a" * 64}}}}

    def test_snapshot_and_historical_revision_are_pinned_at_submission(self):
        self.edit(self.experiment, parameters={"num_pts": 400})
        job = self.queued()
        self.assertEqual(job.specification, self.experiment["revision"]["specification"])
        self.assertEqual(job.specification_sha256, self.experiment["revision"]["specification_sha256"])
        self.assertEqual(job.revision_version, 1)
        self.assertEqual(self.client.get(self.jobs_url).json()["jobs"][0]["parameters"], {"num_pts": 200})

    def test_idempotency_replays_existing_job_without_consuming_budget(self):
        key = str(uuid.uuid4())
        first = self.submit(idempotency_key=key)
        for _ in range(12):
            repeated = self.submit(idempotency_key=key)
            self.assertEqual(repeated.status_code, 200)
            self.assertTrue(repeated.json()["reused"])
            self.assertEqual(repeated.json()["job"]["id"], first.json()["job"]["id"])
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(self.submit().status_code, 201)

    def test_idempotency_conflict_does_not_retarget_saved_work(self):
        key = str(uuid.uuid4())
        self.submit(idempotency_key=key)
        self.edit(self.experiment, parameters={"num_pts": 100})
        self.assert_error(self.submit(idempotency_key=key, revision_version=2), 409, "idempotency_conflict")

    @override_settings(MERIDIAN_EXECUTION_ENABLED=False)
    def test_disabled_execution_has_no_runnable_submission(self):
        self.assertFalse(self.client.get("/api/v1/session").json()["capabilities"]["execution"])
        self.assert_error(self.submit(), 503, "execution_unavailable")
        self.assertEqual(Job.objects.count(), 0)

    def test_invalid_types_and_unsupported_fields_never_enqueue(self):
        cases = [{"revision_version": True}, {"revision_version": 1.0}, {"revision_version": "1"},
                 {"revision_version": 0}, {"revision_version": 51}, {"idempotency_key": None},
                 {"idempotency_key": "../private"}, {"experiment_id": []},
                 {"schema_version": True}, {"command": "any command"}]
        for fields in cases:
            with self.subTest(fields=fields):
                self.assert_error(self.submit(**fields), 400, "invalid_request")
        self.assertEqual(Job.objects.count(), 0)

    def test_missing_revision_and_corrupted_snapshot_are_rejected(self):
        self.assert_error(self.submit(revision_version=2), 404, "not_found")
        from .models import ExperimentRevision
        ExperimentRevision.objects.update(specification_sha256="0" * 64)
        self.assert_error(self.submit(), 409, "invalid_specification")

    def test_active_quota_and_cancellation_release_only_the_queue_slot(self):
        first, second = self.queued(), self.queued()
        self.assert_error(self.submit(), 409, "queue_full")
        result = self.mutate("post", f"{self.jobs_url}/{first.pk}/cancel", {"schema_version": 1})
        self.assertEqual(result.json()["job"]["state"], "cancelled")
        self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(Job.objects.count(), 3)

    def test_deleted_jobs_do_not_reset_the_daily_submission_budget(self):
        for _ in range(10):
            job = self.queued()
            self.mutate("post", f"{self.jobs_url}/{job.pk}/cancel", {"schema_version": 1})
            self.assertEqual(self.mutate("delete", f"{self.jobs_url}/{job.pk}").status_code, 200)
        rejected = self.submit()
        self.assert_error(rejected, 429, "submission_limit")
        self.assertGreater(int(rejected["Retry-After"]), 0)

    def test_job_endpoints_require_authentication_csrf_and_owner(self):
        job = self.queued()
        detail = f"{self.jobs_url}/{job.pk}"
        self.assert_error(Client().get(detail), 401, "authentication_required")
        self.assert_error(self.client.post(detail + "/cancel", data='{"schema_version":1}',
                                           content_type="application/json"), 403, "csrf_failed")
        from django.contrib.auth import get_user_model
        peer = get_user_model().objects.create_user("jobs.peer", password="Separate!Account-2468")
        other = Client()
        other.force_login(peer)
        for method, path, data in [("get", self.jobs_url, {}), ("get", detail, {}),
                                   ("delete", detail, {}), ("post", detail + "/cancel", {"schema_version": 1}),
                                   ("post", self.jobs_url, {"schema_version": 1})]:
            with self.subTest(path=path, method=method):
                result = getattr(other, method)(path, data=json.dumps(data), content_type="application/json") if method != "get" else other.get(path)
                self.assert_error(result, 404, "not_found")

    def test_active_jobs_block_parent_and_job_deletion_then_allow_cascade(self):
        job = self.queued()
        experiment_url = f"{self.url}/{job.experiment_id}"
        self.assert_error(self.mutate("delete", experiment_url), 409, "active_jobs")
        self.assert_error(self.mutate("delete", f"/api/v1/projects/{job.project_id}"), 409, "active_jobs")
        self.assert_error(self.mutate("delete", f"{self.jobs_url}/{job.pk}"), 409, "active_job")
        self.mutate("post", f"{self.jobs_url}/{job.pk}/cancel", {"schema_version": 1})
        self.assertEqual(self.mutate("delete", experiment_url).status_code, 200)
        self.assertFalse(Job.objects.exists())

    def test_filters_cannot_accept_paths_repeated_keys_or_foreign_jobs(self):
        job = self.queued()
        self.assertEqual(len(self.client.get(self.jobs_url, {"revision_version": 1}).json()["jobs"]), 1)
        self.assertEqual(self.client.get(self.jobs_url, {"experiment_id": str(uuid.uuid4())}).json()["jobs"], [])
        for query in ("revision_version=1&revision_version=2", "path=private", "experiment_id=../", "revision_version=true"):
            self.assert_error(self.client.get(self.jobs_url + "?" + query), 400, "invalid_request")

    def test_fifo_claim_is_exclusive_and_records_an_attempt(self):
        first, second = self.queued(), self.queued()
        claimed, attempt = queue.claim(uuid.uuid4())
        self.assertEqual(claimed.pk, first.pk)
        self.assertEqual(attempt.number, 1)
        self.assertIsNone(queue.claim(uuid.uuid4()))
        self.assertEqual(JobAttempt.objects.count(), 1)

    def test_heartbeat_cannot_revive_an_expired_lease_or_change_another_attempt(self):
        job = self.queued()
        job, attempt = queue.claim(uuid.uuid4())
        self.assertEqual(queue.heartbeat(job.pk, uuid.uuid4(), "evaluating"), "lost")
        self.assertEqual(queue.heartbeat(job.pk, attempt.pk, "evaluating"), "continue")
        Job.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(queue.heartbeat(job.pk, attempt.pk, "estimating"), "lost")
        self.assertFalse(queue.finish(job.pk, attempt.pk, self.outcome()))
        job.refresh_from_db()
        self.assertIsNone(job.result)

    def test_cancellation_wins_race_with_late_success(self):
        self.queued()
        job, attempt = queue.claim(uuid.uuid4())
        self.mutate("post", f"{self.jobs_url}/{job.pk}/cancel", {"schema_version": 1})
        self.assertEqual(queue.heartbeat(job.pk, attempt.pk, "evaluating"), "cancel")
        self.assertTrue(queue.finish(job.pk, attempt.pk, self.outcome(), artifacts_available=True))
        job.refresh_from_db()
        self.assertEqual(job.state, "cancelled")
        self.assertIsNone(job.result)
        self.assertEqual(job.failure["code"], "cancelled")
        self.assertIsNone(job.published_attempt)
        self.assertIsNone(job.artifact_manifest)
        self.assertFalse(job.artifacts_available)

    def test_recovery_retries_once_and_fences_the_old_worker(self):
        self.queued()
        job, first = queue.claim(uuid.uuid4())
        Job.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(queue.expired_attempts(), [(job.pk, first.pk)])
        self.assertIsNone(queue.claim(uuid.uuid4()))
        self.assertTrue(queue.recover(job.pk, first.pk))
        job, second = queue.claim(uuid.uuid4())
        self.assertEqual(second.number, 2)
        self.assertFalse(queue.finish(job.pk, first.pk, self.outcome()))
        Job.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        self.assertTrue(queue.recover(job.pk, second.pk))
        job.refresh_from_db()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.failure["code"], "infrastructure_failure")
        self.assertIsNone(queue.claim(uuid.uuid4()))

    def test_recovery_of_cancelled_attempt_never_requeues(self):
        self.queued()
        job, attempt = queue.claim(uuid.uuid4())
        self.mutate("post", f"{self.jobs_url}/{job.pk}/cancel", {"schema_version": 1})
        Job.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        queue.recover(job.pk, attempt.pk)
        job.refresh_from_db()
        self.assertEqual(job.state, "cancelled")

    def test_infrastructure_failure_retries_but_numerical_failure_does_not(self):
        self.queued()
        job, first = queue.claim(uuid.uuid4())
        self.assertTrue(queue.finish(job.pk, first.pk, self.outcome("failed", "infrastructure_failure")))
        job, second = queue.claim(uuid.uuid4())
        self.assertTrue(queue.finish(job.pk, second.pk, self.outcome("failed", "numerical_failure")))
        job.refresh_from_db()
        self.assertEqual(job.state, "failed")
        self.assertEqual(list(job.attempts.values_list("failure__code", flat=True)),
                         ["infrastructure_failure", "numerical_failure"])

    def test_validated_success_keeps_failed_qualification_visible_and_is_final(self):
        self.queued()
        job, attempt = queue.claim(uuid.uuid4())
        self.assertTrue(queue.finish(job.pk, attempt.pk, self.outcome(), artifacts_available=True))
        job.refresh_from_db()
        self.assertEqual(job.state, "succeeded")
        self.assertFalse(job.result["qualification"]["passed"])
        self.assertEqual(job.published_attempt, attempt.pk)
        self.assertFalse(queue.finish(job.pk, attempt.pk, self.outcome("failed", "numerical_failure")))

    def test_publication_rejects_missing_success_metadata(self):
        self.queued()
        job, attempt = queue.claim(uuid.uuid4())
        with self.assertRaises(ValueError):
            queue.finish(job.pk, attempt.pk, {"state": "succeeded", "result": None, "manifest": None})
        job.refresh_from_db()
        self.assertEqual(job.state, "running")

    def test_artifact_promotion_rechecks_digest_and_never_follows_symlinks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "scratch"
            staging.mkdir()
            (staging / "status.json").write_bytes(b"{}")
            manifest = {"schema_version": 1, "files": {"status.json": {"size_bytes": 2, "sha256": sha256(b"{}").hexdigest()}}}
            result = {"artifacts": {"file_count": 1, "total_bytes": 2, "manifest_sha256": sha256(canonical(manifest)).hexdigest()}}
            outcome = {"state": "succeeded", "result": result, "manifest": manifest}
            with override_settings(MERIDIAN_JOB_ARTIFACT_ROOT=root / "durable"):
                (root / "durable").mkdir()
                job_id, attempt_id = uuid.uuid4(), uuid.uuid4()
                self.assertTrue(publish_files(job_id, attempt_id, staging, outcome))
                saved = root / "durable" / str(job_id) / str(attempt_id)
                self.assertEqual((saved / "status.json").read_bytes(), b"{}")
                self.assertEqual(json.loads((saved / "publication-manifest.json").read_text()), manifest)
                (staging / "status.json").write_bytes(b"[]")
                with self.assertRaises(ValueError):
                    publish_files(job_id, uuid.uuid4(), staging, outcome)
                (staging / "status.json").unlink()
                (staging / "status.json").symlink_to(saved / "status.json")
                with self.assertRaises(OSError):
                    publish_files(job_id, uuid.uuid4(), staging, outcome)
                self.assertEqual(len(list((root / "durable" / str(job_id)).iterdir())), 1)

    def test_empty_failure_manifest_does_not_publish_a_result(self):
        self.assertFalse(publish_files(uuid.uuid4(), uuid.uuid4(), Path("unused"),
                                       {"state": "failed", "manifest": {"schema_version": 1, "files": {}}}))

    def test_tampered_publication_is_output_failure_without_retry(self):
        job = self.queued()

        def execute(specification, prepared, staging, **kwargs):
            staging.mkdir()
            (staging / "status.json").write_bytes(b"[]")
            manifest = {"schema_version": 1, "files": {"status.json": {
                "size_bytes": 2, "sha256": sha256(b"{}").hexdigest()}}}
            result = {"artifacts": {"file_count": 1, "total_bytes": 2,
                                    "manifest_sha256": sha256(canonical(manifest)).hexdigest()}}
            return {"state": "succeeded", "result": result, "manifest": manifest}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scratch").mkdir()
            (root / "artifacts").mkdir()
            with override_settings(MERIDIAN_JOB_SCRATCH_ROOT=root / "scratch",
                                   MERIDIAN_JOB_ARTIFACT_ROOT=root / "artifacts"):
                adapter = SimpleNamespace(execute_attempt=execute, cleanup_attempt=lambda attempt: True)
                # The HTTP/backend test environment intentionally has no NumPy.
                # Exercise publication with an adapter boundary test double.
                with patch("workspace.job_worker.LeaseGuard"), \
                     patch.dict("sys.modules", {"workspace.job_execution": adapter}):
                    self.assertTrue(run_one(uuid.uuid4(), threading.Event()))
            self.assertEqual(list((root / "scratch").iterdir()), [])
            self.assertEqual(list((root / "artifacts" / str(job.pk)).iterdir()), [])
        job.refresh_from_db()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.failure["code"], "numerical_failure")
        self.assertEqual(job.attempt_count, 1)
        self.assertIsNone(job.result)
        self.assertFalse(job.artifacts_available)
        self.assertIsNone(queue.claim(uuid.uuid4()))

    def test_blocked_database_heartbeat_cannot_block_local_lease_expiry(self):
        entered, release = threading.Event(), threading.Event()
        guard = LeaseGuard(uuid.uuid4(), uuid.uuid4(), threading.Event())
        guard.deadline = time.monotonic() + 30

        def blocked(*args):
            entered.set()
            release.wait(3)
            return "continue"

        with patch("workspace.job_queue.heartbeat", side_effect=blocked):
            thread = threading.Thread(target=guard._renew)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                guard.deadline = time.monotonic() - 1
                started = time.monotonic()
                with self.assertRaises(AttemptLeaseLost):
                    guard.checkpoint("running")
                self.assertLess(time.monotonic() - started, 0.5)
            finally:
                release.set()
                thread.join(timeout=4)
            self.assertEqual(guard.reason, "lost")
            self.assertFalse(thread.is_alive())

    def test_artifact_expiry_removes_files_but_preserves_metrics_and_attempts(self):
        self.queued()
        job, attempt = queue.claim(uuid.uuid4())
        queue.finish(job.pk, attempt.pk, self.outcome(), artifacts_available=True)
        Job.objects.filter(pk=job.pk).update(artifacts_expire_at=timezone.now() - timedelta(seconds=1))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scratch, artifacts = root / "scratch", root / "artifacts"
            scratch.mkdir()
            published = artifacts / str(job.pk) / str(attempt.pk)
            published.mkdir(parents=True)
            (published / "status.json").write_text("{}")
            with override_settings(MERIDIAN_JOB_SCRATCH_ROOT=scratch, MERIDIAN_JOB_ARTIFACT_ROOT=artifacts), \
                 patch.dict("sys.modules", {"workspace.job_execution": SimpleNamespace(cleanup_attempt=lambda _: True)}):
                collect_artifacts()
            self.assertFalse(published.exists())
        job.refresh_from_db()
        self.assertFalse(job.artifacts_available)
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(job.result, self.outcome()["result"])
        self.assertEqual(job.attempts.count(), 1)

    def test_scratch_gc_preserves_active_attempt_and_requires_confirmed_child_cleanup(self):
        self.queued()
        job, attempt = queue.claim(uuid.uuid4())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scratch, artifacts = root / "scratch", root / "artifacts"
            scratch.mkdir()
            artifacts.mkdir()
            active, orphan = scratch / str(attempt.pk), scratch / str(uuid.uuid4())
            active.mkdir()
            orphan.mkdir()
            (active / "partial.csv").write_text("unfinished")
            with override_settings(MERIDIAN_JOB_SCRATCH_ROOT=scratch, MERIDIAN_JOB_ARTIFACT_ROOT=artifacts):
                with patch.dict("sys.modules", {"workspace.job_execution": SimpleNamespace(cleanup_attempt=lambda _: False)}):
                    collect_artifacts()
                self.assertTrue(orphan.exists())
                with patch.dict("sys.modules", {"workspace.job_execution": SimpleNamespace(cleanup_attempt=lambda _: True)}):
                    collect_artifacts()
            self.assertFalse(orphan.exists())
            self.assertEqual((active / "partial.csv").read_text(), "unfinished")
