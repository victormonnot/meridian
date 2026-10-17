"""Exercise real session/CSRF boundaries and private project persistence."""
from datetime import timedelta
import hashlib
from io import StringIO
import json
from unittest.mock import patch
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import identify_hasher
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .api import rate_budget
from .models import AuthWindow, Project


DATASET_ID = "tumvi-room1-512-16-v1"
PASSWORD = "Rosette!River-84-Valve"
SESSION_URL = "/api/v1/session"
PROJECTS_URL = "/api/v1/projects"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
LOGOUT_URL = "/api/v1/auth/logout"


class WorkspaceCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alpha = get_user_model().objects.create_user("engineer.alpha", password=PASSWORD)
        cls.beta = get_user_model().objects.create_user("engineer.beta", password=PASSWORD)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def csrf(self, client=None, secure=False):
        response = (client or self.client).get(SESSION_URL, secure=secure)
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def mutate(self, method, url, data=None, *, client=None, secure=False, **extra):
        client = client or self.client
        headers = {"HTTP_X_CSRFTOKEN": self.csrf(client, secure)}
        if secure:
            headers["HTTP_ORIGIN"] = "https://testserver"
        headers.update(extra)
        return getattr(client, method)(url, data=json.dumps(data or {}),
                                       content_type="application/json", secure=secure, **headers)

    def sign_in(self, username="engineer.alpha", *, client=None, secure=False):
        response = self.mutate("post", LOGIN_URL, {"username": username, "password": PASSWORD},
                               client=client, secure=secure)
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def create_project(self, *, client=None, name="Indoor VIO integration"):
        response = self.mutate("post", PROJECTS_URL,
                               {"name": name, "dataset_id": DATASET_ID}, client=client)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["project"]

    def assert_error(self, response, status, code=None):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response.json()["schema_version"], 1)
        if code:
            self.assertEqual(response.json()["error"]["code"], code)


class AuthenticationTests(WorkspaceCase):
    def test_anonymous_session_exposes_no_execution_or_private_data(self):
        response = self.client.get(SESSION_URL)
        data = response.json()
        self.assertIsNone(data["user"])
        self.assertTrue(data["csrf_token"])
        self.assertEqual(data["capabilities"], {"execution": False, "uploads": False})
        self.assertEqual(data["limits"]["projects"], settings.MERIDIAN_PROJECT_LIMIT)
        self.assertNotIn("projects", data)

    def test_registration_hashes_password_and_creates_database_session(self):
        response = self.mutate("post", REGISTER_URL,
                               {"username": "new.engineer", "password": PASSWORD})
        self.assertEqual(response.status_code, 201, response.content)
        user = get_user_model().objects.get(username="new.engineer")
        self.assertNotEqual(user.password, PASSWORD)
        self.assertEqual(identify_hasher(user.password).algorithm, "pbkdf2_sha256")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(response.json()["user"], {"id": user.pk, "username": user.username})
        self.assertNotIn(PASSWORD, response.content.decode())
        session_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(Session.objects.get(session_key=session_key).get_decoded()["_auth_user_id"], str(user.pk))

    def test_registration_uses_native_username_and_password_validation(self):
        invalid = [
            {"username": "space name", "password": PASSWORD},
            {"username": "", "password": PASSWORD},
            {"username": "x" * 151, "password": PASSWORD},
            {"username": "short.password", "password": "short"},
            {"username": "numeric.password", "password": "12345678901234567890"},
            {"username": "common.password", "password": "password123456"},
            {"username": "similar.account", "password": "similar.account"},
            {"username": "long.password", "password": "A" * 1025},
        ]
        for data in invalid:
            with self.subTest(username=data["username"]):
                self.assert_error(self.mutate("post", REGISTER_URL, data,
                                             client=Client(enforce_csrf_checks=True)), 400, "validation_error")
        self.assertEqual(get_user_model().objects.count(), 2)

    def test_duplicate_registration_does_not_replace_existing_password(self):
        self.assert_error(self.mutate("post", REGISTER_URL,
                                     {"username": self.alpha.username, "password": "Other!Valid-Key-962"}),
                          400, "validation_error")
        self.alpha.refresh_from_db()
        self.assertTrue(self.alpha.check_password(PASSWORD))

    @override_settings(MERIDIAN_REGISTRATION_OPEN=False)
    def test_registration_can_be_closed_without_disabling_login(self):
        self.assertFalse(self.client.get(SESSION_URL).json()["registration_open"])
        self.assert_error(self.mutate("post", REGISTER_URL,
                                     {"username": "new.engineer", "password": PASSWORD}),
                          403, "registration_closed")
        self.sign_in()

    def test_authenticated_registration_is_rejected(self):
        self.sign_in()
        self.assert_error(self.mutate("post", REGISTER_URL,
                                     {"username": "new.engineer", "password": PASSWORD}),
                          409, "already_authenticated")

    def test_wrong_password_unknown_user_and_inactive_account_share_failure(self):
        self.beta.is_active = False
        self.beta.save(update_fields=["is_active"])
        requests = [
            {"username": self.alpha.username, "password": "wrong-password"},
            {"username": "missing.engineer", "password": PASSWORD},
            {"username": self.beta.username, "password": PASSWORD},
        ]
        failures = [self.mutate("post", LOGIN_URL, data) for data in requests]
        for response in failures:
            self.assert_error(response, 401, "invalid_credentials")
        self.assertEqual(failures[0].json(), failures[1].json())
        self.assertEqual(failures[0].json(), failures[2].json())
        self.assertIsNone(self.client.get(SESSION_URL).json()["user"])

    def test_new_client_recovers_user_and_project_from_database_session(self):
        self.sign_in()
        project = self.create_project()
        another = Client(enforce_csrf_checks=True)
        another.cookies[settings.SESSION_COOKIE_NAME] = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(another.get(SESSION_URL).json()["user"]["id"], self.alpha.pk)
        self.assertEqual(another.get(PROJECTS_URL).json()["projects"], [project])

    def test_logout_deletes_server_session_and_old_cookie_cannot_be_replayed(self):
        self.sign_in()
        old_cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        response = self.mutate("post", LOGOUT_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["user"])
        self.assertFalse(Session.objects.filter(session_key=old_cookie).exists())
        replay = Client(enforce_csrf_checks=True)
        replay.cookies[settings.SESSION_COOKIE_NAME] = old_cookie
        self.assertIsNone(replay.get(SESSION_URL).json()["user"])
        self.assert_error(replay.get(PROJECTS_URL), 401, "authentication_required")

    def test_expired_database_session_cannot_access_workspace(self):
        self.sign_in()
        key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        Session.objects.filter(session_key=key).update(expire_date=timezone.now() - timedelta(seconds=1))
        self.assert_error(self.client.get(PROJECTS_URL), 401, "authentication_required")
        self.assertIsNone(self.client.get(SESSION_URL).json()["user"])

    def test_login_rotates_an_existing_session_and_csrf_secret(self):
        existing = self.client.session
        existing["pre_authentication"] = True
        existing.save()
        old_key = existing.session_key
        self.client.cookies[settings.SESSION_COOKIE_NAME] = old_key
        old_csrf = self.csrf()
        response = self.client.post(LOGIN_URL,
                                    data=json.dumps({"username": self.alpha.username, "password": PASSWORD}),
                                    content_type="application/json", HTTP_X_CSRFTOKEN=old_csrf)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value, old_key)
        self.assertFalse(Session.objects.filter(session_key=old_key).exists())
        self.assert_error(self.client.post(LOGOUT_URL, data="{}", content_type="application/json",
                                          HTTP_X_CSRFTOKEN=old_csrf), 403, "csrf_failed")


class RequestSecurityTests(WorkspaceCase):
    def test_registration_login_and_logout_require_csrf(self):
        for url in [REGISTER_URL, LOGIN_URL, LOGOUT_URL]:
            with self.subTest(url=url):
                self.assert_error(self.client.post(url, data=json.dumps({"username": "someone", "password": PASSWORD}),
                                                   content_type="application/json"), 403, "csrf_failed")
        self.assertEqual(get_user_model().objects.count(), 2)

    def test_cross_origin_requests_fail_even_with_valid_csrf_token(self):
        self.assert_error(self.mutate("post", LOGIN_URL,
                                     {"username": self.alpha.username, "password": PASSWORD},
                                     HTTP_ORIGIN="https://unrelated.example"), 403, "csrf_failed")
        self.sign_in()
        self.assert_error(self.mutate("post", PROJECTS_URL,
                                     {"name": "Blocked", "dataset_id": DATASET_ID},
                                     HTTP_ORIGIN="https://unrelated.example"), 403, "csrf_failed")
        self.assertEqual(Project.objects.count(), 0)

    def test_project_mutations_require_csrf_and_do_not_modify_on_rejection(self):
        self.sign_in()
        project = self.create_project()
        url = PROJECTS_URL + "/" + project["id"]
        for method, path, data in [("post", PROJECTS_URL, {"name": "Blocked", "dataset_id": DATASET_ID}),
                                   ("patch", url, {"name": "Blocked"}), ("delete", url, {})]:
            with self.subTest(method=method):
                response = getattr(self.client, method)(path, data=json.dumps(data), content_type="application/json")
                self.assert_error(response, 403, "csrf_failed")
        self.assertEqual(Project.objects.count(), 1)
        self.assertEqual(Project.objects.get().name, project["name"])

    def test_authentication_rejects_nontext_unknown_fields_and_schema(self):
        for url in [REGISTER_URL, LOGIN_URL]:
            for extra in [{"username": None}, {"password": 123}, {"username": ["engineer"]},
                          {"unknown": "value"}, {"schema_version": 2}]:
                with self.subTest(url=url, extra=extra):
                    data = {"username": "new.engineer", "password": PASSWORD, **extra}
                    self.assert_error(self.mutate("post", url, data), 400, "invalid_request")
        self.assertEqual(get_user_model().objects.count(), 2)

    def test_authentication_rejects_null_characters(self):
        for url in [REGISTER_URL, LOGIN_URL]:
            for field in ["username", "password"]:
                with self.subTest(url=url, field=field):
                    data = {"username": "new.engineer", "password": PASSWORD}
                    data[field] += "\x00"
                    self.assert_error(self.mutate("post", url, data), 400)

    def test_invalid_unicode_strings_are_rejected_before_hashing_or_persistence(self):
        for url in [REGISTER_URL, LOGIN_URL]:
            for field in ["username", "password"]:
                with self.subTest(url=url, field=field):
                    data = {"username": "new.engineer", "password": PASSWORD}
                    data[field] += "\ud800"
                    self.assert_error(self.mutate("post", url, data), 400)
        self.sign_in()
        self.assert_error(self.mutate("post", PROJECTS_URL,
                                     {"name": "Invalid \udfff", "dataset_id": DATASET_ID}), 400)
        self.assertEqual(Project.objects.count(), 0)

    def test_malformed_nonobject_oversized_and_wrong_content_type_requests_fail(self):
        for payload in ["{", "[]", "null", '"text"', '{"username":"' + "x" * 9000 + '"}']:
            with self.subTest(payload_length=len(payload)):
                response = self.client.post(REGISTER_URL, data=payload, content_type="application/json",
                                            HTTP_X_CSRFTOKEN=self.csrf())
                self.assert_error(response, 400, "invalid_request")
        response = self.client.post(REGISTER_URL, data={"username": "new.engineer", "password": PASSWORD},
                                    HTTP_X_CSRFTOKEN=self.csrf())
        self.assert_error(response, 400, "invalid_request")

    def test_api_versions_methods_and_path_substitution_cannot_expose_files(self):
        self.sign_in()
        project = self.create_project()
        for url in ["/api/v2/projects", PROJECTS_URL + "/not-a-uuid", "/api/v1/artifacts/trajectory.csv",
                    PROJECTS_URL + "/" + project["id"] + "/artifacts/trajectory.csv",
                    "/api/v1/datasets/../catalog/room1.json", "/api/v1/datasets/%2e%2e%2froom1.json",
                    "/outputs/vio-reference/room1-run-1/trajectory.csv", "/.personal/CONTEXT.md"]:
            with self.subTest(url=url):
                self.assert_error(self.client.get(url), 404, "not_found")
        response = self.mutate("put", PROJECTS_URL, {})
        self.assert_error(response, 405, "method_not_allowed")
        self.assertEqual(response["Allow"], "GET, POST")

    @override_settings(DEBUG=False, SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True,
                       SECURE_SSL_REDIRECT=True)
    def test_https_cookie_flags_and_response_privacy_headers(self):
        response = self.sign_in(secure=True)
        for name in [settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME]:
            cookie = response.cookies[name]
            self.assertTrue(cookie["secure"])
            self.assertTrue(cookie["httponly"])
            self.assertEqual(cookie["samesite"], "Lax")
            self.assertEqual(cookie["path"], "/")
            self.assertEqual(cookie["domain"], "")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        redirect = self.client.get(SESSION_URL)
        self.assertEqual(redirect.status_code, 301)
        self.assertEqual(redirect["Location"], "https://testserver" + SESSION_URL)


class ProjectTests(WorkspaceCase):
    def test_project_and_catalog_endpoints_require_authentication(self):
        for url in [PROJECTS_URL, PROJECTS_URL + "/" + str(uuid.uuid4()),
                    "/api/v1/datasets", "/api/v1/datasets/" + DATASET_ID]:
            with self.subTest(url=url):
                self.assert_error(self.client.get(url), 401, "authentication_required")
        for method, url in [("post", PROJECTS_URL), ("patch", PROJECTS_URL + "/" + str(uuid.uuid4())),
                            ("delete", PROJECTS_URL + "/" + str(uuid.uuid4()))]:
            with self.subTest(method=method):
                self.assert_error(self.mutate(method, url, {}), 401, "authentication_required")

    def test_two_accounts_can_only_list_read_update_and_delete_their_own_projects(self):
        self.sign_in()
        alpha_project = self.create_project(name="Alpha private project")
        beta_client = Client(enforce_csrf_checks=True)
        self.sign_in(self.beta.username, client=beta_client)
        beta_project = self.create_project(client=beta_client, name="Beta private project")
        self.assertEqual(self.client.get(PROJECTS_URL).json()["projects"], [alpha_project])
        self.assertEqual(beta_client.get(PROJECTS_URL).json()["projects"], [beta_project])
        for client, own, other in [(self.client, alpha_project, beta_project),
                                   (beta_client, beta_project, alpha_project)]:
            url = PROJECTS_URL + "/" + other["id"]
            missing_url = PROJECTS_URL + "/" + str(uuid.uuid4())
            with self.subTest(username=own["name"]):
                self.assertEqual(client.get(PROJECTS_URL + "/" + own["id"]).json()["project"], own)
                missing = client.get(missing_url)
                self.assert_error(client.get(url), 404, "not_found")
                self.assertEqual(client.get(url).json(), missing.json())
                self.assert_error(self.mutate("patch", url, {"name": "Stolen"}, client=client), 404, "not_found")
                self.assert_error(self.mutate("delete", url, {}, client=client), 404, "not_found")
        self.assertEqual(set(Project.objects.values_list("name", flat=True)),
                         {"Alpha private project", "Beta private project"})
        for client, own in [(self.client, alpha_project), (beta_client, beta_project)]:
            url = PROJECTS_URL + "/" + own["id"]
            updated = self.mutate("patch", url, {"name": "Updated", "description": "Private configuration notes"},
                                  client=client)
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["project"]["name"], "Updated")
            self.assertEqual(self.mutate("delete", url, {}, client=client).json()["deleted"], True)
        self.assertEqual(Project.objects.count(), 0)

    def test_project_metadata_persists_and_dataset_version_is_fixed(self):
        self.sign_in()
        project = self.create_project(name="  VIO baseline  ")
        self.assertEqual(project["name"], "VIO baseline")
        self.assertEqual(project["dataset_id"], DATASET_ID)
        stored = Project.objects.get(pk=project["id"])
        self.assertEqual(stored.owner_id, self.alpha.pk)
        self.assertEqual(stored.description, "")
        self.assertEqual(project["created_at"], stored.created_at.isoformat())
        self.assertEqual(project["updated_at"], stored.updated_at.isoformat())
        self.assertEqual(set(project), {"id", "name", "description", "dataset_id", "created_at", "updated_at"})
        url = PROJECTS_URL + "/" + project["id"]
        self.assert_error(self.mutate("patch", url, {"dataset_id": "room2"}), 400, "invalid_request")
        self.assertEqual(self.client.get(url).json()["project"], project)

    @override_settings(MERIDIAN_PROJECT_LIMIT=2)
    def test_project_limit_is_per_account_and_deletion_releases_capacity(self):
        self.sign_in()
        first = self.create_project()
        self.create_project()
        self.assert_error(self.mutate("post", PROJECTS_URL, {"name": "Third", "dataset_id": DATASET_ID}),
                          409, "project_limit")
        beta_client = Client(enforce_csrf_checks=True)
        self.sign_in(self.beta.username, client=beta_client)
        self.create_project(client=beta_client)
        self.assertEqual(Project.objects.filter(owner=self.alpha).count(), 2)
        self.mutate("delete", PROJECTS_URL + "/" + first["id"])
        self.create_project()
        self.assertEqual(Project.objects.filter(owner=self.alpha).count(), 2)

    def test_unknown_nontext_and_version_fields_are_rejected_without_writes(self):
        self.sign_in()
        base = {"name": "Valid", "dataset_id": DATASET_ID}
        for fields in [{"name": None}, {"description": ["notes"]}, {"dataset_id": 1},
                       {"owner": str(self.beta.pk)}, {"id": str(uuid.uuid4())}, {"schema_version": 2},
                       {"unknown": "value"}]:
            with self.subTest(fields=fields):
                self.assert_error(self.mutate("post", PROJECTS_URL, {**base, **fields}), 400, "invalid_request")
        self.assertEqual(Project.objects.count(), 0)
        project = self.create_project()
        url = PROJECTS_URL + "/" + project["id"]
        for fields in [{"name": None}, {"description": {}}, {"owner_id": str(self.beta.pk)}, {"schema_version": 2}]:
            with self.subTest(patch=fields):
                self.assert_error(self.mutate("patch", url, fields), 400, "invalid_request")
        self.assertEqual(self.client.get(url).json()["project"], project)

    def test_missing_fields_unknown_dataset_and_invalid_text_are_rejected(self):
        self.sign_in()
        for data in [{"name": "Missing dataset"}, {"dataset_id": DATASET_ID}, {}]:
            self.assert_error(self.mutate("post", PROJECTS_URL, data), 400, "invalid_request")
        for fields in [{"name": " "}, {"name": "x" * 101}, {"description": "x" * 2001},
                       {"dataset_id": "tumvi-room2-512-16-v1"}, {"name": "null\x00name"},
                       {"description": "null\x00description"}]:
            with self.subTest(fields=fields):
                self.assert_error(self.mutate("post", PROJECTS_URL,
                                             {"name": "Valid", "dataset_id": DATASET_ID, **fields}), 400)
        self.assertEqual(Project.objects.count(), 0)

    def test_execution_upload_and_artifact_routes_are_unavailable(self):
        self.sign_in()
        project = self.create_project()
        for suffix in ["runs", "execute", "uploads", "artifacts", "export"]:
            for prefix in ["/api/v1/", PROJECTS_URL + "/" + project["id"] + "/"]:
                with self.subTest(prefix=prefix, suffix=suffix):
                    self.assert_error(self.mutate("post", prefix + suffix, {}), 404, "not_found")
        self.assertEqual(Project.objects.count(), 1)
        self.assertEqual(self.client.get(SESSION_URL).json()["capabilities"], {"execution": False, "uploads": False})


class CatalogTests(WorkspaceCase):
    def test_authenticated_catalog_matches_public_qualification_without_raw_data(self):
        self.sign_in()
        response = self.client.get("/api/v1/datasets")
        self.assertEqual(response.status_code, 200)
        entries = response.json()["datasets"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], DATASET_ID)
        self.assertEqual(entry["schema_version"], 1)
        self.assertEqual(self.client.get("/api/v1/datasets/" + DATASET_ID).json()["dataset"], entry)
        public_path = settings.BASE_DIR / "catalog" / "room1.json"
        self.assertEqual(entry, json.loads(public_path.read_text()))
        repo = settings.BASE_DIR.parent
        summary_path = repo / entry["source_qualification_summary_url"]
        summary = json.loads(summary_path.read_text())
        self.assertEqual(hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                         entry["provenance"]["qualification_summary_sha256"])
        self.assertEqual(entry["provenance"]["archive_sha256"], summary["dataset"]["source"]["sha256"])
        self.assertEqual(entry["provenance"]["license"], summary["dataset"]["license"]["spdx"])
        self.assertEqual(entry["camera_pairs"], summary["dataset"]["streams"]["cam0"]["samples"])
        self.assertEqual(entry["imu_samples"], summary["dataset"]["streams"]["imu0"]["samples"])
        self.assertEqual(entry["reference"]["unsupported_camera_samples"], summary["reference_camera_samples_without_support"])
        self.assertEqual(entry["reference"]["coverage_fraction"],
                         entry["reference"]["supported_camera_samples"] / entry["camera_pairs"])
        for calibration in entry["calibration"]["files"]:
            self.assertEqual(hashlib.sha256((repo / calibration["url"]).read_bytes()).hexdigest(), calibration["sha256"])
        for name in ["first_camera_timestamp_ns", "last_camera_timestamp_ns"]:
            self.assertIsInstance(entry["timestamps"][name], str)
            self.assertGreater(int(entry["timestamps"][name]), 2 ** 53 - 1)
        self.assertEqual(entry["capabilities"]["supported"], ["stereo_vio"])
        self.assertEqual(entry["acquisition"], "handheld")
        self.assertIn("local", entry["capabilities"]["scope"])
        for forbidden in ["outputs/", "/home/", ".personal/", "download_url", "trajectory.csv"]:
            self.assertNotIn(forbidden, response.content.decode())

    def test_unknown_catalog_entry_and_unavailable_versions_return_404(self):
        self.sign_in()
        for name in ["tumvi-room1-512-16-v2", "tumvi-room2-512-16-v1", "unknown"]:
            self.assert_error(self.client.get("/api/v1/datasets/" + name), 404, "not_found")


class RateLimitTests(WorkspaceCase):
    def test_database_rate_budget_is_shared_and_resets_at_boundary(self):
        start = timezone.now()
        with patch("workspace.api.timezone.now", return_value=start):
            self.assertEqual(rate_budget("test", "private.identity", 2, 900), 0)
            self.assertEqual(rate_budget("test", "private.identity", 2, 900), 0)
            self.assertEqual(rate_budget("test", "private.identity", 2, 900), 900)
        window = AuthWindow.objects.get()
        self.assertEqual(window.count, 2)
        self.assertEqual(len(window.key), 64)
        self.assertNotIn("private.identity", window.key)
        with patch("workspace.api.timezone.now", return_value=start + timedelta(seconds=899, milliseconds=500)):
            self.assertEqual(rate_budget("test", "private.identity", 2, 900), 1)
        with patch("workspace.api.timezone.now", return_value=start + timedelta(seconds=900)):
            self.assertEqual(rate_budget("test", "private.identity", 2, 900), 0)
        window.refresh_from_db()
        self.assertEqual(window.count, 1)
        self.assertEqual(window.started_at, start + timedelta(seconds=900))

    def test_username_limit_spans_clients_peers_and_case_variants(self):
        start = timezone.now()
        with patch("workspace.api.timezone.now", return_value=start), patch("workspace.api.authenticate", return_value=None):
            for attempt in range(10):
                response = self.mutate("post", LOGIN_URL,
                                       {"username": "ENGINEER.ALPHA" if attempt % 2 else "engineer.alpha", "password": "wrong"},
                                       REMOTE_ADDR=f"192.0.2.{attempt + 1}")
                self.assert_error(response, 401, "invalid_credentials")
            another = Client(enforce_csrf_checks=True)
            limited = self.mutate("post", LOGIN_URL, {"username": "Engineer.Alpha", "password": "wrong"},
                                  client=another, REMOTE_ADDR="192.0.2.100")
            self.assert_error(limited, 429, "rate_limited")
            self.assertEqual(limited["Retry-After"], "900")

    def test_ingress_limit_ignores_spoofed_forwarded_for_and_blocks_authentication(self):
        start = timezone.now()
        with patch("workspace.api.timezone.now", return_value=start):
            for _ in range(100):
                self.assertEqual(rate_budget("ingress-login", "192.0.2.5", 100, 900), 0)
            with patch("workspace.api.authenticate") as authenticate:
                for forwarded in ["198.51.100.1", "203.0.113.9"]:
                    response = self.mutate("post", LOGIN_URL,
                                           {"username": self.alpha.username, "password": PASSWORD},
                                           REMOTE_ADDR="192.0.2.5", HTTP_X_FORWARDED_FOR=forwarded)
                    self.assert_error(response, 429, "rate_limited")
                    self.assertEqual(response["Retry-After"], "900")
                authenticate.assert_not_called()

    def test_registration_ingress_budget_is_separate_and_returns_retry_after(self):
        start = timezone.now()
        with patch("workspace.api.timezone.now", return_value=start):
            for _ in range(20):
                self.assertEqual(rate_budget("ingress-register", "127.0.0.1", 20, 3600), 0)
            response = self.mutate("post", REGISTER_URL, {"username": "new.engineer", "password": PASSWORD})
            self.assert_error(response, 429, "rate_limited")
            self.assertEqual(response["Retry-After"], "3600")
            self.sign_in()
        self.assertEqual(get_user_model().objects.count(), 2)

    def test_failed_registration_consumes_username_budget(self):
        start = timezone.now()
        with patch("workspace.api.timezone.now", return_value=start):
            for _ in range(10):
                self.assert_error(self.mutate("post", REGISTER_URL,
                                             {"username": "new.engineer", "password": "weak"}),
                                  400, "validation_error")
            response = self.mutate("post", LOGIN_URL, {"username": "new.engineer", "password": PASSWORD})
            self.assert_error(response, 429, "rate_limited")
            self.assertEqual(response["Retry-After"], "900")


class HealthTests(WorkspaceCase):
    def test_health_checks_database_without_revealing_details(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"schema_version": 1, "status": "ok"})
        with patch("workspace.api.connection.cursor", side_effect=RuntimeError("private database address")):
            failed = self.client.get("/api/health")
        self.assert_error(failed, 503, "unavailable")
        self.assertNotIn("private database address", failed.content.decode())

    def test_cleanup_removes_only_expired_sessions_and_old_authentication_windows(self):
        self.sign_in()
        project = self.create_project()
        active_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        now = timezone.now()
        Session.objects.create(session_key="expired-session", session_data="unused",
                               expire_date=now - timedelta(seconds=1))
        AuthWindow.objects.create(key="e" * 64, started_at=now - timedelta(days=1, seconds=1), count=10)
        AuthWindow.objects.create(key="a" * 64, started_at=now - timedelta(minutes=1), count=10)
        output = StringIO()
        call_command("cleanup_workspace", stdout=output)
        self.assertIn("Deleted 1 expired sessions and 1 inactive authentication counters.", output.getvalue())
        self.assertFalse(Session.objects.filter(session_key="expired-session").exists())
        self.assertTrue(Session.objects.filter(session_key=active_key).exists())
        self.assertFalse(AuthWindow.objects.filter(key="e" * 64).exists())
        self.assertTrue(AuthWindow.objects.filter(key="a" * 64).exists())
        self.assertEqual(self.client.get(PROJECTS_URL).json()["projects"], [project])
        repeated = StringIO()
        call_command("cleanup_workspace", stdout=repeated)
        self.assertIn("Deleted 0 expired sessions and 0 inactive authentication counters.", repeated.getvalue())
