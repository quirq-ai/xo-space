---
name: quirq-start
description: Start an already-installed Quirq server exactly as it is on disk — no update, no installer re-run, explicit consent required.
---

Start the locally installed Quirq server. **Starting is not updating**: this
runs the checkout exactly as it is on disk. It never fetches, pulls, or
re-runs the installer.

1. Run the discovery script from the sibling `quirq` skill
   (`../quirq/scripts/discover.sh` relative to this SKILL.md's directory).
   - `running` → already up; report the base URL and stop.
   - `not_installed` → nothing to start; suggest the `quirq-install` skill
     and stop.
   - `installed` → continue with `repo_dir` from the output.

2. Preconditions in `repo_dir`: `venv/bin/python` must exist (if missing,
   the env was never built — tell the user to run `./install.sh` there
   themselves, since that path also updates; stop). `.env` should exist and
   is picked up automatically.

3. Unless the user already explicitly asked to start it in this
   conversation, confirm: "Start Quirq from `<repo_dir>`?" Proceed only on
   an explicit yes.

4. Start it **as a background task of this session** — the server runs
   until this session ends or the task is killed. Send its output to the
   state log so the task stays quiet, using `state_root` from the
   discovery output (fall back to `<repo_dir>/.quirq`, creating it
   first):

   ```bash
   cd <repo_dir> && mkdir -p "<state_root>" && \
     ./venv/bin/python server.py >> "<state_root>/quirq.log" 2>&1
   ```

5. Poll `/health` on 5002 then 5003 (the server falls back automatically)
   for up to ~60 s.

6. On success report port, `<base_url>/space/`, and the log path
   (`<state_root>/quirq.log`) — and that the server runs only as long as
   this session. On failure show the tail of the log verbatim and stop —
   no retries, no cleanup attempts.
