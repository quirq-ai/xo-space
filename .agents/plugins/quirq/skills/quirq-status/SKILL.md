---
name: quirq-status
description: Check whether the local Quirq control plane is installed and running, and report its state. Check-only — never installs, starts, stops, or updates anything.
---

Check the state of the local Quirq control plane. This skill is
**check-only**: it never installs, starts, stops, or updates anything.

1. Run the discovery script from the sibling `quirq` skill
   (`../quirq/scripts/discover.sh` relative to this SKILL.md's directory)
   and parse the JSON it prints.

2. If `state` is `running`:
   - `curl -fsS <base_url>/health` and `curl -fsS <base_url>/api/runtime-config`.
   - Report concisely: port, stage, auth state (authenticated or not, token
     source), active sessions, projects/state roots, and the UI link
     `<base_url>/space/`.

3. If `state` is `installed`:
   - Report the checkout location (`repo_dir`) and that no server is
     running, then ask whether the user wants it started (the
     `quirq-start` skill does that). Do not start it yourself without an
     explicit yes.

4. If `state` is `not_installed`:
   - Report that no install was found (no pointer file, no live server, no
     checkout in the current directory). Mention that the `quirq-install`
     skill installs it, and that the directory choice is theirs.

Report faithfully — if `/health` returns an error or unexpected output,
show what came back instead of interpreting around it.
