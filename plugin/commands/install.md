---
description: Install Quirq into a directory the user chooses (one-time, explicit)
argument-hint: <workspace-directory>
---

Install Quirq (xo-space) on this machine. The workspace directory is
`$ARGUMENTS`.

**Rules — read before acting:**
- The workspace directory is the user's decision. If `$ARGUMENTS` is empty,
  ask for it and stop — never pick, suggest, or default one.
- This is a one-time action. If Quirq is already installed or running, never
  reinstall over it; report what you found instead.

**Steps:**

1. Run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/discover.sh"`.
   - `running` → report the server and its `/space/` URL; stop.
   - `installed` → report the existing checkout location; stop.

2. Validate the directory: expand `~`, require an absolute path after
   expansion. Create it only if the user gave a path that doesn't exist yet
   and confirms creation.

3. Confirm once, showing exactly what will happen: "Installing Quirq into
   `<dir>` — this clones xo-space there, sets up a Python env, and starts
   the server (the directory becomes your workspace root)." Proceed only on
   an explicit yes.

4. Run the official installer **as a background task of this session** —
   it runs the server in its foreground and never returns, and the
   server's output goes to `<dir>/.quirq/quirq.log`, so the task itself
   stays quiet:

   ```bash
   cd <dir> && curl -fsSL https://www.quirq.ai/install | sh
   ```

5. Poll `http://127.0.0.1:5002/health` (then 5003) every ~5 s, up to
   ~5 minutes — the first install builds a Python env and can be slow.

6. On success: report the UI at `http://127.0.0.1:<port>/space/`, the log
   file at `<dir>/.quirq/quirq.log`, and the config file at
   `<dir>/xo-space/.env` — and say clearly that the server runs only as
   long as this session (or until the background task is killed);
   `/quirq:start` brings it back in a later session.

7. On timeout or failure: show the tail of `<dir>/.quirq/quirq.log`
   verbatim and stop. Do not retry on your own.
