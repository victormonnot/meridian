"""HTTP admission boundaries with explicitly synthetic retained-job fixtures."""
import uuid

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import AuthWindow, Experiment, ExperimentRevision, Job, Project
from .test_experiments import ExperimentCase


@override_settings(MERIDIAN_EXECUTION_ENABLED=True)
class AcceptanceLimitTests(ExperimentCase):
    def setUp(self):
        super().setUp()
        self.saved = self.create_experiment()
        self.experiment = Experiment.objects.get(pk=self.saved["experiment"]["id"])
        self.jobs_url = f"/api/v1/projects/{self.project['id']}/jobs"

    def submit(self, key=None):
        return self.mutate("post", self.jobs_url, {
            "schema_version": 1, "experiment_id": str(self.experiment.pk),
            "revision_version": 1, "idempotency_key": str(key or uuid.uuid4())})

    def create_jobs(self, owner, project, experiment, count, state="cancelled"):
        revision = experiment.revisions.get(version=1)
        return Job.objects.bulk_create([
            Job(owner=owner, project=project, experiment=experiment,
                experiment_name=experiment.name, revision_version=1,
                specification=revision.specification,
                specification_sha256=revision.specification_sha256,
                idempotency_key=uuid.uuid4(), state=state,
                phase="queued" if state == "queued" else "complete")
            for _ in range(count)])

    def peer(self, number):
        owner = get_user_model().objects.create_user(f"budget.peer.{number}")
        project = Project.objects.create(owner=owner, name="Synthetic quota fixture",
                                         dataset_id=self.project["dataset_id"])
        experiment = Experiment.objects.create(project=project, name="Baseline",
            parameters={"num_pts": 200}, specification_sha256=self.experiment.specification_sha256)
        original = self.experiment.revisions.get(version=1)
        ExperimentRevision.objects.create(experiment=experiment, version=1, name="Baseline",
            specification=original.specification, specification_sha256=original.specification_sha256)
        return owner, project, experiment

    def test_account_retained_boundary_and_deletion_release_one_slot(self):
        fixtures = self.create_jobs(self.alpha, self.experiment.project, self.experiment, 19)
        self.assertEqual(self.submit().status_code, 201)
        self.assert_error(self.submit(), 409, "job_limit")
        self.assertEqual(Job.objects.filter(owner=self.alpha).count(), 20)
        self.assertEqual(self.mutate("delete", f"{self.jobs_url}/{fixtures[0].pk}").status_code, 200)
        self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(Job.objects.filter(owner=self.alpha).count(), 20)

    def test_global_retained_boundary_applies_below_account_limit(self):
        fixtures = []
        for index in range(10):
            fixtures += self.create_jobs(*self.peer(index), 19 if index == 9 else 20)
        self.assertEqual(Job.objects.count(), 199)
        self.assertEqual(self.submit().status_code, 201)
        self.assert_error(self.submit(), 409, "job_limit")
        self.assertEqual(Job.objects.filter(owner=self.alpha).count(), 1)
        fixtures[0].delete()
        self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(Job.objects.count(), 200)

    def test_global_active_boundary_applies_below_account_limit(self):
        for index, count in enumerate((2, 2, 2, 1)):
            self.create_jobs(*self.peer(index), count, state="queued")
        admitted = self.submit()
        self.assertEqual(admitted.status_code, 201)
        self.assert_error(self.submit(), 409, "queue_full")
        self.assertEqual(Job.objects.filter(owner=self.alpha).count(), 1)
        identifier = admitted.json()["job"]["id"]
        self.assertEqual(self.mutate("post", f"{self.jobs_url}/{identifier}/cancel",
                                    {"schema_version": 1}).status_code, 200)
        self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(Job.objects.filter(state="queued").count(), 8)

    def test_global_daily_boundary_is_not_reset_by_deleted_jobs_or_idempotent_retry(self):
        key = salted_hmac("meridian.compute-window", "global", algorithm="sha256").hexdigest()
        AuthWindow.objects.create(pk=key, started_at=timezone.now(), count=99)
        idempotency = uuid.uuid4()
        admitted = self.submit(idempotency)
        self.assertEqual(admitted.status_code, 201)
        rejected = self.submit()
        self.assert_error(rejected, 429, "submission_limit")
        self.assertTrue(0 < int(rejected["Retry-After"]) <= 86400)
        retry = self.submit(idempotency)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.json()["reused"])
        self.assertEqual(retry.json()["job"]["id"], admitted.json()["job"]["id"])
        identifier = admitted.json()["job"]["id"]
        self.mutate("post", f"{self.jobs_url}/{identifier}/cancel", {"schema_version": 1})
        self.mutate("delete", f"{self.jobs_url}/{identifier}")
        self.assert_error(self.submit(), 429, "submission_limit")
        self.assertEqual(AuthWindow.objects.get(pk=key).count, 100)
        self.assertEqual(Job.objects.count(), 0)
