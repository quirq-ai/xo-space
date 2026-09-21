#!/usr/bin/env python3
"""git credential helper: hand out the GitHub token only for selected repositories.

Installed into ~/.gitconfig by the GitHub connector while repository access is
"selected". The logic lives in services/cowork_agent/connectors/github/git_credential.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.cowork_agent.connectors.github.git_credential import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
