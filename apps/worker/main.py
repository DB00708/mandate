"""Background worker: outbox dispatcher, reconciler, hold/approval expiry, cooling-off, rhythm reminders."""
import logging
import signal
import time

from apps.api.services import queries, saga
from apps.common import config
from apps.common.db import migrate, redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mandate.worker")
running = True


def stop(*_):
    global running
    running = False


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for i in range(30):
        try:
            migrate()
            break
        except Exception as e:  # postgres still starting
            log.info("waiting for database: %s", e)
            time.sleep(1)
    log.info("worker started (tick=%ss, hold_ttl=%ss, cooling_off=%ss)", config.WORKER_TICK_SECONDS,
             config.HOLD_TTL_SECONDS, config.COOLING_OFF_SECONDS)
    last_rhythm = 0.0
    while running:
        try:
            if redis().get("worker:paused"):
                time.sleep(config.WORKER_TICK_SECONDS)
                continue
            saga.tick()
            if time.time() - last_rhythm > 60:
                last_rhythm = time.time()
                queries.remind_running_low()
        except Exception:
            log.exception("tick failed")
        time.sleep(config.WORKER_TICK_SECONDS)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
