"""XO Cowork API tests."""

# Snapshot the missing-tools flags and PATH before any test imports server.py
# (see tests/required_tools.py).
from tests import required_tools as _required_tools  # noqa: F401

# Routes that moved to qq run `qq <command>` first (routers/qq_ops.py). Tests exercise the
# in-process code with their own mocks, which a qq subprocess would not see, so here qq is not
# installed and those routes fall back. Tests of the bridge itself point it back at a stand-in.
from services import qq_runner as _qq_runner  # noqa: E402

_qq_runner.QQ = "qq-is-not-installed-in-tests"
