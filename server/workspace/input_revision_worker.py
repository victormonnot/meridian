"""Prepared input work serviced by the existing isolated import orchestrator."""
import shutil

from django.conf import settings

from meridian.vio_job_control import AttemptCancelled, AttemptLeaseLost
from . import input_revision_queue as queue, input_revision_storage as storage
from .acquisition_execution import ImportFailure
from .acquisition_worker import LeaseGuard
from .revision_execution import cleanup_revision, execute_revision


def run_one(worker_id, shutdown):
    selected = queue.claim_revision(worker_id)
    if selected is None:
        return False
    revision, attempt_id = selected
    scratch = settings.MERIDIAN_IMPORT_SCRATCH_ROOT / str(attempt_id)
    stopped = False
    with LeaseGuard(revision.pk, attempt_id, shutdown, heartbeat=queue.heartbeat_revision) as guard:
        try:
            scratch.mkdir(mode=0o700)
            outcome = execute_revision(storage.source_path(revision), scratch, attempt_id=str(attempt_id),
                checkpoint=guard.checkpoint, image=settings.MERIDIAN_IMPORT_IMAGE, request=revision.request)
            stopped = cleanup_revision(str(attempt_id))
            if not stopped:
                return True
            guard.checkpoint("publishing")
            storage.publish(revision, attempt_id, scratch / "revision/result", outcome, checkpoint=guard.checkpoint)
            guard.checkpoint("publishing")
            queue.finish_revision(revision.pk, attempt_id, outcome)
        except AttemptLeaseLost:
            stopped = cleanup_revision(str(attempt_id))
        except Exception as problem:
            stopped = cleanup_revision(str(attempt_id))
            if stopped:
                code = ("cancelled" if isinstance(problem, AttemptCancelled)
                        else "invalid_input" if isinstance(problem, ValueError) and not isinstance(problem, ImportFailure)
                        else ("invalid_input" if problem.code == "invalid_package" else problem.code)
                        if isinstance(problem, ImportFailure) else "infrastructure_failure")
                queue.finish_revision(revision.pk, attempt_id, {"state": "cancelled" if code == "cancelled" else "rejected",
                                                              "failure": {"code": code}})
        finally:
            if stopped:
                shutil.rmtree(scratch, ignore_errors=True)
    return True


def reconcile():
    for revision_id, attempt_id in queue.expired_revisions():
        if cleanup_revision(str(attempt_id)):
            scratch = settings.MERIDIAN_IMPORT_SCRATCH_ROOT / str(attempt_id)
            if scratch.exists():
                shutil.rmtree(scratch)
            queue.recover_revision(revision_id, attempt_id)
