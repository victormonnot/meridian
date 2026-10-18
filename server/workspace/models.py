import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=2000, blank=True)
    dataset_id = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "id"]


class AuthWindow(models.Model):
    """Shared fixed-window authentication budget; keys contain HMACs, not IPs."""
    key = models.CharField(max_length=64, primary_key=True)
    started_at = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)


class Experiment(models.Model):
    """Current private summary; every accepted change has a separate snapshot."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="experiments")
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=2000, blank=True)
    current_version = models.PositiveIntegerField(default=1)
    parameters = models.JSONField()
    specification_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "id"]
        constraints = [models.CheckConstraint(condition=models.Q(current_version__gte=1), name="experiment_version_positive")]


class ExperimentRevision(models.Model):
    """Append-only application snapshot; deletion follows its parent experiment."""
    experiment = models.ForeignKey(Experiment, on_delete=models.CASCADE, related_name="revisions")
    version = models.PositiveIntegerField()
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=2000, blank=True)
    specification = models.JSONField()
    specification_sha256 = models.CharField(max_length=64)
    copied_from = models.JSONField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["experiment", "version"], name="experiment_revision_unique"),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="revision_version_positive"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Experiment revisions cannot be edited; append a new revision.")
        return super().save(*args, **kwargs)


class ComputeQueue(models.Model):
    """One database mutex coordinates admission, leases and publication."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    heartbeat_at = models.DateTimeField(null=True)
    capacity_available = models.BooleanField(default=False)


class Job(models.Model):
    STATES = [(value, value) for value in ("queued", "running", "evaluating", "succeeded", "failed", "cancelled")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="vio_jobs")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="jobs")
    experiment = models.ForeignKey(Experiment, on_delete=models.CASCADE, related_name="jobs")
    experiment_name = models.CharField(max_length=100)
    revision_version = models.PositiveIntegerField()
    specification = models.JSONField()
    specification_sha256 = models.CharField(max_length=64)
    idempotency_key = models.UUIDField()
    state = models.CharField(max_length=16, choices=STATES, default="queued")
    phase = models.CharField(max_length=24, default="queued")
    attempt_count = models.PositiveSmallIntegerField(default=0)
    current_attempt = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    cancel_requested_at = models.DateTimeField(null=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    failure = models.JSONField(null=True)
    result = models.JSONField(null=True)
    artifact_manifest = models.JSONField(null=True)
    published_attempt = models.UUIDField(null=True)
    artifacts_expire_at = models.DateTimeField(null=True)
    artifacts_available = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "idempotency_key"], name="job_submission_unique"),
            models.CheckConstraint(condition=models.Q(state__in=["queued", "running", "evaluating", "succeeded", "failed", "cancelled"]), name="job_state_valid"),
        ]
        indexes = [models.Index(fields=["state", "created_at"], name="job_queue_order")]


class JobAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="attempts")
    number = models.PositiveSmallIntegerField()
    worker_id = models.UUIDField()
    state = models.CharField(max_length=16, default="running")
    started_at = models.DateTimeField(auto_now_add=True)
    heartbeat_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    failure = models.JSONField(null=True)

    class Meta:
        ordering = ["number"]
        constraints = [models.UniqueConstraint(fields=["job", "number"], name="job_attempt_number_unique")]
