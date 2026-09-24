"""Environment configuration and startup validation for the TurnTrace backend.

Fails fast (naming only the missing variables, never values) when required
provider credentials are absent, per CLAUDE.md security rules.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

REQUIRED_ENV_VARS = ("DEEPGRAM_API_KEY", "GOOGLE_API_KEY", "CARTESIA_API_KEY")


def check_required_env_vars() -> None:
    """Raise a RuntimeError naming any missing required env vars (names only)."""
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )


FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000")

# Gate B: where per-session artifacts (recording.wav, session.json) land.
# Resolved from this file's location, not the process cwd, so it is stable
# regardless of where the server is launched from.
SESSIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "sessions"

# Gate D config (read here so the single source of truth lives in one module;
# not used until the freeze-injection processor is implemented in a later gate).
FREEZE_AFTER_ASSISTANT_TURNS = int(os.environ.get("FREEZE_AFTER_ASSISTANT_TURNS", "2"))

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
CARTESIA_VOICE_ID = os.environ.get(
    "CARTESIA_VOICE_ID", "86e30c1d-714b-4074-a1f2-1cb6b552fb49"
)

# Bounded retry for transient Gemini server errors (500/502/503/504), applied
# via google-genai's HttpRetryOptions. 429 is intentionally excluded: quota
# exhaustion won't recover within this window. See
# .claude/tasks/002-gate-a-error-lifecycle-fix.md for the incident this fixes.
GEMINI_RETRY_ATTEMPTS = 3
GEMINI_RETRY_INITIAL_DELAY = 0.5
GEMINI_RETRY_MAX_DELAY = 2.0
GEMINI_RETRY_EXP_BASE = 2
GEMINI_RETRY_HTTP_STATUS_CODES = (500, 502, 503, 504)
