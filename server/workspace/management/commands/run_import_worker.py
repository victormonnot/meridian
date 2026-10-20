"""Run private sensor imports outside HTTP and outside the VIO executor."""
import signal
import threading
import time
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from workspace import acquisition_queue as queue


class Command(BaseCommand):
    help = "Run the lease-fenced private acquisition import worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--healthcheck", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=2)
        parser.add_argument("--ignore-capacity", action="store_true")

    def handle(self, *args, **options):
        if options["healthcheck"]:
            if not queue.service_status()["worker_available"]:
                raise CommandError("No recent import worker heartbeat.")
            return
        if not settings.MERIDIAN_UPLOADS_ENABLED:
            raise CommandError("Private uploads are disabled.")
        if not 0.1 <= options["poll_seconds"] <= 10:
            raise CommandError("Poll interval must be between 0.1 and 10 seconds.")
        if options["ignore_capacity"] and not settings.DEBUG:
            raise CommandError("Capacity bypass is only available in local debug mode.")
        database = settings.DATABASES["default"]
        if database["ENGINE"] == "django.db.backends.postgresql":
            database["OPTIONS"].update({"connect_timeout": 5,
                "options": "-c statement_timeout=5000 -c lock_timeout=3000",
                "keepalives_idle": 5, "keepalives_interval": 5, "keepalives_count": 2,
                "tcp_user_timeout": 10000})
        from workspace.acquisition_worker import capacity_available, collect_scratch, reconcile, run_one, validate_storage
        validate_storage()
        shutdown, worker_id = threading.Event(), uuid.uuid4()
        old = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig in old:
            signal.signal(sig, lambda *_: shutdown.set())
        collected = 0.0
        try:
            while not shutdown.is_set():
                try:
                    close_old_connections()
                    reconcile()
                    if time.monotonic() - collected > 30:
                        collect_scratch()
                        queue.cleanup_abandoned()
                        collected = time.monotonic()
                    available = options["ignore_capacity"] or capacity_available()
                    queue.pulse_import(available)
                    did_work = run_one(worker_id, shutdown) if available else False
                    if options["once"]:
                        return
                    if not did_work:
                        shutdown.wait(options["poll_seconds"])
                except Exception as error:
                    self.stderr.write("Import service iteration failed: " + type(error).__name__)
                    if options["once"]:
                        raise CommandError("Import service iteration failed.") from None
                    shutdown.wait(5)
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
