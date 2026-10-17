"""Private configuration snapshots, bounded writes, and optimistic concurrency."""
from copy import deepcopy
import json
from unittest.mock import patch
import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, override_settings

from meridian.vio_specification import build_specification, configuration_profile, specification_sha256

from .models import Experiment, ExperimentRevision
from .tests import DATASET_ID, WorkspaceCase


class ExperimentCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.project = self.create_project()
        self.url = f"/api/v1/projects/{self.project['id']}/experiments"

    def create_experiment(self, **changes):
        data = {"schema_version": 1, "name": "Baseline", "description": "Reference configuration",
                "dataset_id": DATASET_ID, "parameters": {"num_pts": 200}, **changes}
        result = self.mutate("post", self.url, data)
        self.assertEqual(result.status_code, 201, result.content)
        return result.json()

    def edit(self, value, **changes):
        current = value["experiment"]
        data = {"schema_version": 1, "base_version": current["version"], "name": current["name"],
                "description": current["description"], "parameters": current["parameters"], **changes}
        return self.mutate("patch", f"{self.url}/{current['id']}", data)

    def duplicate_experiment(self, value, version=1, name="Variant"):
        return self.mutate("post", f"{self.url}/{value['experiment']['id']}/duplicate",
                           {"schema_version": 1, "source_version": version, "name": name})


class ExperimentTests(ExperimentCase):
    def test_profile_is_authenticated_and_uses_the_canonical_contract(self):
        self.assert_error(Client().get("/api/v1/configuration-profile"), 401, "authentication_required")
        result = self.client.get("/api/v1/configuration-profile")
        self.assertEqual(result.json(), {"schema_version": 1, "profile": configuration_profile()})
        self.assert_error(self.mutate("post", "/api/v1/configuration-profile"), 405, "method_not_allowed")

    def test_create_normalizes_parameters_and_persists_the_canonical_snapshot(self):
        result = self.create_experiment(parameters={}, name="  Baseline  ")
        current, revision = result["experiment"], result["revision"]
        expected = build_specification(DATASET_ID, {})
        self.assertEqual(current["name"], "Baseline")
        self.assertEqual(current["parameters"], {"num_pts": 200})
        self.assertEqual(current["project_id"], self.project["id"])
        self.assertEqual(current["version"], 1)
        self.assertEqual(revision["specification"], expected)
        self.assertEqual(current["specification_sha256"], specification_sha256(expected))
        self.assertEqual(revision["specification_sha256"], current["specification_sha256"])
        self.assertIsNone(revision["copied_from"])
        self.assertEqual(Experiment.objects.get().parameters, {"num_pts": 200})
        self.assertEqual(ExperimentRevision.objects.get().specification, expected)
        listing = self.client.get(self.url).json()
        self.assertEqual(listing["experiments"], [current])
        self.assertEqual(listing["limits"], {"experiments": 20, "revisions": 50})
        detail = self.client.get(f"{self.url}/{current['id']}").json()
        self.assertEqual(detail, result)
        self.assertNotIn("result", detail)
        self.assertNotIn("job", detail)

    def test_optional_description_defaults_to_empty_text(self):
        data = {"schema_version": 1, "name": "Minimal", "dataset_id": DATASET_ID, "parameters": {}}
        result = self.mutate("post", self.url, data)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()["revision"]["description"], "")
        updated = self.edit(result.json(), name="Updated", description="Some notes")
        data = {"schema_version": 1, "base_version": 2, "name": "Updated", "parameters": {}}
        result = self.mutate("patch", f"{self.url}/{updated.json()['experiment']['id']}", data)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["revision"]["description"], "")

    def test_revision_history_remains_unchanged_after_configuration_and_metadata_edits(self):
        initial = self.create_experiment()
        original = deepcopy(initial["revision"])
        result = self.edit(initial, name="More features", description="Candidate", parameters={"num_pts": 400})
        self.assertEqual(result.status_code, 200, result.content)
        changed = result.json()
        self.assertEqual(changed["experiment"]["version"], 2)
        self.assertNotEqual(changed["revision"]["specification_sha256"], original["specification_sha256"])
        base = f"{self.url}/{initial['experiment']['id']}"
        self.assertEqual(self.client.get(base + "/revisions/1").json()["revision"], original)
        listing = self.client.get(base + "/revisions").json()["revisions"]
        self.assertEqual([entry["version"] for entry in listing], [2, 1])
        self.assertEqual([entry["parameters"]["num_pts"] for entry in listing], [400, 200])
        self.assertTrue(all("specification" not in entry for entry in listing))
        self.assertEqual(ExperimentRevision.objects.count(), 2)
        snapshot = ExperimentRevision.objects.get(version=1)
        snapshot.name = "Forbidden rewrite"
        with self.assertRaises(ValidationError):
            snapshot.save()
        self.assertEqual(self.client.get(base + "/revisions/1").json()["revision"], original)

    def test_metadata_edit_appends_revision_but_preserves_computational_hash(self):
        initial = self.create_experiment()
        updated = self.edit(initial, name="Renamed baseline").json()
        self.assertEqual(updated["experiment"]["version"], 2)
        self.assertEqual(updated["revision"]["specification"], initial["revision"]["specification"])
        self.assertEqual(updated["revision"]["specification_sha256"], initial["revision"]["specification_sha256"])

    def test_noop_returns_same_revision_and_timestamp_without_consuming_quota(self):
        initial = self.create_experiment()
        result = self.edit(initial, name=" Baseline ", parameters={})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["unchanged"])
        self.assertEqual(result.json()["experiment"], initial["experiment"])
        self.assertEqual(result.json()["revision"], initial["revision"])
        self.assertEqual(ExperimentRevision.objects.count(), 1)

    def test_stale_base_conflicts_even_when_submitted_values_equal_current_values(self):
        initial = self.create_experiment()
        changed = self.edit(initial, parameters={"num_pts": 100}).json()
        self.assert_error(self.edit(initial, parameters={"num_pts": 400}), 409, "version_conflict")
        self.assert_error(self.edit(changed, base_version=1), 409, "version_conflict")
        self.assertEqual(ExperimentRevision.objects.count(), 2)
        self.assertEqual(Experiment.objects.get().parameters, {"num_pts": 100})

    @override_settings(MERIDIAN_EXPERIMENT_LIMIT=1)
    def test_create_and_duplicate_share_project_quota_and_deletion_frees_a_slot(self):
        value = self.create_experiment()
        self.assert_error(self.duplicate_experiment(value), 409, "experiment_limit")
        data = {"schema_version": 1, "name": "Another", "dataset_id": DATASET_ID, "parameters": {}}
        self.assert_error(self.mutate("post", self.url, data), 409, "experiment_limit")
        response = self.mutate("delete", f"{self.url}/{value['experiment']['id']}")
        self.assertEqual(response.json(), {"schema_version": 1, "deleted": True})
        self.assertEqual(ExperimentRevision.objects.count(), 0)
        self.create_experiment(name="Replacement")
        self.assertEqual(Experiment.objects.count(), 1)

    @override_settings(MERIDIAN_REVISION_LIMIT=2)
    def test_revision_quota_preserves_history_and_allows_noop_at_limit(self):
        value = self.edit(self.create_experiment(), name="Revision two").json()
        self.assert_error(self.edit(value, name="Would exceed cap"), 409, "revision_limit")
        result = self.edit(value)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["unchanged"])
        self.assertEqual(Experiment.objects.get().current_version, 2)
        self.assertEqual(ExperimentRevision.objects.count(), 2)

    def test_duplicate_copies_selected_historical_snapshot_and_survives_source_deletion(self):
        original = self.create_experiment()
        source_url = f"{self.url}/{original['experiment']['id']}"
        self.edit(original, description="New notes", parameters={"num_pts": 400})
        response = self.duplicate_experiment(original, version=1, name="Copied baseline")
        self.assertEqual(response.status_code, 201)
        copied = response.json()
        self.assertNotEqual(copied["experiment"]["id"], original["experiment"]["id"])
        self.assertEqual(copied["experiment"]["version"], 1)
        self.assertEqual(copied["revision"]["description"], original["revision"]["description"])
        self.assertEqual(copied["revision"]["specification"], original["revision"]["specification"])
        self.assertEqual(copied["revision"]["copied_from"], {
            "experiment_id": original["experiment"]["id"], "version": 1,
            "specification_sha256": original["revision"]["specification_sha256"],
        })
        self.mutate("delete", source_url)
        result = self.client.get(f"{self.url}/{copied['experiment']['id']}")
        self.assertEqual(result.json(), copied)

    def test_failed_duplicate_snapshot_insert_leaves_no_partial_experiment(self):
        original = self.create_experiment()
        with patch("workspace.experiments.ExperimentRevision.objects.create", side_effect=RuntimeError("Insert failed")):
            with self.assertRaisesRegex(RuntimeError, "Insert failed"):
                self.duplicate_experiment(original)
        self.assertEqual(Experiment.objects.count(), 1)
        self.assertEqual(ExperimentRevision.objects.count(), 1)
        self.assertEqual(str(Experiment.objects.get().pk), original["experiment"]["id"])

    def test_revision_numbers_are_unique_and_positive_at_database_boundary(self):
        value = self.create_experiment()
        snapshot = ExperimentRevision.objects.get()
        for number in [0, 1]:
            with self.subTest(version=number):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    ExperimentRevision.objects.create(experiment_id=value["experiment"]["id"], version=number,
                                                      name="Duplicate", specification=snapshot.specification,
                                                      specification_sha256=snapshot.specification_sha256)
        self.assertEqual(ExperimentRevision.objects.count(), 1)

    def test_deleting_project_cascades_all_experiments_and_revisions(self):
        first = self.create_experiment()
        self.edit(first, parameters={"num_pts": 100})
        self.duplicate_experiment(first)
        self.assertEqual(ExperimentRevision.objects.count(), 3)
        response = self.mutate("delete", f"/api/v1/projects/{self.project['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Experiment.objects.exists())
        self.assertFalse(ExperimentRevision.objects.exists())

    def test_diff_reports_only_changed_fields_in_both_directions(self):
        first = self.create_experiment()
        self.edit(first, name="Variant", description="Different notes", parameters={"num_pts": 400})
        url = f"{self.url}/{first['experiment']['id']}/compare"
        changes = self.client.get(url + "?from=1&to=2").json()
        self.assertEqual(changes, {"schema_version": 1, "from_version": 1, "to_version": 2, "changes": [
            {"field": "name", "before": "Baseline", "after": "Variant"},
            {"field": "description", "before": "Reference configuration", "after": "Different notes"},
            {"field": "parameters.num_pts", "before": 200, "after": 400},
        ]})
        reverse = self.client.get(url + "?from=2&to=1").json()["changes"]
        self.assertEqual(reverse, [{"field": item["field"], "before": item["after"], "after": item["before"]}
                                   for item in changes["changes"]])
        self.assertEqual(self.client.get(url + "?from=1&to=1").json()["changes"], [])

    def test_diff_rejects_ambiguous_or_malformed_version_queries(self):
        value = self.create_experiment()
        url = f"{self.url}/{value['experiment']['id']}/compare"
        for query in ["", "?from=1", "?from=1&to=1&extra=x", "?from=1&from=2&to=1",
                      "?from=01&to=1", "?from=0&to=1", "?from=-1&to=1", "?from=1.0&to=1",
                      "?from=true&to=1", "?from=%20&to=1", "?from=1000&to=1"]:
            with self.subTest(query=query):
                self.assert_error(self.client.get(url + query), 400, "invalid_request")
        self.assert_error(self.client.get(url + "?from=1&to=2"), 404, "not_found")

    def test_invalid_create_shapes_types_and_identity_do_not_persist(self):
        baseline = {"schema_version": 1, "name": "Candidate", "description": "", "dataset_id": DATASET_ID,
                    "parameters": {"num_pts": 200}}
        invalid = [None, [], False, "text", {**baseline, "unexpected": "field"},
                   {key: value for key, value in baseline.items() if key != "parameters"}]
        for key, values in {
            "schema_version": [True, 1.0, "1", 0, 2, None],
            "name": [None, 1, [], "", "  ", "a" * 101, "nul\x00name", "bad\ud800name"],
            "description": [False, {}, "x" * 2001, "bad\udffftext", "nul\x00text"],
            "dataset_id": [None, "other-dataset", "/tmp/sensors", "https://example.org/dataset"],
            "parameters": [None, [], True, "config.yaml", {"num_pts": True}, {"num_pts": 200.0},
                           {"num_pts": "200"}, {"num_pts": 99}, {"num_pts": 201}, {"num_pts": 401},
                           {"num_pts": {"value": 200}}, {"num_pts": [200]}, {"path": "/etc/passwd"},
                           {"num_pts": 200, "runtime": "arbitrary"}],
        }.items():
            invalid.extend({**baseline, key: value} for value in values)
        for data in invalid:
            with self.subTest(data=repr(data)[:140]):
                token = self.csrf()
                result = self.client.post(self.url, data=json.dumps(data), content_type="application/json",
                                          HTTP_X_CSRFTOKEN=token)
                self.assertEqual(result.status_code, 400, result.content)
        self.assertFalse(Experiment.objects.exists())
        self.assertFalse(ExperimentRevision.objects.exists())

    def test_malformed_duplicate_nonfinite_and_oversized_json_are_rejected(self):
        raw = ["{", "null", '{"schema_version":1,"schema_version":1}',
               '{"schema_version":1,"name":"x","dataset_id":"' + DATASET_ID + '","parameters":{"num_pts":NaN}}',
               " " * 8193, b'\xff', b'{"name":"\xff"}']
        for data in raw:
            with self.subTest(data=repr(data)[:80]):
                result = self.client.post(self.url, data=data, content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf())
                self.assert_error(result, 400, "invalid_request")
        result = self.client.post(self.url, data="{}", content_type="text/plain", HTTP_X_CSRFTOKEN=self.csrf())
        self.assert_error(result, 400, "invalid_request")
        self.assertFalse(Experiment.objects.exists())

    def test_patch_rejects_server_fields_and_invalid_base_versions(self):
        first = self.create_experiment()
        for changes in [{"base_version": value} for value in [True, 1.0, "1", 0, -1, 51]] + [
            {"dataset_id": DATASET_ID}, {"copied_from": {}}, {"specification_sha256": "f" * 64},
            {"specification": {}}, {"parameters": {"num_pts": 101}},
        ]:
            with self.subTest(changes=changes):
                self.assertEqual(self.edit(first, **changes).status_code, 400)
        self.assertEqual(ExperimentRevision.objects.count(), 1)

    def test_duplicate_rejects_invalid_or_missing_source_revision_without_writing(self):
        first = self.create_experiment()
        url = f"{self.url}/{first['experiment']['id']}/duplicate"
        for source_version in [True, 1.0, "1", 0, -1, 51]:
            self.assert_error(self.mutate("post", url, {"schema_version": 1, "source_version": source_version,
                                                        "name": "Copy"}), 400, "invalid_request")
        self.assert_error(self.duplicate_experiment(first, version=2), 404, "not_found")
        self.assert_error(self.mutate("post", url, {"schema_version": 1, "source_version": 1,
                                                    "name": "Copy", "copied_from": {}}), 400, "invalid_request")
        self.assertEqual(Experiment.objects.count(), 1)

    def test_all_experiment_and_revision_routes_hide_other_owners_and_wrong_projects(self):
        first = self.create_experiment()
        another = self.create_project(name="Another project")
        beta = Client(enforce_csrf_checks=True)
        self.sign_in("engineer.beta", client=beta)
        base = f"{self.url}/{first['experiment']['id']}"
        for client, prefix in [(beta, self.url), (self.client, f"/api/v1/projects/{another['id']}/experiments")]:
            detail = f"{prefix}/{first['experiment']['id']}"
            for suffix in ["", "/revisions", "/revisions/1", "/compare?from=1&to=1"]:
                with self.subTest(path=detail + suffix):
                    self.assert_error(client.get(detail + suffix), 404, "not_found")
            for method, suffix in [("patch", ""), ("delete", ""), ("post", "/duplicate")]:
                self.assert_error(self.mutate(method, detail + suffix, {}, client=client), 404, "not_found")
        self.assert_error(beta.get(self.url), 404, "not_found")
        self.assert_error(self.mutate("post", self.url, {}, client=beta), 404, "not_found")
        self.assert_error(self.client.get(f"{self.url}/{uuid.uuid4()}"), 404, "not_found")
        self.assertEqual(self.client.get(base).json(), first)

    def test_authentication_csrf_and_revision_read_only_methods_are_enforced(self):
        first = self.create_experiment()
        base = f"{self.url}/{first['experiment']['id']}"
        anonymous = Client(enforce_csrf_checks=True)
        for url in [self.url, base, base + "/revisions", base + "/revisions/1", base + "/compare?from=1&to=1"]:
            self.assert_error(anonymous.get(url), 401, "authentication_required")
        self.assert_error(self.mutate("post", self.url, {}, client=anonymous), 401, "authentication_required")
        for method, url in [("post", self.url), ("patch", base), ("delete", base), ("post", base + "/duplicate")]:
            result = getattr(self.client, method)(url, data="{}", content_type="application/json")
            self.assert_error(result, 403, "csrf_failed")
        for method in ["post", "patch", "delete"]:
            for suffix in ["/revisions", "/revisions/1", "/compare?from=1&to=1"]:
                self.assert_error(self.mutate(method, base + suffix), 405, "method_not_allowed")
        self.assertEqual(ExperimentRevision.objects.count(), 1)

    def test_stored_revision_reads_do_not_rebuild_against_a_future_profile(self):
        initial = self.create_experiment()
        base = f"{self.url}/{initial['experiment']['id']}"
        with patch("workspace.experiments.build_specification", side_effect=AssertionError("Changed future profile")):
            self.assertEqual(self.client.get(base).json(), initial)
            self.assertEqual(self.client.get(base + "/revisions/1").json()["revision"], initial["revision"])
            copied = self.duplicate_experiment(initial)
            self.assertEqual(copied.status_code, 201)
            self.assertEqual(copied.json()["revision"]["specification"], initial["revision"]["specification"])
