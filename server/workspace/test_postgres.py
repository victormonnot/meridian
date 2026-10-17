"""PostgreSQL transaction races; SQLite cannot establish these guarantees."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
from queue import Queue
from threading import Barrier
from time import monotonic, sleep
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.test import Client, TransactionTestCase, override_settings

from .api import rate_budget
from .models import AuthWindow, Project


@skipUnless(connection.vendor == "postgresql", "Requires independent PostgreSQL transactions and row locks")
@override_settings(MERIDIAN_PROJECT_LIMIT=20)
class PostgreSQLConcurrencyTests(TransactionTestCase):
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
