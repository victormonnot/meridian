"""Private inspection transport: exact identities, bounded reads and revocation."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import csv
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from meridian.sensor_contract import contract_sha256
from . import acquisition_inspection as api
from .models import Acquisition, Project


EXAMPLES = Path(__file__).resolve().parents[2] / "examples/sensor-contracts"


def timing(stamps):
    intervals = [int(b) - int(a) for a, b in zip(stamps, stamps[1:])]
    interval = str(intervals[0]) if intervals else None
    return {"interval_count": len(intervals), "elapsed_ns": str(int(stamps[-1]) - int(stamps[0])),
            "rate_hz": 1e9 / intervals[0] if intervals else None,
            "interval_ns": dict.fromkeys(["minimum", "p05", "p50", "p95", "maximum"], interval),
            "quantile_method": "nearest_rank", "histogram": [],
            "gap_threshold_ns": str(3 * intervals[0]) if intervals else None,
            "gap_count": 0, "duplicate_count": 0, "backward_count": 0,
            "ordering_evidence": "enforced_by_import"}


class AcquisitionInspectionTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        override = override_settings(MERIDIAN_ACQUISITION_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.owner = get_user_model().objects.create_user("inspection-owner")
        self.peer = get_user_model().objects.create_user("inspection-peer")
        self.project = Project.objects.create(owner=self.owner, name="Input inspection", dataset_id="tumvi-room1-512-16-v1")
        self.peer_project = Project.objects.create(owner=self.peer, name="Other input", dataset_id="tumvi-room1-512-16-v1")
        descriptor = json.loads((EXAMPLES / "stereo-no-reference.json").read_text())
        self.value = Acquisition.objects.create(
            owner=self.owner, project=self.project, name="Private inspection", filename="synthetic.zip",
            size_bytes=7, bytes_received=7, sha256=sha256(b"archive").hexdigest(),
            idempotency_key=uuid.uuid4(), request_sha256="1" * 64,
            budget={"chunk_bytes": 16}, state="ready", phase="complete",
            published_attempt=uuid.uuid4(), files_available=True,
            expires_at=timezone.now() + timedelta(hours=1), descriptor=descriptor,
            result={"inspection": {"status": "available", "method_version": api.METHOD,
                                   "summary_path": api.SUMMARY_PATH}},
            manifest={"schema_version": 1, "files": {}})
        self.directory = self.root / str(self.value.pk) / "published" / str(self.value.published_attempt)
        self.directory.mkdir(parents=True)
        (self.directory.parents[1] / "original").write_bytes(b"archive")
        self.url = f"/api/v1/projects/{self.project.pk}/acquisitions/{self.value.pk}/inspection"
        self.client.force_login(self.owner)
        self.other = Client()
        self.other.force_login(self.peer)
        self.summary = {
            "schema_version": 1, "kind": "sensor_inspection", "method_version": api.METHOD, "status": "available",
            "acquisition": {"id": descriptor["id"], "sha256": contract_sha256(descriptor)},
            "policy": {"gap_interval_multiplier": 3, "reference_max_bracket_ns": "20000000",
                       "stereo_nearest_tolerance_ns": "5000000", "display_point_limit": 1200},
            "evidence": {"import_validity": "validated", "inspection_availability": "available",
                         "calibration_suitability": "not_assessed", "estimator_compatibility": "not_assessed",
                         "estimate_support": "not_generated", "evaluation_availability": "not_assessed"},
            "clocks": descriptor["clocks"], "frames": descriptor["frames"], "streams": [],
            "comparisons": [], "reference_availability": "absent",
            "limitations": ["Synthetic transport fixture; no physical quality assertion."]}
        self.chunk_paths = {}
        self.preview_paths = {}
        self.chunks = {}
        inventory = {item["path"]: item for item in descriptor["files"]}
        for number, stream in enumerate(descriptor["streams"]):
            with (EXAMPLES / stream["index_path"]).open() as source:
                original = list(csv.DictReader(source))
            rows = []
            for row in original:
                item = {"sample_id": row["sample_id"], "timestamp_ns": row["timestamp"], "gap_before": None}
                if stream["kind"] == "imu":
                    item.update(gyro_rad_s=[float(row[key]) for key in ["wx", "wy", "wz"]],
                                specific_force_m_s2=[float(row[key]) for key in ["fx", "fy", "fz"]])
                else:
                    source_path = (Path(stream["index_path"]).parent / row["filename"]).as_posix()
                    preview = f"inspection/previews/{number * 100 + int(row['sample_id']):06}.png"
                    content = (EXAMPLES / source_path).read_bytes()
                    self.publish(preview, content)
                    self.preview_paths[(stream["id"], row["sample_id"])] = preview
                    item.update(image={"preview_path": preview, "source_path": source_path,
                                       "source_sha256": inventory[source_path]["sha256"],
                                       "source_size_bytes": len(content), "width": 8, "height": 6,
                                       "preview_width": 8, "preview_height": 6, "indicators": {}},
                                support=[], stereo_match=None)
                rows.append(item)
            name = f"inspection/streams/{number:03}/00000/000000.json"
            chunk = {"schema_version": 1, "method_version": api.METHOD,
                     "stream_id": stream["id"], "segment_id": "segment0", "rows": rows}
            self.chunk_paths[stream["id"]] = name
            self.chunks[stream["id"]] = chunk
            self.publish(name, chunk)
            stamps = [row["timestamp_ns"] for row in rows]
            metadata = {key: deepcopy(stream[key]) for key in ["id", "kind", "frame_id", "clock_id",
                        "timestamp_semantics", "measurement_semantics", "processing"]}
            metadata.update(sample_count=len(rows), segments=[{
                "id": "segment0", "sample_count": len(rows), "first_ns": stamps[0], "last_ns": stamps[-1],
                "gap_count": 0, "timing": timing(stamps), "boundary_from_previous": None,
                "chunks": [{"path": name, "rows": len(rows), "first_ns": stamps[0], "last_ns": stamps[-1],
                            "first_sample_id": rows[0]["sample_id"], "last_sample_id": rows[-1]["sample_id"]}]}])
            if stream["kind"] == "imu":
                metadata.update(source_units={"angular_velocity": "rad/s", "specific_force": "m/s^2"},
                                units={"angular_velocity": "rad/s", "specific_force": "m/s^2"},
                                saturation={"status": "unknown", "reason": "sensor_limits_not_declared"})
            else:
                metadata["camera"] = deepcopy(stream["camera"])
            self.summary["streams"].append(metadata)
        self.publish(api.SUMMARY_PATH, self.summary)

    def publish(self, name, content):
        data = content if isinstance(content, bytes) else json.dumps(content, allow_nan=False).encode()
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.value.manifest["files"][name] = {"size_bytes": len(data), "sha256": sha256(data).hexdigest()}
        self.value.save(update_fields=["manifest"])

    def selection(self, stream="imu0"):
        segment = next(item for item in self.summary["streams"] if item["id"] == stream)["segments"][0]
        return {"stream_id": stream, "segment_id": "segment0", "first_ns": segment["first_ns"], "last_ns": segment["last_ns"]}

    def test_owner_summary_distinguishes_quality_from_calibration_and_estimation(self):
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["inspection"], self.summary)
        self.assertEqual(result.json()["availability"], "available")
        self.assertEqual(result["Cache-Control"], "no-store")
        self.assertIn("Cookie", result["Vary"])

    def test_window_uses_exact_nanoseconds_and_source_observations(self):
        result = self.client.get(self.url + "/window", self.selection())
        self.assertEqual(result.status_code, 200, result.content)
        window = result.json()["window"]
        self.assertEqual(window["sample_count"], 3)
        self.assertEqual(window["display"]["points"], self.chunks["imu0"]["rows"])
        self.assertEqual(window["interval"]["first_ns"], self.selection()["first_ns"])
        self.assertEqual(window["imu"]["axes"]["fz"]["minimum"], -9.81)
        self.assertEqual(window["imu"]["saturation"]["status"], "unknown")

    def test_image_preview_preserves_exact_source_identity_without_decoding(self):
        result = self.client.get(self.url + "/images/cam0/0")
        self.assertEqual(result.status_code, 200, result.content)
        expected = (EXAMPLES / "sensors/cam0/images/0.png").read_bytes()
        self.assertEqual(result.content, expected)
        self.assertEqual(result["Content-Type"], "image/png")
        self.assertEqual(result["X-Content-SHA256"], sha256(expected).hexdigest())
        self.assertEqual(result["X-Source-SHA256"], sha256(expected).hexdigest())
        self.assertEqual(result["X-Timestamp-Ns"], "1520530308199447626")
        self.assertEqual(result["Cache-Control"], "no-store")
        self.assertIn("Cookie", result["Vary"])

    def test_every_route_checks_both_owner_relations_before_opening_files(self):
        for client, expected in [(Client(), 401), (self.other, 404)]:
            for suffix in ["", "/window", "/images/cam0/0"]:
                with self.subTest(status=expected, suffix=suffix), patch.object(api.storage, "open_relative", side_effect=AssertionError("Unauthorized filesystem access")):
                    result = client.get(self.url + suffix, self.selection() if suffix == "/window" else {})
                    self.assertEqual(result.status_code, expected)
        self.value.project = self.peer_project
        self.value.save(update_fields=["project"])
        mismatch = self.url.replace(str(self.project.pk), str(self.peer_project.pk))
        with patch.object(api.storage, "open_relative", side_effect=AssertionError("Mismatched project filesystem access")):
            self.assertEqual(self.client.get(mismatch).status_code, 404)

    def test_old_publication_is_explicitly_not_generated_and_never_written(self):
        self.value.result = {"execution_readiness": "not_assessed"}
        self.value.save(update_fields=["result"])
        with patch.object(api.storage, "open_relative", side_effect=AssertionError("Old snapshot must not trigger generation")):
            result = self.client.get(self.url)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["availability"], "not_generated")
            self.assertIsNone(result.json()["inspection"])
            self.assertEqual(self.client.get(self.url + "/window", self.selection()).status_code, 409)

    def test_resource_limited_inspection_keeps_valid_import_status_separate(self):
        self.value.result = {"inspection": {"status": "unavailable", "reason": "inspection_resource_limit"}}
        self.value.save(update_fields=["result"])
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["availability"], "unavailable")
        self.value.refresh_from_db()
        self.assertEqual(self.value.state, "ready")
        self.assertTrue(self.value.files_available)

    def test_deleted_files_revoke_windows_and_previews(self):
        self.value.files_available = False
        self.value.deleted_at = timezone.now()
        self.value.save(update_fields=["files_available", "deleted_at"])
        with patch.object(api.storage, "open_relative", side_effect=AssertionError("Deleted evidence must not be read")):
            self.assertEqual(self.client.get(self.url).json()["availability"], "deleted")
            self.assertEqual(self.client.get(self.url + "/window", self.selection()).status_code, 410)
            self.assertEqual(self.client.get(self.url + "/images/cam0/0").status_code, 410)

    def test_deletion_during_read_fences_response(self):
        original = api._summary
        def deleting(value):
            result = original(value)
            Acquisition.objects.filter(pk=value.pk).update(files_available=False)
            return result
        with patch.object(api, "_summary", side_effect=deleting):
            self.assertEqual(self.client.get(self.url).status_code, 410)

    def test_summary_identity_and_version_cannot_cross_acquisitions(self):
        for change in [{"acquisition": {"id": "foreign", "sha256": "0" * 64}}, {"method_version": "future"}]:
            with self.subTest(change=change):
                self.publish(api.SUMMARY_PATH, {**self.summary, **change})
                self.assertEqual(self.client.get(self.url).status_code, 503)

    def test_unlisted_missing_and_changed_content_fail_closed(self):
        name = self.chunk_paths["imu0"]
        path = self.directory / name
        original = path.read_bytes()
        for action in ["change", "remove", "unlist"]:
            with self.subTest(action=action):
                self.publish(name, original)
                if action == "change":
                    path.write_bytes(original.replace(b"9.81", b"8.81"))
                elif action == "remove":
                    path.unlink()
                else:
                    del self.value.manifest["files"][name]
                    self.value.save(update_fields=["manifest"])
                self.assertEqual(self.client.get(self.url + "/window", self.selection()).status_code, 503)

    def test_links_and_oversized_manifest_entries_are_rejected_before_read(self):
        path = self.directory / api.SUMMARY_PATH
        content = path.read_bytes()
        outside = self.root / "outside.json"
        outside.write_bytes(content)
        for kind in ["symlink", "hardlink", "oversized"]:
            with self.subTest(kind=kind):
                path.unlink()
                if kind == "symlink":
                    path.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, path)
                else:
                    path.write_bytes(content)
                    self.value.manifest["files"][api.SUMMARY_PATH]["size_bytes"] = api.MAX_JSON_BYTES + 1
                    self.value.save(update_fields=["manifest"])
                self.assertEqual(self.client.get(self.url).status_code, 503)

    def test_ambiguous_json_and_utf16_are_not_accepted_as_publication_metadata(self):
        for data in [b'{"schema_version":1,"schema_version":1}', b'{"n":NaN}', json.dumps(self.summary).encode("utf-16")]:
            with self.subTest(data=data[:30]):
                self.publish(api.SUMMARY_PATH, data)
                self.assertEqual(self.client.get(self.url).status_code, 503)

    def test_invalid_queries_never_select_other_segments_or_raw_file_paths(self):
        for change in [{"first_ns": "1.0"}, {"first_ns": "+1"}, {"first_ns": "-0"},
                       {"first_ns": "9223372036854775808"}, {"first_ns": "0"},
                       {"last_ns": self.selection()["first_ns"], "first_ns": self.selection()["last_ns"]},
                       {"stream_id": "../../original"}, {"path": "original"}]:
            with self.subTest(change=change):
                self.assertEqual(self.client.get(self.url + "/window", {**self.selection(), **change}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {"path": "original"}).status_code, 400)
        self.assertEqual(self.client.get(self.url + "/window", {**self.selection(), "segment_id": "absent"}).status_code, 404)
        self.assertEqual(self.client.get(self.url + "/images/cam0/00").status_code, 404)
        self.assertEqual(self.client.get(self.url + "/images/cam0/999").status_code, 404)
        self.assertEqual(self.client.get(self.url + "/images/imu0/0").status_code, 404)

    def test_preview_cannot_escape_branch_or_claim_another_source_identity(self):
        for change in [{"preview_path": "acquisition/sensors/cam0/images/0.png"}, {"source_sha256": "f" * 64},
                       {"source_size_bytes": 1}, {"preview_width": 321}]:
            with self.subTest(change=change):
                chunk = deepcopy(self.chunks["cam0"])
                chunk["rows"][0]["image"].update(change)
                self.publish(self.chunk_paths["cam0"], chunk)
                self.assertEqual(self.client.get(self.url + "/images/cam0/0").status_code, 503)

    def test_preview_bytes_are_hash_checked_and_bounded(self):
        name = self.preview_paths[("cam0", "0")]
        path = self.directory / name
        content = path.read_bytes()
        path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        self.assertEqual(self.client.get(self.url + "/images/cam0/0").status_code, 503)
        self.publish(name, b"\x89PNG\r\n\x1a\n" + b"x" * api.MAX_PREVIEW_BYTES)
        self.assertEqual(self.client.get(self.url + "/images/cam0/0").status_code, 503)

    def test_window_reads_only_selected_stream_chunks(self):
        for stream in ["cam0", "cam1"]:
            (self.directory / self.chunk_paths[stream]).unlink()
        result = self.client.get(self.url + "/window", self.selection())
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["window"]["sample_count"], 3)

    def test_window_resource_bound_requests_a_narrower_interval(self):
        from meridian.sensor_inspection import InspectionLimitError
        with patch("meridian.sensor_inspection.inspection_window", side_effect=InspectionLimitError("Response budget")):
            result = self.client.get(self.url + "/window", self.selection())
        self.assertEqual(result.status_code, 413)
        self.assertEqual(result.json()["error"]["code"], "inspection_window_limit")

    def test_unknown_image_sample_in_a_valid_nonconsecutive_id_range_is_not_found(self):
        stream = self.summary["streams"][0]
        stream["segments"][0]["chunks"][0]["last_sample_id"] = "2"
        chunk = deepcopy(self.chunks["cam0"])
        chunk["rows"][-1]["sample_id"] = "2"
        self.publish(self.chunk_paths["cam0"], chunk)
        self.publish(api.SUMMARY_PATH, self.summary)
        self.assertEqual(self.client.get(self.url + "/images/cam0/1").status_code, 404)

    def test_dot_in_stream_identifier_is_supported_without_becoming_a_path(self):
        descriptor = deepcopy(self.value.descriptor)
        descriptor["streams"][0]["id"] = "cam.0"
        descriptor["stereo_pairing"]["camera_ids"][0] = "cam.0"
        self.value.descriptor = descriptor
        self.value.save(update_fields=["descriptor"])
        self.summary["streams"][0]["id"] = "cam.0"
        self.summary["acquisition"]["sha256"] = contract_sha256(descriptor)
        chunk = deepcopy(self.chunks["cam0"])
        chunk["stream_id"] = "cam.0"
        self.publish(self.chunk_paths["cam0"], chunk)
        self.publish(api.SUMMARY_PATH, self.summary)
        result = self.client.get(self.url + "/images/cam.0/0")
        self.assertEqual(result.status_code, 200, result.content)

    def test_publication_requires_complete_index_bound_to_source_acquisition(self):
        api.storage.check_inspection_publication(self.directory, self.value.result,
                                                self.value.manifest, self.value.descriptor)
        incomplete = deepcopy(self.value.manifest)
        del incomplete["files"][self.chunk_paths["imu0"]]
        with self.assertRaisesRegex(ValueError, "chunk inventory"):
            api.storage.check_inspection_publication(self.directory, self.value.result,
                                                    incomplete, self.value.descriptor)
        changed = deepcopy(self.summary)
        changed["acquisition"]["sha256"] = "0" * 64
        self.publish(api.SUMMARY_PATH, changed)
        with self.assertRaisesRegex(ValueError, "different acquisition"):
            api.storage.check_inspection_publication(self.directory, self.value.result,
                                                    self.value.manifest, self.value.descriptor)

    def test_resource_limited_publication_cannot_keep_partial_inspection(self):
        report = {"inspection": {"status": "unavailable", "reason": "inspection_resource_limit"}}
        with self.assertRaisesRegex(ValueError, "partial evidence"):
            api.storage.check_inspection_publication(self.directory, report,
                                                    self.value.manifest, self.value.descriptor)
        api.storage.check_inspection_publication(self.directory, report,
                                                {"schema_version": 1, "files": {}}, self.value.descriptor)

    def test_publication_rejects_missing_or_orphan_image_previews(self):
        missing = deepcopy(self.value.manifest)
        del missing["files"][self.preview_paths[("cam0", "0")]]
        with self.assertRaisesRegex(ValueError, "preview is absent"):
            api.storage.check_inspection_publication(self.directory, self.value.result,
                                                    missing, self.value.descriptor)
        self.publish("inspection/previews/999999.png", b"\x89PNG\r\n\x1a\n")
        with self.assertRaisesRegex(ValueError, "orphan preview"):
            api.storage.check_inspection_publication(self.directory, self.value.result,
                                                    self.value.manifest, self.value.descriptor)

    def test_publication_rejects_camera_chunk_with_foreign_source_identity(self):
        chunk = deepcopy(self.chunks["cam0"])
        chunk["rows"][0]["image"]["source_sha256"] = "f" * 64
        self.publish(self.chunk_paths["cam0"], chunk)
        with self.assertRaisesRegex(ValueError, "source image"):
            api.storage.check_inspection_publication(self.directory, self.value.result,
                                                    self.value.manifest, self.value.descriptor)
