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

4. Start it **in the background**:

   ```bash
   cd <repo_dir> && ./venv/bin/python server.py
   ```

5. Poll `/health` on 5002 then 5003 (the server falls back automatically)
   for up to ~60 s.

6. On success report port and `<base_url>/space/`. On failure show the
   server's output verbatim and stop — no retries, no cleanup attempts.
