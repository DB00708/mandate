import os


def _f(name, default):
    return float(os.getenv(name, default))


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://mandate:mandate@postgres:5432/mandate")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
TEST_MODE = os.getenv("MANDATE_TEST_MODE", "0") == "1"
DEMO_MODE = os.getenv("MANDATE_DEMO_MODE", "1") == "1"
ONBOARDING_TOKEN = os.getenv("ONBOARDING_TOKEN", "sharma-onboarding-dev")

HOLD_TTL_SECONDS = _f("HOLD_TTL_SECONDS", 900)                 # budget holds expire after 15 minutes
ESCALATION_TTL_SECONDS = _f("ESCALATION_TTL_SECONDS", 6 * 3600)
RULE_CHANGE_TTL_SECONDS = _f("RULE_CHANGE_TTL_SECONDS", 3600)
CONFIRM_TTL_SECONDS = _f("CONFIRM_TTL_SECONDS", 600)
COOLING_OFF_SECONDS = _f("COOLING_OFF_SECONDS", 600)           # 10-minute notice to all owners
RECONCILE_AFTER_SECONDS = _f("RECONCILE_AFTER_SECONDS", 30)
WORKER_TICK_SECONDS = _f("WORKER_TICK_SECONDS", 0.5)
MAX_OUTBOX_ATTEMPTS = int(os.getenv("MAX_OUTBOX_ATTEMPTS", "12"))
BREAKER_THRESHOLD = int(os.getenv("BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN_SECONDS = _f("BREAKER_COOLDOWN_SECONDS", 10)
TIER3_LOCK_AFTER = int(os.getenv("TIER3_LOCK_AFTER", "3"))     # reported voice attempts before lockout
TIER3_LOCK_SECONDS = int(os.getenv("TIER3_LOCK_SECONDS", str(24 * 3600)))
SPEAKER_MATCH_THRESHOLD = int(os.getenv("SPEAKER_MATCH_THRESHOLD", "70"))
ODD_HOUR_CHECK = os.getenv("ODD_HOUR_CHECK", "1") == "1"
RHYTHM_REMIND_DAYS = _f("RHYTHM_REMIND_DAYS", 2)
QUIET_HOURS = (22, 7)
