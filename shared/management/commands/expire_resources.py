"""One-shot expiry for an external scheduler, or explicit --watch polling."""
import json
import logging
import math
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from shared.services import run_expiry

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Expire resources and reconcile quantities atomically per source (Rev 2 section 5.4)."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=200)
        parser.add_argument("--watch", action="store_true", help="Repeat explicitly; the default is one cycle.")
        parser.add_argument("--interval", type=float, default=None, help="Seconds between --watch cycles.")

    def handle(self, *args, **options):
        interval = options["interval"]
        if interval is None:
            interval = settings.EXPIRY_INTERVAL_SECONDS
        if not math.isfinite(interval) or interval <= 0 or options["batch_size"] < 1:
            raise CommandError("interval and batch-size must be positive finite values.")
        try:
            while True:
                started = time.monotonic()
                try:
                    result = run_expiry(batch_size=options["batch_size"])
                except Exception as exc:
                    raise CommandError("Expiry cycle failed; see the backend log. Completed source transactions are retained.") from exc
                result["duration_ms"] = round((time.monotonic()-started)*1000, 2)
                logger.info("Expiry cycle: %s", result)
                self.stdout.write(json.dumps(result, sort_keys=True))
                if not options["watch"]:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stderr.write("Expiry watch stopped.")
