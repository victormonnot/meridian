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
