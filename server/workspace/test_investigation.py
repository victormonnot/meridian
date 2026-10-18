"""Private evidence reads: owner, retention, immutable identity and file safety."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
from meridian import vio_investigation_metrics

from .models import Job
from .test_experiments import ExperimentCase


class InvestigationTests(ExperimentCase):
    def setUp(self):
        super().setUp()
        experiment = self.create_experiment()
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts, self.cameras = self.root / "artifacts", self.root / "cameras"
        self.artifacts.mkdir()
        self.cameras.mkdir()
        self.settings_override = override_settings(MERIDIAN_JOB_ARTIFACT_ROOT=self.artifacts,
                                                   MERIDIAN_CAMERA_ROOT=self.cameras)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        spec = experiment["revision"]["specification"]
        self.job = Job.objects.create(
            owner=self.alpha, project_id=self.project["id"], experiment_id=experiment["experiment"]["id"],
            experiment_name="Retained run", revision_version=1, specification=spec,
            specification_sha256=experiment["revision"]["specification_sha256"],
            idempotency_key=uuid.uuid4(), state="succeeded", phase="succeeded", attempt_count=1,
            published_attempt=uuid.uuid4(), artifacts_available=True,
            artifacts_expire_at=timezone.now() + timedelta(days=7),
            result={"metrics": "persistent metadata"}, artifact_manifest={"schema_version": 1, "files": {}})
        self.directory = self.artifacts / str(self.job.pk) / str(self.job.published_attempt)
        self.directory.mkdir(parents=True)
        self.url = f"/api/v1/projects/{self.job.project_id}/jobs/{self.job.pk}/investigation"
        self.first = int(spec["interval"]["first_camera_timestamp_ns"])
        self.last = int(spec["interval"]["last_camera_timestamp_ns"])
        common = {"schema_version": 1, "specification_sha256": self.job.specification_sha256,
                  "dataset_manifest_sha256": spec["dataset"]["prepared_manifest_sha256"]}
        self.evidence = {
            **common, "run_state": "complete",
            "time": {"origin_timestamp_ns": str(self.first), "first_timestamp_ns": str(self.first),
                     "last_timestamp_ns": str(self.last)},
            "alignment": {"scale": 1}, "protocol": {"alignment": "global_se3"},
            "provenance": {"interval_scorer_sha256": sha256(Path(vio_investigation_metrics.__file__).read_bytes()).hexdigest()},
            "frames": [
                {"index": i, "timestamp_ns": str(stamp), "estimated": error is not None,
                 "reference_supported": True, "associated": error is not None,
                 "evaluated": error is not None, "position_error_m": error,
                 "orientation_error_deg": error}
                for i, (stamp, error) in enumerate(((self.first, 1), (self.first + 10, None),
                                                    (self.first + 20, 3), (self.last, 4)))],
        }
        # These bytes test the transport boundary only, not PNG decoding. Actual
        # 512x512 recordings are verified separately by the real browser checks.
        self.png = b"\x89PNG\r\n\x1a\n" + b"synthetic transport fixture"
        self.image_index = {**common, "cameras": {}}
        for camera in ("cam0", "cam1"):
            (self.cameras / camera).mkdir()
            (self.cameras / camera / f"{self.first}.png").write_bytes(self.png)
            self.image_index["cameras"][camera] = [
                {"index": i, "timestamp_ns": str(self.first + i * 50_000_000),
                 "relative_path": f"sensors/mav0/{camera}/data/{self.first + i * 50_000_000}.png",
                 "size_bytes": len(self.png), "sha256": sha256(self.png).hexdigest()}
                for i in range(spec["interval"]["camera_pairs"])]
        self.publish("investigation.json", self.evidence)
        self.publish("image-index.json", self.image_index)

    def publish(self, name, value):
        data = json.dumps(value).encode() if not isinstance(value, bytes) else value
        (self.directory / name).write_bytes(data)
        self.job.artifact_manifest["files"][name] = {"sha256": sha256(data).hexdigest(), "size_bytes": len(data)}
        self.job.save(update_fields=["artifact_manifest"])

    def image_url(self, camera="cam0", index=0):
        return f"{self.url}/images/{camera}/{index}"

    def test_owner_reads_exact_evidence_without_float_timestamp_roundoff(self):
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["investigation"], self.evidence)
        self.assertEqual(result.json()["investigation"]["frames"][0]["timestamp_ns"], str(self.first))
        self.assertEqual(result["Cache-Control"], "no-store")
        self.assertIn("Cookie", result["Vary"])
        detail = self.client.get(self.url.removesuffix("/investigation")).json()["job"]
        self.assertTrue(detail["investigation_available"])

    def test_camera_response_is_exact_png_bytes_with_verified_identity(self):
        for camera in ("cam0", "cam1"):
            result = self.client.get(self.image_url(camera))
            self.assertEqual(result.status_code, 200, result.content)
            self.assertEqual(result.content, self.png)
            self.assertEqual(result["Content-Type"], "image/png")
            self.assertEqual(result["X-Content-SHA256"], sha256(self.png).hexdigest())
            self.assertEqual(result["X-Timestamp-Ns"], str(self.first))
            self.assertEqual(result["Cache-Control"], "no-store")
            self.assertIn("Cookie", result["Vary"])

    def test_interval_scores_full_resolution_support_with_global_alignment(self):
        result = self.client.get(self.url + "/metrics", {"start_ns": str(self.first), "end_ns": str(self.first + 20)})
        self.assertEqual(result.status_code, 200, result.content)
        interval = result.json()["interval"]
        self.assertEqual(interval["counts"]["expected_camera_samples"], 3)
        self.assertEqual(interval["counts"]["evaluated_camera_samples"], 2)
        self.assertEqual(interval["coverage_fraction"], 2 / 3)
        self.assertAlmostEqual(interval["metrics"]["ate_rmse_m"], math.sqrt(5))
        self.assertEqual(interval["alignment_scope"], "full_recording_global_se3_unchanged")

    def test_empty_interval_has_null_metrics_and_no_fabricated_support(self):
        result = self.client.get(self.url + "/metrics", {"start_ns": str(self.first + 1), "end_ns": str(self.first + 2)})
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["interval"]["status"], "no_camera_samples")
        self.assertIsNone(result.json()["interval"]["metrics"])
        self.assertIsNone(result.json()["interval"]["coverage_fraction"])

    def test_all_routes_authenticate_before_opening_any_file(self):
        peer = get_user_model().objects.create_user("investigation.peer", password="Separate!Account-2468")
        other = Client()
        other.force_login(peer)
        routes = (self.url, self.url + "/metrics", self.image_url())
        with patch("workspace.investigation._read") as read:
            for url in routes:
                self.assert_error(Client().get(url), 401, "authentication_required")
                self.assert_error(other.get(url), 404, "not_found")
                wrong_project = url.replace(str(self.job.project_id), str(uuid.uuid4()))
                self.assert_error(self.client.get(wrong_project), 404, "not_found")
            read.assert_not_called()

    def test_retention_is_enforced_before_collector_removes_files(self):
        self.job.artifacts_expire_at = timezone.now() - timedelta(seconds=1)
        self.job.save(update_fields=["artifacts_expire_at"])
        for url in (self.url, self.url + "/metrics", self.image_url()):
            self.assert_error(self.client.get(url), 410, "artifacts_expired")
        detail = self.client.get(self.url.removesuffix("/investigation")).json()["job"]
        self.assertFalse(detail["investigation_available"])
        self.assertEqual(detail["result"], {"metrics": "persistent metadata"})
        self.assertTrue((self.directory / "investigation.json").exists())

    def test_deleted_job_cannot_serve_retained_bytes(self):
        self.job.delete()
        self.assertTrue((self.directory / "investigation.json").exists())
        for url in (self.url, self.url + "/metrics", self.image_url()):
            self.assert_error(self.client.get(url), 404, "not_found")

    def test_old_and_unpublished_executions_have_explicit_unavailable_state(self):
        del self.job.artifact_manifest["files"]["investigation.json"]
        self.job.save(update_fields=["artifact_manifest"])
        self.assert_error(self.client.get(self.url), 409, "investigation_unavailable")
        self.publish("investigation.json", self.evidence)
        for state in ("queued", "running", "evaluating", "cancelled"):
            self.job.state = state
            self.job.save(update_fields=["state"])
            self.assert_error(self.client.get(self.url), 409, "investigation_unavailable")

    def test_failed_run_can_expose_validated_partial_evidence_without_success(self):
        self.job.state, self.job.result = "failed", None
        self.job.failure = {"code": "initialization_failure", "message": "Initialization failed."}
        self.job.save(update_fields=["state", "result", "failure"])
        self.evidence["run_state"] = "partial"
        self.publish("investigation.json", self.evidence)
        self.assertEqual(self.client.get(self.url).json()["investigation"]["run_state"], "partial")
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, "failed")
        self.assertIsNone(self.job.result)

    def test_completion_state_cannot_be_falsified_by_artifact(self):
        self.evidence["run_state"] = "partial"
        self.publish("investigation.json", self.evidence)
        self.assert_error(self.client.get(self.url), 503, "artifact_unavailable")

    def test_interval_scorer_must_match_recorded_source_identity(self):
        self.evidence["provenance"]["interval_scorer_sha256"] = "0" * 64
        self.publish("investigation.json", self.evidence)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        result = self.client.get(self.url + "/metrics", {"start_ns": str(self.first), "end_ns": str(self.last)})
        self.assert_error(result, 503, "artifact_unavailable")

    def test_interval_bounds_are_strict_unique_and_within_recording(self):
        queries = ["", f"start_ns={self.first}", f"start_ns={self.first}&end_ns={self.last}&path=private",
                   f"start_ns={self.first}&start_ns={self.first}&end_ns={self.last}",
                   f"start_ns={self.first}.0&end_ns={self.last}",
                   f"start_ns=0{self.first}&end_ns={self.last}",
                   f"start_ns={self.first-1}&end_ns={self.last}",
                   f"start_ns={self.last}&end_ns={self.first}",
                   f"start_ns=true&end_ns={self.last}", f"start_ns={2**80}&end_ns={2**80}"]
        for query in queries:
            with self.subTest(query=query):
                self.assert_error(self.client.get(self.url + "/metrics?" + query), 400, "invalid_request")

    def test_camera_addresses_cannot_select_unlisted_files(self):
        for url in (self.image_url("reference"), self.image_url(index=2821), self.image_url(index=999999),
                    self.url + "/images/cam0/../reference.csv"):
            self.assert_error(self.client.get(url), 404, "not_found")
        self.assert_error(self.client.get(self.image_url() + "?path=private"), 400, "invalid_request")
        self.assert_error(self.client.get(self.url + "?path=private"), 400, "invalid_request")

    def test_tampered_and_missing_artifacts_do_not_leak_local_paths(self):
        path = self.directory / "investigation.json"
        original = path.read_bytes()
        path.write_bytes(original.replace(b'"complete"', b'"modified"'))
        result = self.client.get(self.url)
        self.assert_error(result, 503, "artifact_unavailable")
        self.assertNotIn(str(self.root), result.content.decode())
        path.unlink()
        self.assert_error(self.client.get(self.url), 503, "artifact_unavailable")

    def test_wrong_specification_dataset_or_schema_is_rejected(self):
        for changes in ({"schema_version": True}, {"schema_version": 2},
                        {"specification_sha256": "0" * 64}, {"dataset_manifest_sha256": "0" * 64}):
            with self.subTest(changes=changes):
                self.publish("investigation.json", {**self.evidence, **changes})
                self.assert_error(self.client.get(self.url), 503, "artifact_unavailable")

    def test_duplicate_json_keys_and_nonfinite_json_are_rejected(self):
        for data in (b'{"schema_version":1,"schema_version":1}', b'{"value":NaN}'):
            self.publish("investigation.json", data)
            self.assert_error(self.client.get(self.url), 503, "artifact_unavailable")

    def test_budget_rejects_file_before_open_and_read(self):
        self.job.artifact_manifest["files"]["investigation.json"]["size_bytes"] = 32 * 1024 * 1024 + 1
        self.job.save(update_fields=["artifact_manifest"])
        with patch("workspace.investigation.os.open") as opened:
            self.assert_error(self.client.get(self.url), 503, "artifact_unavailable")
            opened.assert_not_called()

    def test_artifact_and_attempt_symlinks_are_rejected(self):
        target = self.root / "outside.json"
        target.write_bytes((self.directory / "investigation.json").read_bytes())
        artifact = self.directory / "investigation.json"
        artifact.unlink()
        artifact.symlink_to(target)
        self.assert_error(self.client.get(self.url), 503, "artifact_unavailable")
        artifact.unlink()
        artifact.write_bytes(target.read_bytes())
        relocated = self.root / "relocated"
        self.directory.rename(relocated)
        self.directory.symlink_to(relocated, target_is_directory=True)
        self.assert_error(self.client.get(self.url), 503, "artifact_unavailable")

    def test_camera_symlink_hardlink_and_fifo_never_serve_bytes(self):
        frame = self.cameras / "cam0" / f"{self.first}.png"
        outside = self.root / "outside.png"
        outside.write_bytes(self.png)
        for kind in ("symlink", "hardlink", "fifo"):
            frame.unlink()
            if kind == "symlink":
                frame.symlink_to(outside)
            elif kind == "hardlink":
                os.link(outside, frame)
            else:
                os.mkfifo(frame)
            self.assert_error(self.client.get(self.image_url()), 503, "artifact_unavailable")

    def test_camera_index_tampering_cannot_escape_read_only_camera_root(self):
        for field, value in (("relative_path", "../../private"), ("timestamp_ns", "../private"),
                             ("index", True), ("sha256", "0" * 64), ("size_bytes", 2 * 1024 * 1024 + 1)):
            with self.subTest(field=field):
                index = deepcopy(self.image_index)
                index["cameras"]["cam0"][0][field] = value
                self.publish("image-index.json", index)
                self.assert_error(self.client.get(self.image_url()), 503, "artifact_unavailable")

    def test_camera_directory_links_and_missing_files_are_rejected(self):
        original = self.cameras / "cam0"
        target = self.root / "relocated-camera"
        original.rename(target)
        original.symlink_to(target, target_is_directory=True)
        self.assert_error(self.client.get(self.image_url()), 503, "artifact_unavailable")
        original.unlink()
        target.rename(original)
        (original / f"{self.first}.png").unlink()
        self.assert_error(self.client.get(self.image_url()), 503, "artifact_unavailable")
