#!/usr/bin/env python3
"""Operator acceptance exercise against an idle, explicitly named deployment.

Creates disposable accounts and real jobs. Deliberately interrupts its own
worker/native execution, corrupts then restores one owned artifact, and advances
one owned artifact expiry. Never use this as a background monitor.
Credentials remain in memory; the evidence directory must stay private.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


ACTIVE = {"queued", "running", "evaluating"}
TERMINAL = {"succeeded", "failed", "cancelled"}


def now():
    return datetime.now(timezone.utc).isoformat()


class Client:
    def __init__(self, base):
        self.base = base
        self.csrf = None
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.user = None
        self.jobs = []
        self.accepted = 0
        self.project = self.experiment = None
        self.last_headers = {}

    def request(self, path, method="GET", data=None, expected=200, binary=False):
        headers = {"Origin": self.base, "Cache-Control": "no-store"}
        if self.csrf:
            headers["X-CSRFToken"] = self.csrf
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + "/api/v1" + path, method=method, headers=headers,
            data=None if data is None else json.dumps(data).encode())
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            self.last_headers = {key.lower(): value for key, value in response.headers.items()}
            if response.status != expected:
                raise RuntimeError(f"Unexpected HTTP {response.status}: {method} {path}")
            if binary:
                return content, dict(response.headers)
            payload = json.loads(content)
            if "csrf_token" in payload:
                self.csrf = payload["csrf_token"]
            return payload

    @property
    def jobs_path(self):
        return f"/projects/{self.project}/jobs"

    def submit(self, expected=201, key=None):
        key = key or str(uuid.uuid4())
        payload = self.request(self.jobs_path, "POST", {
            "schema_version": 1, "experiment_id": self.experiment,
            "revision_version": 1, "idempotency_key": key}, expected)
        if expected == 201:
            self.jobs.append(payload["job"]["id"])
            self.accepted += 1
        return payload, key

    def job(self, identifier):
        return self.request(f"{self.jobs_path}/{identifier}")["job"]

    def cancel(self, identifier):
        return self.request(f"{self.jobs_path}/{identifier}/cancel", "POST",
                            {"schema_version": 1})["job"]


class Qualification:
    def __init__(self, args):
        self.args = args
        self.output = Path(args.output)
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.clients = []
        self.services = {}
        self.report = {
            "schema_version": 1, "started_at": now(), "passed": False,
            "transport": urllib.parse.urlparse(args.base_url).scheme,
            "method": "Operator-induced faults on disposable accounts and jobs.",
            "events": [], "checks": [], "accounts": [], "jobs": {},
            "limitations": [
                "Native interruption is induced, not an observed estimator defect.",
                "Expiration is advanced on one owned job; seven days do not elapse.",
                "Polling and container event times are observations, not a throughput guarantee.",
                "Global retained and daily boundaries are separately checked with PostgreSQL fixtures.",
                "Evidence includes private account/job identifiers and must not be published wholesale.",
            ],
        }

    def save(self):
        target = self.output / "report.json"
        descriptor = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(self.report, stream, indent=2, allow_nan=False)
            stream.write("\n")

    def check(self, label, condition=True):
        if not condition:
            raise AssertionError(label)
        self.report["checks"].append(label)
        self.save()
        print(label, flush=True)

    def docker(self, *args, check=True, data=None, timeout=60):
        return subprocess.run(["docker", *args], input=data, text=True,
                              capture_output=True, check=check, timeout=timeout)

    def python(self, service, code, **values):
        program = "values = " + repr(values) + "\n" + code
        response = self.docker("exec", "-i", self.services[service], "python", "-",
                               data=program)
        return json.loads(response.stdout)

    def database(self, code, **values):
        return self.python("api", """
import os, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from workspace.models import Job, Project, Experiment, RunComparison
""" + code, **values)

    def active(self):
        return self.database("""
print(json.dumps(list(Job.objects.filter(state__in=["queued","running","evaluating"])
    .values("id","owner_id","state","current_attempt")), default=str))
""")

    def guard(self, idle=False):
        owned = {str(client.user["id"]) for client in self.clients}
        active = self.active()
        if idle and active:
            raise RuntimeError("The deployment is not idle; refusing service interruption.")
        if any(str(job["owner_id"]) not in owned for job in active):
            raise RuntimeError("Unrelated work appeared; refusing service interruption.")
        return active

    def children(self):
        ids = self.docker("ps", "-aq", "--filter",
                          "label=meridian.component=vio-job", "--format", "{{.ID}}").stdout.split()
        if not ids:
            return []
        items = json.loads(self.docker("inspect", *ids).stdout)
        return [{
            "id": item["Id"], "attempt": item["Config"]["Labels"].get("meridian.attempt"),
            "phase": item["Config"]["Labels"].get("meridian.phase"),
            "running": item["State"]["Running"],
        } for item in items]

    def initialize(self):
        for service in ("api", "worker", "db"):
            ids = self.docker("ps", "-q", "--filter",
                f"label=com.docker.compose.project={self.args.project}", "--filter",
                f"label=com.docker.compose.service={service}").stdout.split()
            if len(ids) != 1:
                raise RuntimeError(f"Expected one running {service} in the explicit Compose project.")
            self.services[service] = ids[0]
        self.guard(idle=True)
        self.check("initial deployment has no active jobs or native children", not self.children())
        self.report["database_before"] = self.database("""
print(json.dumps({"users":get_user_model().objects.count(), "projects":Project.objects.count(),
 "experiments":Experiment.objects.count(),"jobs":Job.objects.count(),
 "comparisons":RunComparison.objects.count()}))
""")
        self.report["images"] = {
            service: self.docker("inspect", "-f", "{{.Image}}", identifier).stdout.strip()
            for service, identifier in self.services.items()}
        for index in range(5):
            client = Client(self.args.base_url.rstrip("/"))
            client.request("/session")
            name = "qa-accept-ops-" + secrets.token_hex(6)
            password = "Qa!" + secrets.token_urlsafe(28)
            client.user = client.request("/auth/register", "POST", {
                "username": name, "password": password}, 201)["user"]
            self.clients.append(client)
            self.report["accounts"].append(client.user)
            self.save()
            project = client.request("/projects", "POST", {
                "name": f"Operational acceptance {index + 1}",
                "description": "Disposable, operator-induced acceptance scenarios.",
                "dataset_id": "tumvi-room1-512-16-v1"}, 201)["project"]
            client.project = project["id"]
            experiment = client.request(f"/projects/{client.project}/experiments", "POST", {
                "schema_version": 1, "name": "Frozen room1 baseline",
                "description": "", "dataset_id": project["dataset_id"],
                "parameters": {"num_pts": 200}}, 201)
            client.experiment = experiment["experiment"]["id"]
            binding = self.database("""
user=get_user_model().objects.get(pk=values["user"]["id"],username=values["user"]["username"])
project=Project.objects.get(pk=values["project"],owner=user)
experiment=Experiment.objects.get(pk=values["experiment"],project=project)
print(json.dumps({"deployment_bound":True}))
""", user=client.user, project=client.project, experiment=client.experiment)
            if not binding["deployment_bound"]:
                raise RuntimeError("HTTP origin and Compose deployment differ")
        self.check("five disposable accounts have independent owned projects")
        self.check("HTTP account/project identities bind to the exact Compose database")

    def wait(self, client, identifier, expected, fault=None):
        deadline = time.monotonic() + self.args.timeout
        previous = None
        injected = None
        old_absent = False
        while time.monotonic() < deadline:
            job = client.job(identifier)
            signature = (job["state"], job["phase"], job["attempt_count"])
            if signature != previous:
                self.report["events"].append({"at": now(), "job_id": identifier,
                    "state": job["state"], "phase": job["phase"],
                    "attempt_count": job["attempt_count"]})
                previous = signature
                self.save()
                print(json.dumps({"job": identifier, "state": signature}), flush=True)
            children = self.children()
            native = [child for child in children if child["running"] and child["phase"] == "native"]
            self.check_concurrency(native)
            if native and fault and injected is None:
                active = self.guard()
                current = next(item for item in active if str(item["id"]) == identifier)
                child = next(item for item in native if item["attempt"] == str(current["current_attempt"]))
                injected = child
                self.report.setdefault("faults", []).append({
                    "at": now(), "kind": fault, "job_id": identifier,
                    "attempt_id": child["attempt"]})
                self.save()
                if fault == "worker":
                    self.docker("kill", "--signal", "KILL", self.services["worker"])
                    self.docker("start", self.services["worker"])
                elif fault == "native":
                    self.docker("kill", "--signal", "KILL", child["id"])
                elif fault == "cancel":
                    client.cancel(identifier)
                    client.cancel(identifier)
                else:
                    raise ValueError("Unknown fault")
            if native and injected and fault == "worker":
                if any(child["attempt"] != injected["attempt"] for child in native):
                    absent = self.docker("inspect", injected["id"], check=False)
                    old_absent = absent.returncode != 0 and "no such" in absent.stderr.lower()
                    if not old_absent:
                        raise AssertionError("Old child remains when retry starts")
            if job["state"] in TERMINAL:
                self.report["jobs"][identifier] = job
                self.save()
                if job["state"] != expected or (fault and not injected):
                    raise AssertionError(f"Unexpected terminal outcome for {identifier}")
                if fault == "worker":
                    self.check("worker recovery removes old child before retry", old_absent)
                    self.check("worker recovery preserves failed attempt and succeeds once",
                        job["attempt_count"] == 2
                        and [a["state"] for a in job["attempts"]] == ["expired", "succeeded"]
                        and job["attempts"][0]["failure"]["code"] == "infrastructure_failure")
                return job
            time.sleep(1)
        raise TimeoutError("Job did not reach its expected terminal state")

    def check_concurrency(self, native):
        count = len(native)
        self.report["maximum_observed_native_concurrency"] = max(
            count, self.report.get("maximum_observed_native_concurrency", 0))
        if count > 1:
            raise AssertionError("More than one native execution observed")

    def executions(self):
        owner = self.clients[0]
        recovered, _ = owner.submit()
        identifier = recovered["job"]["id"]
        result = self.wait(owner, identifier, "succeeded", fault="worker")
        self.check("recovered result keeps its original saved specification",
                   result["specification_sha256"] == recovered["job"]["specification_sha256"])
        data, headers = owner.request(f"{owner.jobs_path}/{identifier}/export?schema_version=1",
                                      binary=True)
        digest = hashlib.sha256(data).hexdigest()
        lowered = {key.lower(): value for key, value in headers.items()}
        self.check("recovered export matches its transport SHA-256 and byte length",
            lowered["x-content-sha256"] == digest and int(lowered["content-length"]) == len(data))
        (self.output / "recovered-experiment.zip").write_bytes(data)
        os.chmod(self.output / "recovered-experiment.zip", 0o600)
        self.report["recovered_export"] = {"sha256": digest, "size_bytes": len(data)}
        for fault, expected in (("cancel", "cancelled"), ("native", "failed")):
            submitted, _ = owner.submit()
            job = self.wait(owner, submitted["job"]["id"], expected, fault=fault)
            self.check(f"{fault} interruption has no successful metrics or complete export",
                job["result"] is None and job["export_available"] is False
                and job["attempt_count"] == 1)
            if fault == "native":
                self.check("interrupted native output is a terminal numerical failure",
                           job["failure"]["code"] == "numerical_failure")
            owner.request(f"{owner.jobs_path}/{job['id']}/export?schema_version=1",
                          expected=409)
        self.guard(idle=True)
        self.check("execution scenarios leave no native children", not self.children())
        self.integrity_and_expiry(owner, identifier)

    def integrity_and_expiry(self, owner, identifier):
        artifact = self.database("""
job=Job.objects.get(pk=values["job"],owner_id=values["owner"])
assert job.state=="succeeded" and job.artifacts_available
print(json.dumps({"job":str(job.pk),"attempt":str(job.published_attempt),
                 "file":job.artifact_manifest["files"]["trajectory.csv"]}))
""", job=identifier, owner=owner.user["id"])
        # Write only this known QA artifact. Restore its exact bytes and mode on
        # every exit; do not mutate publication metadata to bless corrupt bytes.
        mutation = """
import hashlib, json, os
from pathlib import Path
root=Path(os.environ["MERIDIAN_JOB_ARTIFACT_ROOT"])
path=root/values["job"]/values["attempt"]/"trajectory.csv"
assert not path.is_symlink() and path.is_file()
if values["operation"]=="corrupt":
 raw=path.read_bytes()
 assert len(raw)==values["file"]["size_bytes"]
 assert hashlib.sha256(raw).hexdigest()==values["file"]["sha256"]
 backup=path.with_name("trajectory.acceptance-backup")
 assert not backup.exists()
 mode=path.stat().st_mode & 0o777
 backup.write_bytes(raw); os.chmod(backup,mode)
 os.chmod(path,0o600);path.write_bytes(b"invalid trajectory\\n");os.chmod(path,mode)
else:
 backup=path.with_name("trajectory.acceptance-backup")
 if backup.exists():
  raw=backup.read_bytes();mode=backup.stat().st_mode & 0o777
  assert hashlib.sha256(raw).hexdigest()==values["file"]["sha256"]
  os.chmod(path,0o600);path.write_bytes(raw);os.chmod(path,mode);backup.unlink()
 else:
  assert hashlib.sha256(path.read_bytes()).hexdigest()==values["file"]["sha256"]
print(json.dumps({"operation":values["operation"],"passed":True}))
"""
        mutation_attempted = False
        try:
            mutation_attempted = True
            self.python("worker", mutation, **artifact, operation="corrupt")
            denied = owner.request(f"{owner.jobs_path}/{identifier}/export?schema_version=1",
                                   expected=503)
            self.check("corrupt retained artifact cannot be exported",
                       denied["error"]["code"] == "export_integrity_failed")
        finally:
            if mutation_attempted:
                self.python("worker", mutation, **artifact, operation="restore")
        restored, _ = owner.request(f"{owner.jobs_path}/{identifier}/export?schema_version=1",
                                     binary=True)
        self.check("restoring exact bytes restores the identical export",
                   hashlib.sha256(restored).hexdigest() == self.report["recovered_export"]["sha256"])
        before = owner.job(identifier)["result"]
        self.database("""
from datetime import timedelta
job=Job.objects.get(pk=values["job"],owner_id=values["owner"],state="succeeded")
job.artifacts_expire_at=timezone.now()-timedelta(seconds=1)
job.save(update_fields=["artifacts_expire_at"])
print(json.dumps({"accelerated_expiration":True}))
""", job=identifier, owner=owner.user["id"])
        expired = owner.request(f"{owner.jobs_path}/{identifier}/export?schema_version=1",
                                expected=410)
        self.check("accelerated expiration revokes export without erasing metrics",
                   expired["error"]["code"] == "artifacts_expired"
                   and owner.job(identifier)["result"] == before)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            remaining = self.python("worker", """
import json,os
from pathlib import Path
path=Path(os.environ["MERIDIAN_JOB_ARTIFACT_ROOT"])/values["job"]/values["attempt"]
print(json.dumps(path.exists()))
""", job=identifier, attempt=artifact["attempt"])
            if not remaining and not owner.job(identifier)["artifacts_available"]:
                break
            time.sleep(2)
        else:
            raise TimeoutError("Expired artifact collection did not complete")
        self.check("worker physically collects the accelerated expired attempt")

    def quotas(self):
        self.guard(idle=True)
        self.docker("stop", "--time", "40", self.services["worker"])
        try:
            self.guard(idle=True)
            queued = []
            for index in (0, 1, 2, 3, 4, 0, 1, 2):
                client = self.clients[index]
                result, key = client.submit()
                queued.append((client, result["job"]["id"], key))
                if result["job"]["state"] != "queued":
                    raise AssertionError("Expected a queued request while worker is stopped")
            rejected, _ = self.clients[4].submit(expected=409)
            self.check("global queue admits eight and rejects ninth below account limit",
                       rejected["error"]["code"] == "queue_full" and len(self.active()) == 8)
            for client, identifier, _ in queued:
                client.cancel(identifier)
            self.check("queued cancellation releases all eight active slots", not self.active())
            owner = self.clients[0]
            while owner.accepted < 10:
                created, _ = owner.submit()
                identifier = created["job"]["id"]
                owner.cancel(identifier)
                owner.request(f"{owner.jobs_path}/{identifier}", "DELETE")
            rejected, _ = owner.submit(expected=429)
            self.check("ten accepted account submissions survive cancellation and deletion",
                       rejected["error"]["code"] == "submission_limit")
            retry_after = owner.last_headers.get("retry-after", "")
            self.check("daily limit provides a positive bounded Retry-After",
                       retry_after.isdigit() and 0 < int(retry_after) <= 86400)
            first = next(item for item in queued if item[0] is owner)
            repeated, _ = owner.submit(expected=200, key=first[2])
            self.check("idempotent retry still returns its existing job at daily limit",
                       repeated["reused"] and repeated["job"]["id"] == first[1])
        finally:
            # Never restart with our quota-test jobs still queued after an error.
            for client in self.clients:
                for identifier in client.jobs:
                    try:
                        if client.job(identifier)["state"] in ACTIVE:
                            client.cancel(identifier)
                    except Exception:
                        pass
            self.docker("start", self.services["worker"])

    def cleanup(self):
        if not self.clients:
            return
        for client in self.clients:
            for identifier in client.jobs:
                try:
                    if client.job(identifier)["state"] in ACTIVE:
                        client.cancel(identifier)
                except Exception:
                    pass
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            active = self.guard()
            if not active:
                break
            time.sleep(2)
        else:
            raise RuntimeError("Own jobs remain active; retain accounts for operator recovery")
        self.report["cleanup"] = self.database("""
from django.contrib.sessions.models import Session
from django.utils.crypto import salted_hmac
from workspace.models import AuthWindow
global_key=salted_hmac("meridian.compute-window","global",algorithm="sha256").hexdigest()
budget_before=list(AuthWindow.objects.filter(pk=global_key).values("started_at","count"))
deleted={}
for identity in values["accounts"]:
 user=get_user_model().objects.get(pk=identity["id"],username=identity["username"])
 keys=[salted_hmac("meridian.compute-window",str(user.pk),algorithm="sha256").hexdigest(),
       salted_hmac("meridian.auth-window","username:"+user.username.casefold(),algorithm="sha256").hexdigest()]
 sessions=[s.pk for s in Session.objects.all() if s.get_decoded().get("_auth_user_id")==str(user.pk)]
 Session.objects.filter(pk__in=sessions).delete()
 AuthWindow.objects.filter(pk__in=keys).delete()
 counts=user.delete()[1]
 for key,count in counts.items():deleted[key]=deleted.get(key,0)+count
assert list(AuthWindow.objects.filter(pk=global_key).values("started_at","count"))==budget_before
print(json.dumps({"deleted":deleted,"global_submission_budget_preserved":True,
 "database_after":{"users":get_user_model().objects.count(),"projects":Project.objects.count(),
 "experiments":Experiment.objects.count(),"jobs":Job.objects.count(),
 "comparisons":RunComparison.objects.count()}}))
""", accounts=[client.user for client in self.clients])
        self.check("cleanup preserves initial database record counts",
                   self.report["cleanup"]["database_after"] == self.report["database_before"])
        jobs = [identifier for client in self.clients for identifier in client.jobs]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            remaining = self.python("worker", """
import json,os
from pathlib import Path
root=Path(os.environ["MERIDIAN_JOB_ARTIFACT_ROOT"])
print(json.dumps([name for name in values["jobs"] if (root/name).exists()]))
""", jobs=jobs)
            if not remaining:
                break
            time.sleep(2)
        else:
            raise TimeoutError("Deleted acceptance artifact directories remain")
        self.check("cleanup physically removes all owned artifact directories")

    def run(self):
        try:
            self.initialize()
            self.executions()
            self.quotas()
            self.report["passed"] = True
        except BaseException as error:
            self.report["error_type"] = type(error).__name__
            raise
        finally:
            try:
                if self.services.get("worker"):
                    self.docker("start", self.services["worker"])
                self.cleanup()
            except Exception as error:
                self.report["passed"] = False
                self.report["cleanup_error_type"] = type(error).__name__
                raise
            finally:
                self.report["finished_at"] = now()
                self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--project", required=True, help="Exact Docker Compose project name.")
    parser.add_argument("--output", required=True, type=Path, help="New private evidence directory.")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    address = urllib.parse.urlparse(args.base_url)
    if (address.scheme not in {"http", "https"} or not address.hostname
            or address.username or address.password or address.path not in ("", "/")
            or address.query or address.fragment
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.project)
            or not 60 <= args.timeout <= 3600):
        parser.error("Use a plain HTTP(S) origin, exact Compose project and timeout 60..3600.")
    Qualification(args).run()


if __name__ == "__main__":
    main()
