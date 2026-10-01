"""The brain's settings: the one module that reads its environment.

Every value is read on each call, so a changed ``.env`` applies at the next
tick or request without a restart (the pollers do the same).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Background re-learning, fading and discovery. The API works without it.
ENV_ENABLED = "BRAIN_ENABLED"
ENV_TICK = "BRAIN_TICK_S"
#: ``none`` (statistics only), ``agent`` (the active agent), a module in
#: ``services/brain/model/`` or a dotted import path to your own.
ENV_MODEL = "BRAIN_MODEL"
ENV_MODEL_CALLS = "BRAIN_MODEL_MAX_CALLS"
ENV_MAX_FILE_BYTES = "BRAIN_MAX_FILE_BYTES"
ENV_MAX_FILES = "BRAIN_MAX_FILES"
ENV_FADE = "BRAIN_FADE_PER_DAY"
ENV_BUILD_TIMEOUT = "BRAIN_BUILD_TIMEOUT_S"
ENV_TEST_TIMEOUT = "BRAIN_TEST_TIMEOUT_S"
ENV_FIX_ATTEMPTS = "BRAIN_FIX_ATTEMPTS"
#: The harness that builds approved designs; empty means the active agent.
ENV_BUILD_AGENT = "BRAIN_BUILD_AGENT"

DEFAULT_TICK_S = 600.0
MIN_TICK_S = 30.0


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _number(name: str, default: float, *, minimum: float, maximum: float | None = None) -> float:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = max(minimum, float(raw))
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    return min(value, maximum) if maximum is not None else value


def loop_enabled() -> bool:
    return _flag(ENV_ENABLED, False)


def tick_seconds() -> float:
    return _number(ENV_TICK, DEFAULT_TICK_S, minimum=MIN_TICK_S)


def model_name() -> str:
    return (os.getenv(ENV_MODEL, "") or "").strip() or "none"


def model_calls_per_learn() -> int:
    """Model calls one learn run may spend on extraction; chunks past the
    budget are read statistically, so a large project cannot run up a bill."""
    return int(_number(ENV_MODEL_CALLS, 40, minimum=0))


def max_file_bytes() -> int:
    return int(_number(ENV_MAX_FILE_BYTES, 200_000, minimum=1_000))


def max_files() -> int:
    return int(_number(ENV_MAX_FILES, 3000, minimum=1))


def fade_per_day() -> float:
    """The share of an unused link's weight lost per day."""
    return _number(ENV_FADE, 0.02, minimum=0.0, maximum=0.5)


def build_timeout_s() -> float:
    return _number(ENV_BUILD_TIMEOUT, 1800.0, minimum=60.0)


def test_timeout_s() -> float:
    return _number(ENV_TEST_TIMEOUT, 600.0, minimum=5.0)


def fix_attempts() -> int:
    return int(_number(ENV_FIX_ATTEMPTS, 2, minimum=0, maximum=5))


def build_agent() -> str:
    return (os.getenv(ENV_BUILD_AGENT, "") or "").strip()
