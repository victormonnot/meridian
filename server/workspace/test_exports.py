"""Export access, retention, bounded evidence reads and download transport.

The archive builder is replaced only in these HTTP-boundary fixtures; its
format/semantic checks have separate numerical-core tests and real-run checks.
"""
from datetime import timedelta
from hashlib import sha256
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone

from meridian.vio_export import MAX_EXPORT_BYTES, MAX_FILE_BYTES, REQUIRED_ARTIFACTS
from . import exports
from .models import Job
from .test_experiments import ExperimentCase


class ExportTests(ExperimentCase):
    def setUp(self):
        super().setUp()
        experiment = self.create_experiment(parameters={"num_pts": 100})
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts, self.sources = self.root / "artifacts", self.root / "source"
        self.artifacts.mkdir()
        self.sources.mkdir()
        self.source_name = "src/meridian/__init__.py"
        source = self.sources / self.source_name
        source.parent.mkdir(parents=True)
        source.write_bytes(b'"""Explicit source fixture."""\n')
        setting = override_settings(MERIDIAN_JOB_ARTIFACT_ROOT=self.artifacts,
                                    MERIDIAN_EXPORT_SOURCE_ROOT=self.sources)
        setting.enable()
        self.addCleanup(setting.disable)
        source_patch = patch.object(exports, "SOURCE_FILES", (self.source_name,))
        source_patch.start()
        self.addCleanup(source_patch.stop)
        now = timezone.now()
        self.job = Job.objects.create(
            owner=self.alpha, project_id=self.project["id"],
            experiment_id=experiment["experiment"]["id"],
            experiment_name="Private user metadata is not in the archive",
            revision_version=1, specification=experiment["revision"]["specification"],
            specification_sha256=experiment["revision"]["specification_sha256"],
            idempotency_key=uuid.uuid4(), state="succeeded", phase="succeeded",
            attempt_count=1, started_at=now - timedelta(minutes=2), finished_at=now,
            published_attempt=uuid.uuid4(), artifacts_available=True,
            artifacts_expire_at=now + timedelta(days=7), result={"metadata": "retained"},
            artifact_manifest={"schema_version": 1, "files": {}})
        self.directory = self.artifacts / str(self.job.pk) / str(self.job.published_attempt)
        self.directory.mkdir(parents=True)
        self.contents = {name: f"HTTP evidence fixture for {name}\n".encode() for name in REQUIRED_ARTIFACTS}
        for name, data in self.contents.items():
            (self.directory / name).write_bytes(data)
            self.job.artifact_manifest["files"][name] = {"sha256": sha256(data).hexdigest(), "size_bytes": len(data)}
        self.job.save(update_fields=["artifact_manifest"])
        self.detail = f"/api/v1/projects/{self.job.project_id}/jobs/{self.job.pk}"
        self.url = self.detail + "/export?schema_version=1"
        self.zip = b"PK\x03\x04binary-transport-fixture"
        builder = patch.object(exports, "build_export", return_value=self.zip)
        self.build = builder.start()
        self.addCleanup(builder.stop)

    def test_owner_download_has_exact_bytes_hash_attachment_and_no_cache(self):
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.content, self.zip)
        self.assertEqual(result["Content-Type"], "application/zip")
        self.assertEqual(result["Content-Disposition"], f'attachment; filename="meridian-run-{self.job.pk}-v1.zip"')
        self.assertEqual(result["Content-Length"], str(len(self.zip)))
        self.assertEqual(result["X-Content-SHA256"], sha256(self.zip).hexdigest())
        self.assertEqual(result["Cache-Control"], "no-store")
        self.assertEqual(result["X-Content-Type-Options"], "nosniff")
        self.assertIn("Cookie", result["Vary"])
        args, options = self.build.call_args
        self.assertEqual(args, (self.job.specification,))
        self.assertEqual(options["artifacts"], self.contents)
        self.assertEqual(set(options["source_files"]), {self.source_name})
        self.assertEqual(options["execution"], {
            "job_id": str(self.job.pk), "experiment_id": str(self.job.experiment_id),
            "revision_version": 1, "state": "succeeded",
            "created_at": self.job.created_at.isoformat(), "started_at": self.job.started_at.isoformat(),
            "finished_at": self.job.finished_at.isoformat(), "specification_sha256": self.job.specification_sha256})
        detail = self.client.get(self.detail).json()["job"]
        self.assertTrue(detail["export_available"])
        self.assertIsNone(detail["export_unavailable_reason"])
        self.assertEqual(self.client.get(self.url).content, self.zip)

    def test_authentication_and_both_ownership_relations_precede_file_reads(self):
        peer = get_user_model().objects.create_user("export.peer", password="Separate!Account-2468")
        other = Client()
        other.force_login(peer)
        with patch.object(exports, "_read") as read, patch.object(exports, "_source_files") as sources:
            self.assert_error(Client().get(self.url), 401, "authentication_required")
            self.assert_error(other.get(self.url), 404, "not_found")
            self.assert_error(self.client.get(self.url.replace(str(self.job.project_id), str(uuid.uuid4()))), 404, "not_found")
            self.job.owner = peer
            self.job.save(update_fields=["owner"])
            self.assert_error(self.client.get(self.url), 404, "not_found")
            self.assert_error(other.get(self.url), 404, "not_found")
            read.assert_not_called()
            sources.assert_not_called()

    def test_query_contract_rejects_unknown_missing_repeated_or_ambiguous_formats(self):
        route = self.detail + "/export"
        for query in ("", "?format=zip", "?schema_version=1&path=/etc/passwd", "?schema_version=1&schema_version=1"):
            self.assert_error(self.client.get(route + query), 400, "invalid_request")
        for version in ("2", "01", "1.0", "", "true"):
            self.assert_error(self.client.get(route + "?schema_version=" + version), 400, "unsupported_schema")
        self.build.assert_not_called()

    def test_mutating_methods_are_not_accepted(self):
        for method in ("post", "put", "patch", "delete"):
            result = self.mutate(method, self.url)
            self.assert_error(result, 405, "method_not_allowed")
            self.assertEqual(result["Allow"], "GET")

    def test_expiry_revokes_download_before_physical_collection(self):
        self.job.artifacts_expire_at = timezone.now() - timedelta(seconds=1)
        self.job.save(update_fields=["artifacts_expire_at"])
        self.assert_error(self.client.get(self.url), 410, "artifacts_expired")
        self.assertTrue((self.directory / "trajectory.csv").exists())
        detail = self.client.get(self.detail).json()["job"]
        self.assertFalse(detail["export_available"])
        self.assertIn("expired", detail["export_unavailable_reason"])
        self.assertEqual(detail["result"], {"metadata": "retained"})
        self.build.assert_not_called()

    def test_deleted_job_revokes_hosted_download(self):
        self.job.delete()
        self.assertTrue((self.directory / "trajectory.csv").exists())
        self.assert_error(self.client.get(self.url), 404, "not_found")
        self.build.assert_not_called()

    def test_deleted_project_revokes_export_even_before_artifact_cleanup(self):
        self.job.project.delete()
        self.assertTrue((self.directory / "trajectory.csv").exists())
        self.assert_error(self.client.get(self.url), 404, "not_found")
        self.build.assert_not_called()

    def test_revoked_account_session_cannot_download_retained_files(self):
        self.alpha.delete()
        self.assertTrue((self.directory / "trajectory.csv").exists())
        self.assert_error(self.client.get(self.url), 401, "authentication_required")
        self.build.assert_not_called()

    def test_incomplete_cancelled_failed_or_active_jobs_do_not_offer_reproduction(self):
        for state in ("queued", "running", "evaluating", "failed", "cancelled"):
            self.job.state = state
            self.job.save(update_fields=["state"])
            self.assert_error(self.client.get(self.url), 409, "export_unavailable")
            self.assertFalse(self.client.get(self.detail).json()["job"]["export_available"])
        self.build.assert_not_called()

    def test_missing_retention_or_publication_metadata_is_explicitly_unavailable(self):
        original = self.job.artifact_manifest
        for field, value in (("artifacts_available", False), ("published_attempt", None),
                             ("artifacts_expire_at", None), ("artifact_manifest", {"schema_version": 1, "files": {}})):
            old = getattr(self.job, field)
            setattr(self.job, field, value)
            self.job.save(update_fields=[field])
            self.assert_error(self.client.get(self.url), 409, "export_unavailable")
            setattr(self.job, field, old)
            self.job.save(update_fields=[field])
        self.assertEqual(self.job.artifact_manifest, original)

    def test_publication_schema_and_file_identity_are_checked_before_archive_build(self):
        self.job.artifact_manifest["schema_version"] = True
        self.job.save(update_fields=["artifact_manifest"])
        self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
        self.job.artifact_manifest["schema_version"] = 1
        self.job.artifact_manifest["files"]["trajectory.csv"]["size_bytes"] = MAX_FILE_BYTES + 1
        self.job.save(update_fields=["artifact_manifest"])
        self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
        self.build.assert_not_called()

    def test_aggregate_evidence_budget_is_checked_before_reading(self):
        for item in self.job.artifact_manifest["files"].values():
            item["size_bytes"] = MAX_FILE_BYTES
        self.job.save(update_fields=["artifact_manifest"])
        with patch.object(exports, "_read") as read:
            self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
            read.assert_not_called()

    def test_artifact_tamper_and_missing_file_do_not_produce_an_archive(self):
        path = self.directory / "trajectory.csv"
        path.write_bytes(b"X" * len(self.contents["trajectory.csv"]))
        self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
        path.unlink()
        self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
        self.build.assert_not_called()

    def test_artifact_links_and_fifo_are_rejected(self):
        path = self.directory / "trajectory.csv"
        saved = self.root / "saved-trajectory"
        path.rename(saved)
        for kind in ("symlink", "hardlink", "fifo"):
            if kind == "symlink":
                path.symlink_to(saved)
            elif kind == "hardlink":
                os.link(saved, path)
            else:
                os.mkfifo(path)
            self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
            path.unlink()
        self.build.assert_not_called()

    def test_source_links_parent_links_and_missing_sources_are_rejected(self):
        path = self.sources / self.source_name
        saved = self.root / "saved-source"
        path.rename(saved)
        path.symlink_to(saved)
        self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
        path.unlink()
        self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
        path.write_bytes(saved.read_bytes())
        parent = path.parent
        moved = self.root / "moved-source"
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
        self.build.assert_not_called()

    def test_unlisted_private_files_and_logs_are_never_read_or_exported(self):
        (self.sources / ".env").write_text("private fixture")
        (self.directory / "runner.log").symlink_to(self.sources / ".env")
        self.job.artifact_manifest["files"]["runner.log"] = {"sha256": "a" * 64, "size_bytes": 15}
        self.job.save(update_fields=["artifact_manifest"])
        self.assertEqual(self.client.get(self.url).status_code, 200)
        options = self.build.call_args.kwargs
        self.assertNotIn("runner.log", options["artifacts"])
        self.assertNotIn(".env", options["source_files"])

    def test_corrupt_saved_specification_is_not_exported(self):
        self.job.specification_sha256 = "0" * 64
        self.job.save(update_fields=["specification_sha256"])
        self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
        self.build.assert_not_called()

    def test_builder_failure_is_generic_and_does_not_expose_paths(self):
        self.build.side_effect = ValueError("sensitive internal path /srv/private/example")
        result = self.client.get(self.url)
        self.assert_error(result, 503, "export_integrity_failed")
        self.assertNotIn(b"/srv", result.content)
        self.assertNotIn(b"sensitive", result.content)

    def test_deleted_or_expired_during_assembly_does_not_send_bytes(self):
        def expire(*args, **kwargs):
            Job.objects.filter(pk=self.job.pk).update(artifacts_expire_at=timezone.now() - timedelta(seconds=1))
            return self.zip
        self.build.side_effect = expire
        self.assert_error(self.client.get(self.url), 410, "artifacts_expired")
        self.job.save(update_fields=["artifacts_expire_at"])
        def delete(*args, **kwargs):
            Job.objects.filter(pk=self.job.pk).delete()
            return self.zip
        self.build.side_effect = delete
        self.assert_error(self.client.get(self.url), 404, "not_found")

    def test_publication_changed_during_assembly_does_not_send_bytes(self):
        def change(*args, **kwargs):
            Job.objects.filter(pk=self.job.pk).update(published_attempt=uuid.uuid4())
            return self.zip
        self.build.side_effect = change
        self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")

    def test_oversized_generated_archive_is_not_sent(self):
        self.build.return_value = b"x" * (MAX_EXPORT_BYTES + 1)
        self.assert_error(self.client.get(self.url), 503, "export_integrity_failed")
