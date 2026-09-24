---
name: quirq-status
description: Check whether XO Space is installed and running, and report its URL and workspace. Read-only; never installs, starts, stops or updates the server.
---

Resolve `<plugin-root>` as two directories above this skill directory and run:

```bash
bash "<plugin-root>/scripts/discover.sh"
```

Parse the JSON and report:

- `running`: verify `/health` and `/api/runtime-config` on `base_url`, then give
  the UI link `<base_url>/space/`, active backend, projects/state roots and health.
  Report auth as authenticated/not authenticated only; never print credentials.
- `installed`: report `repo_dir` and that the server is stopped. Explain that
  asking to open Space or invoking `quirq-start` starts that checkout.
- `not_installed`: say no install was found. Explain that asking to open Space
  or invoking `quirq-install` installs it into a chosen workspace (default
  `~/xo-workspace`).

Do not mutate anything for this request. Report failed/unexpected API responses
faithfully and redact credentials from any diagnostics.
