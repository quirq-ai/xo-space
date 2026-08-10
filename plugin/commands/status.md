---
description: Check whether Quirq is installed and running, and report its state
---

Check the state of the local Quirq control plane. This command is
**check-only**: it never installs, starts, stops, or updates anything.

1. Run: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/discover.sh"` and parse the JSON.

2. If `state` is `running`:
   - `curl -fsS <base_url>/health` and `curl -fsS <base_url>/api/runtime-config`.
   - Report concisely: port, stage, auth state (authenticated or not, token
     source), active sessions, projects/state roots, and the UI link
     `<base_url>/space/`.

3. If `state` is `installed`:
   - Report the checkout location (`repo_dir`) and that no server is
     running, then ask whether the user wants it started (they can also run
     `/quirq:start`). Do not start it yourself without an explicit yes.

4. If `state` is `not_installed`:
   - Report that no install was found (no pointer file, no live server, no
     checkout in the current directory). Mention that `/quirq:install
     <directory>` installs it, and that the directory choice is theirs.

Report faithfully — if `/health` returns an error or unexpected output,
show what came back instead of interpreting around it.
