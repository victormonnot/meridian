"""Remove expired sessions and inactive authentication counters."""
from datetime import timedelta
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.utils import timezone
from workspace.models import AuthWindow


class Command(BaseCommand):
    help = "Delete expired sessions and authentication counters older than one day."

    def handle(self, *args, **options):
        now = timezone.now()
        sessions, _ = Session.objects.filter(expire_date__lt=now).delete()
        windows, _ = AuthWindow.objects.filter(started_at__lt=now - timedelta(days=1)).delete()
        from workspace.acquisition_queue import cleanup_abandoned
        acquisitions = cleanup_abandoned()
        self.stdout.write(f"Cleaned {acquisitions} inactive recording transfers.")
        self.stdout.write(f"Deleted {sessions} expired sessions and {windows} inactive authentication counters.")
