"""PostgreSQL transaction races; SQLite cannot establish these guarantees."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
from queue import Queue
from threading import Barrier
from time import monotonic, sleep
from unittest import skipUnless
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.test import Client, RequestFactory, TransactionTestCase, override_settings

from .api import rate_budget
from .models import AuthWindow, Experiment, ExperimentRevision, Project


class ConcurrentDatabaseWorkers:
    """Use committed fixtures and real connections, with bounded lock waits."""

    @contextmanager
    def workers(self, operation):
        ready = Queue()
        start = Barrier(3)
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workspace-race")

        def run(index):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '10s'")
                    cursor.execute("SET lock_timeout = '6s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    ready.put(cursor.fetchone()[0])
                start.wait(timeout=5)
                return operation(index)
            finally:
                # Django connections are thread-local; close them in their owner.
                connections.close_all()

        futures = [pool.submit(run, index) for index in range(2)]
        try:
            pids = [ready.get(timeout=6) for _ in range(2)]
            self.assertEqual(len(set(pids)), 2)
            yield futures, pids, start
        finally:
            start.abort()
            # Every SQL wait and barrier in these workers has its own timeout.
            # Join before TransactionTestCase flushes or drops the test database.
            pool.shutdown(wait=True, cancel_futures=True)

    def wait_for_both_database_locks(self, pids, futures):
        deadline = monotonic() + 4
        while monotonic() < deadline:
            for future in futures:
                if future.done():
                    future.result()  # Surface the underlying worker failure first.
                    self.fail("A request completed before the owner lock was released.")
            with connection.cursor() as cursor:
                cursor.execute("SELECT DISTINCT pid FROM pg_locks WHERE NOT granted AND pid = ANY(%s)", [pids])
                waiting = {row[0] for row in cursor.fetchall()}
            if waiting == set(pids):
                return
            sleep(0.01)
        self.fail("Both PostgreSQL requests did not reach a lock wait within four seconds.")

@skipUnless(connection.vendor == "postgresql", "Requires independent PostgreSQL transactions and row locks")
@override_settings(MERIDIAN_PROJECT_LIMIT=20)
class PostgreSQLConcurrencyTests(ConcurrentDatabaseWorkers, TransactionTestCase):
    def test_concurrent_creations_cannot_both_claim_the_last_project_slot(self):
        password = "Rosette!River-84-Valve"
        owner = get_user_model().objects.create_user("concurrent.engineer", password=password)
        source_client = Client(enforce_csrf_checks=True)
        csrf_token = source_client.get("/api/v1/session").json()["csrf_token"]
        login_response = source_client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": owner.username, "password": password}),
            content_type="application/json", HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(login_response.status_code, 200, login_response.content)
        csrf_token = login_response.json()["csrf_token"]
        self.assertTrue(source_client.cookies[settings.SESSION_COOKIE_NAME].value)
        cookie_snapshot = deepcopy(source_client.cookies)
        Project.objects.bulk_create([
            Project(owner=owner, name=f"Existing project {index}", dataset_id="tumvi-room1-512-16-v1")
            for index in range(19)
        ])

        def create(index):
            client = Client(enforce_csrf_checks=True)
            client.cookies = deepcopy(cookie_snapshot)
            return client.post(
                "/api/v1/projects",
                data=json.dumps({"name": f"Concurrent project {index}", "dataset_id": "tumvi-room1-512-16-v1"}),
                content_type="application/json", HTTP_X_CSRFTOKEN=csrf_token,
            )

        with self.workers(create) as (futures, pids, start):
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=owner.pk)
                start.wait(timeout=5)
                # Both requests contend while the same last slot is available.
                self.wait_for_both_database_locks(pids, futures)
            responses = [future.result(timeout=12) for future in futures]

        self.assertEqual(sorted(response.status_code for response in responses), [201, 409],
                         [response.content for response in responses])
        rejected = next(response for response in responses if response.status_code == 409)
        self.assertEqual(rejected.json()["error"]["code"], "project_limit")
        created = next(response for response in responses if response.status_code == 201).json()["project"]
        self.assertTrue(Project.objects.filter(pk=created["id"], owner=owner).exists())
        self.assertEqual(Project.objects.filter(owner=owner).count(), 20)
        self.assertEqual(Project.objects.filter(owner=owner, name__startswith="Concurrent project").count(), 1)

    def test_concurrent_first_use_of_budget_consumes_only_one_available_attempt(self):
        self.assertFalse(AuthWindow.objects.exists())
        both_missed = Barrier(2)

        def consume(_index):
            first_read = True

            def synchronize_missing_lookup(execute, sql, params, many, context):
                nonlocal first_read
                result = execute(sql, params, many, context)
                if first_read and sql.lstrip().upper().startswith("SELECT") and '"workspace_authwindow"' in sql:
                    first_read = False
                    # Both initial SELECT results are empty before either INSERT.
                    # The real ORM then races on the unique key; no DB work is mocked.
                    both_missed.wait(timeout=5)
                return result

            with connection.execute_wrapper(synchronize_missing_lookup):
                return rate_budget("concurrent-first-use", "same-identity", limit=1, seconds=900)

        try:
            with self.workers(consume) as (futures, _pids, start):
                start.wait(timeout=5)
                waits = [future.result(timeout=12) for future in futures]
        finally:
            both_missed.abort()

        self.assertEqual(sum(wait == 0 for wait in waits), 1, waits)
        self.assertEqual(sum(wait > 0 for wait in waits), 1, waits)
        self.assertEqual(AuthWindow.objects.count(), 1)
        self.assertEqual(AuthWindow.objects.get().count, 1)


@skipUnless(connection.vendor == "postgresql", "Requires independent PostgreSQL transactions and row locks")
@override_settings(MERIDIAN_EXPERIMENT_LIMIT=2, MERIDIAN_REVISION_LIMIT=2)
class PostgreSQLExperimentConcurrencyTests(ConcurrentDatabaseWorkers, TransactionTestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("concurrent.experiments", password="Rosette!River-84-Valve")
        self.project = Project.objects.create(owner=self.owner, name="Concurrent configurations",
                                              dataset_id="tumvi-room1-512-16-v1")
        self.url = f"/api/v1/projects/{self.project.pk}/experiments"
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.csrf_token = client.get("/api/v1/session").json()["csrf_token"]
        self.cookies = deepcopy(client.cookies)
        created = self.send("post", self.url, self.creation("Baseline"))
        self.assertEqual(created.status_code, 201, created.content)
        self.baseline = created.json()["experiment"]

    def creation(self, name):
        return {"schema_version": 1, "name": name, "dataset_id": self.project.dataset_id, "parameters": {}}

    def send(self, method, url, data):
        client = Client(enforce_csrf_checks=True)
        client.cookies = deepcopy(self.cookies)
        return getattr(client, method)(url, data=json.dumps(data), content_type="application/json",
                                       HTTP_X_CSRFTOKEN=self.csrf_token)

    def race_under_project_lock(self, operation):
        with self.workers(operation) as (futures, pids, start):
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                start.wait(timeout=5)
                self.wait_for_both_database_locks(pids, futures)
            return [future.result(timeout=12) for future in futures]

    def test_simultaneous_edits_from_same_base_append_exactly_one_revision(self):
        def edit(index):
            return self.send("patch", f"{self.url}/{self.baseline['id']}", {
                "schema_version": 1, "base_version": 1, "name": f"Candidate {index}",
                "parameters": {"num_pts": 100 if index == 0 else 400},
            })

        results = self.race_under_project_lock(edit)
        self.assertEqual(sorted(result.status_code for result in results), [200, 409],
                         [result.content for result in results])
        conflict = next(result for result in results if result.status_code == 409)
        self.assertEqual(conflict.json()["error"]["code"], "version_conflict")
        accepted = next(result for result in results if result.status_code == 200).json()
        self.assertEqual(Experiment.objects.get().current_version, 2)
        self.assertEqual(Experiment.objects.get().parameters, accepted["experiment"]["parameters"])
        self.assertEqual(list(ExperimentRevision.objects.values_list("version", flat=True)), [2, 1])
        self.assertEqual(ExperimentRevision.objects.get(version=1).specification["parameters"], {"num_pts": 200})

    def test_create_and_duplicate_cannot_both_claim_last_experiment_slot(self):
        def create(index):
            if index == 0:
                return self.send("post", self.url, self.creation("New experiment"))
            return self.send("post", f"{self.url}/{self.baseline['id']}/duplicate",
                             {"schema_version": 1, "source_version": 1, "name": "Copied experiment"})

        results = self.race_under_project_lock(create)
        self.assertEqual(sorted(result.status_code for result in results), [201, 409],
                         [result.content for result in results])
        rejected = next(result for result in results if result.status_code == 409)
        self.assertEqual(rejected.json()["error"]["code"], "experiment_limit")
        self.assertEqual(Experiment.objects.filter(project=self.project).count(), 2)
        self.assertEqual(ExperimentRevision.objects.count(), 2)

    def test_project_deletion_serializes_with_experiment_creation_and_cascades(self):
        def mutate(index):
            if index == 0:
                return self.send("delete", f"/api/v1/projects/{self.project.pk}", {})
            return self.send("post", self.url, self.creation("Concurrent child"))

        results = self.race_under_project_lock(mutate)
        self.assertEqual(results[0].status_code, 200, results[0].content)
        self.assertIn(results[1].status_code, [201, 404], results[1].content)
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())
        self.assertFalse(Experiment.objects.exists())
        self.assertFalse(ExperimentRevision.objects.exists())


@skipUnless(connection.vendor == "postgresql", "Requires independent PostgreSQL transactions and row locks")
@override_settings(MERIDIAN_EXECUTION_ENABLED=True)
class PostgreSQLJobConcurrencyTests(ConcurrentDatabaseWorkers, TransactionTestCase):
    def setUp(self):
        from meridian.vio_specification import build_specification, specification_sha256
        from .models import ComputeQueue
        self.owner = get_user_model().objects.create_user("compute.engineer")
        self.project = Project.objects.create(owner=self.owner, name="Compute race", dataset_id="tumvi-room1-512-16-v1")
        spec = build_specification(self.project.dataset_id, {"num_pts": 200})
        digest = specification_sha256(spec)
        self.experiment = Experiment.objects.create(project=self.project, name="Baseline", parameters=spec["parameters"], specification_sha256=digest)
        ExperimentRevision.objects.create(experiment=self.experiment, version=1, name="Baseline", specification=spec, specification_sha256=digest)
        ComputeQueue.objects.get_or_create(pk=1)

    def submit(self, key):
        from .jobs import jobs
        request = RequestFactory().post("/jobs", data=json.dumps({"schema_version": 1, "experiment_id": str(self.experiment.pk),
                                        "revision_version": 1, "idempotency_key": str(key)}), content_type="application/json")
        request.user = self.owner
        return jobs(request, self.project.pk)

    def race_on_queue(self, operation):
        from .models import ComputeQueue
        with self.workers(operation) as (futures, pids, start):
            with transaction.atomic():
                ComputeQueue.objects.select_for_update().get(pk=1)
                start.wait(timeout=5)
                self.wait_for_both_database_locks(pids, futures)
            return [future.result(timeout=12) for future in futures]

    def test_simultaneous_same_submission_creates_one_job(self):
        from .models import Job
        key = uuid.uuid4()
        results = self.race_on_queue(lambda _: self.submit(key))
        self.assertEqual(sorted(result.status_code for result in results), [200, 201])
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(len({json.loads(result.content)["job"]["id"] for result in results}), 1)

    def test_simultaneous_submissions_cannot_exceed_last_account_slot(self):
        from .models import Job
        self.assertEqual(self.submit(uuid.uuid4()).status_code, 201)
        results = self.race_on_queue(lambda _: self.submit(uuid.uuid4()))
        self.assertEqual(sorted(result.status_code for result in results), [201, 409])
        self.assertEqual(Job.objects.filter(state="queued").count(), 2)

    def test_two_workers_cannot_claim_two_jobs_in_the_global_slot(self):
        from .job_queue import claim
        from .models import JobAttempt
        self.submit(uuid.uuid4())
        self.submit(uuid.uuid4())
        results = self.race_on_queue(lambda _: claim(uuid.uuid4()))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(JobAttempt.objects.count(), 1)

    def test_cancel_and_finish_are_serialized_without_late_success(self):
        from .job_queue import claim, finish
        from .jobs import cancel
        self.submit(uuid.uuid4())
        job, attempt = claim(uuid.uuid4())

        def operation(index):
            if index:
                return finish(job.pk, attempt.pk, {"state": "succeeded", "failure": None,
                    "result": {"qualification": {"passed": True}},
                    "manifest": {"schema_version": 1, "files": {"status.json": {"size_bytes": 2, "sha256": "a" * 64}}}})
            request = RequestFactory().post("/cancel", data='{"schema_version":1}', content_type="application/json")
            request.user = self.owner
            return cancel(request, self.project.pk, job.pk)

        results = self.race_on_queue(operation)
        self.assertEqual(results[0].status_code, 200)
        self.assertTrue(results[1])
        job.refresh_from_db()
        cancelled_first = json.loads(results[0].content)["job"]["cancel_requested"]
        self.assertEqual(job.state, "cancelled" if cancelled_first else "succeeded")
        if cancelled_first:
            self.assertIsNone(job.result)
