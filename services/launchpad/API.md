# Launchpad deploy: the HTTP contract

The Space's deploy API, for xo-coworker's Deploy panel. Everything runs in the
xo-space server; the browser never sees a GitHub token or the XO key.

- `flow.json` (beside this file) is the flow's one definition: its states, its
  steps with their titles, every error's message and fix, the decisions only
  the owner can make, and the Dockerfile templates. `GET /api/deploy/flow`
  serves it, so the UI can draw the step list and show the same words the
  script uses.
- `launchpad.py` is the script that runs it. Each deploy, watch, preflight,
  link and unlink is one run of the script; `check` is the same code called
  in-process.
- `{project}` is the project's folder name, as in `/api/xo-projects/{project_id}`.

Errors outside a stream are `HTTP <status>` with `{"detail": {"code", "message"}}`,
like every Space route. `404 project_not_found` for an unknown project.

## Endpoints

| Method | Path | What it does | Network |
|---|---|---|---|
| GET | `/api/deploy/flow` | flow.json | no |
| GET | `/api/deploy/sizes?template=github` | Machine sizes with CPU and memory, the template's default and allowed ones | yes, cached 10 min |
| GET | `/api/deploy/{project}/check` | The project's state and the filled-in form, plus what runs now | no (files only) |
| GET | `/api/deploy/{project}/preflight?name=` | What a deploy would meet on GitHub and Launchpad; changes nothing | yes, ~1-3 s |
| POST | `/api/deploy/{project}` | Deploy; a server-sent event stream | yes |
| DELETE | `/api/deploy/{project}` | Stop the running deploy | no |
| GET | `/api/deploy/{project}/watch` | Live status stream: the running deploy, else the latest build and release | yes |
| GET | `/api/deploy/{project}/apps` | The account's Launchpad apps, best matches first, for linking | yes |
| POST | `/api/deploy/{project}/link` | Link an existing app: `{"app": "<id or exact name>"}` | yes |
| DELETE | `/api/deploy/{project}/link?forget=false` | Unlink: drop the app from launchpad.json (the app keeps running) | no |
| POST | `/api/deploy/{project}/agent` | Start a "Deploy to Launchpad" thread in the project: an agent prepares it | no |

POST bodies are JSON (`415 json_required` for another content type; a body may
be `{}`), and every write passes Space's browser write guard (`ALLOWED_ORIGINS`,
same origin, or a same-machine proxy), like every other Space write.

## The states

`check.state` is one of:

| state | meaning | the panel offers |
|---|---|---|
| `linked` | launchpad.json names an app | Deploy (pushes the branch), Watch, Unlink |
| `ready` | Dockerfile at the root and a known port | Deploy in one click |
| `detectable` | Deployable once the owner confirms: a guessed or missing port, or a known stack (static, Node, Python) that gets a generated Dockerfile (`build.dockerfile_preview`) | the filled-in form, then Deploy |
| `needs-agent` | Code, but not a shape the script can deploy (`reasons`) | Ask agent to deploy |
| `not-deployable` | No code | nothing |

`blockers` (a non-empty list) means Deploy would refuse now even in a deployable
state: no branch checked out, or the folder is on another branch than the app's.

## GET /api/deploy/{project}/check

Reads files only, answers in milliseconds; call it whenever the panel opens.

```json
{
  "schema": 1,
  "project": "hello-docker-test",
  "state": "ready",
  "summary": "Ready to deploy as hello-docker-test on port 3000. It deploys to its origin dwivedi-ai/hello-launchpad-test if you administer it, else to a new private repo of yours.",
  "reasons": [],
  "blockers": [],
  "warnings": [],
  "config": null,
  "config_error": null,
  "fields": {
    "name": {"value": "hello-docker-test", "source": "the project folder"},
    "port": {"value": 3000, "source": "Dockerfile EXPOSE", "confident": true},
    "health_path": {"value": null, "source": "default: Launchpad checks that the port accepts connections"},
    "env": [
      {"name": "DATABASE_URL", "sensitive": true, "source": ".env.example"},
      {"name": "LOG_LEVEL", "sensitive": false, "source": ".env.example", "default": "info"}
    ],
    "repo": {"full_name": null, "remote": null, "branch": "main",
             "origin": "dwivedi-ai/hello-launchpad-test", "new_repo_name": "hello-docker-test", "own": false}
  },
  "build": {"dockerfile": "Dockerfile", "stack": "docker", "template": null, "port": 3000,
            "port_source": "Dockerfile EXPOSE", "port_confident": true, "health_path": null,
            "dockerfile_preview": null},
  "git": {"repo": true, "branch": "main", "detached": false, "head": "b5a49e8…", "no_commits": false,
          "origin": "https://github.com/dwivedi-ai/hello-launchpad-test.git",
          "origin_github": "dwivedi-ai/hello-launchpad-test", "remotes": ["origin"], "deployed_head": null},
  "prerequisites": {"github_connected": true, "github_source": "gh", "launchpad_key": true, "launchpad_cli": true},
  "running": null,
  "last": null
}
```

- `fields.*.source` says where each value came from: `launchpad.json`,
  `Dockerfile EXPOSE`, `Dockerfile ENV PORT`, `package.json start script`,
  `the server code`, `Next.js`, `FastAPI (uvicorn)`, `Procfile`, `nginx`,
  `the project folder`, or `default (…)`. `port.confident: false` means a guess:
  show it as editable and ask to confirm.
- `summary` names where a deployable project would push. `fields.repo.own`: launchpad.json's
  `repo` is one of the project's own remotes; when it names another repo, `warnings` says so and
  the deploy asks first (`needs confirm_repo`).
- `fields.env`: names only. `sensitive` = mask the input; `default` (non-sensitive
  only) is the example file's value. Nothing says which are required: the app's
  code decides, so all are optional in the form.
- `prerequisites.github_source` says which GitHub login a deploy uses, the first
  found of: `connections` (the token saved in Connections → GitHub), `gh` (this
  machine's own `gh auth login`, reused, so nobody signs in twice), `env`
  (`GH_TOKEN`/`GITHUB_TOKEN` in the Space's environment); `null` with
  `github_connected: false` when there is none. Its scopes are checked by preflight.
- `config` is launchpad.json once linked: `app_id`, `name`, `slug`, `url`, `workflow`
  (the build workflow's file name), `repo {full_name, remote, branch}`, `port`,
  `health_path`, `size`, `storage {path, size_gb}`, `env [{name, sensitive}]`.
  `fields.size` and `fields.storage` give the size and disk from launchpad.json
  (`value: null` = the template's default / no disk).
- The app's address is Launchpad's: show `url` (the app's `public_url`, its first live
  custom domain, else its own `https://<name>-xo-<random>.<Launchpad's apps domain>`).
  Never build one from the name; preflight's `launchpad.app.url` is the live one.
  The linked app's id and URL come from here instantly; its live status,
  release number and last deploy time come from preflight.
- `git.deployed_head` is the commit last pushed to the deploy repo (as of the
  last deploy or fetch); `head` differing from it means there are undeployed commits.
- `running`: `null`, or the deploy (or watch) running right now:
  `{"run_id", "kind": "deploy"|"watch", "started_at", "step": "build", "last": {last event}}`.
  When it is a deploy, open `/watch` to follow it.
- `last`: the last finished deploy since the server started: `{"run_id", "kind",
  "started_at", "finished_at", "outcome": {the done/error/needs event}}`, or null.

## GET /api/deploy/{project}/preflight

Asks GitHub and Launchpad; changes nothing. Each part reports its own `error`
(an error code below), so one service being down still shows the other. Call it
when the panel opens, after `check`.

```json
{
  "state": "ready",
  "github": {"connected": true, "source": "gh", "login": "dwivedi-ai",
             "scopes": ["gist", "read:org", "repo", "workflow"], "scopes_known": true, "workflow_scope": true},
  "repo": {"branch": "main", "origin": "dwivedi-ai/hello-launchpad-test", "origin_reachable": true,
           "is_admin": true, "default_branch": "main", "mode": "origin",
           "full_name": "dwivedi-ai/hello-launchpad-test", "remote": "origin"},
  "launchpad": {"key": true, "cli": true, "name_free": true,
                "sizes": {"template": "github", "default": "xo.medium",
                          "allowed": ["xo.mini", "xo.small", "xo.medium", "xo.large"],
                          "tiers": [{"name": "xo.mini", "label": "Mini", "cpu": 0.5, "memory_gb": 1}, "…"],
                          "fixed_after_create": true, "source": "launchpad"}}
}
```

- `github`: `source` as in check. `connected: false` (with `error:
  "github_token_invalid"` and a `fix` when the token was rejected) → Connect GitHub.
  `workflow_scope: false` comes with a `fix`: for a `gh` login it is
  "Run `gh auth refresh -h github.com -s workflow` … or connect GitHub in
  Connections", otherwise Connect or reconnect GitHub (below). `scopes_known: false`
  (a fine-grained token) → no verdict.
- `repo.mode`, in the order the deploy picks:
  - `recorded`: launchpad.json's repo: `reachable`, `can_push`, `private`, `default_branch`,
    and `own` (one of the project's own remotes: origin or a remote this machine added). When `own` is
    false, and launchpad.json's `app_id` isn't an app of the owner's that already builds that repo, the deploy stops with `needs confirm_repo` until the owner confirms the repo (`use_repo`):
    launchpad.json is the project author's file, so a cloned project's file can name any repo.
  - `remote`: a `launchpad` git remote from an earlier deploy (its launchpad.json
    deleted since), administered by the user; deploys push there again.
  - `origin`: the project's origin, the user is admin; deploys push there.
  - `new`: a new private repo: `full_name` (`<login>/<new_repo_name>`), `exists`
    (true = the name is taken on the account; the deploy reuses it only if it
    is empty or holds this project's history), `reason` (why not origin). The
    deploy needs `create_repo: true` (and `repo_name` to change the name).
- `launchpad.app` (when linked): `{"id", "name", "slug", "status", "url", "repo",
  "branch", "port", "exists": true, "release": 4, "rollout": "ready",
  "last_deploy_at": "2026-11-05T12:01:40Z", "ready": 1, "desired": 1}`;
  `exists: false` means it was deleted on Launchpad: offer Recreate or Unlink.
  `launchpad.env_set`: `{"DATABASE_URL": true, ...}`, which declared names have a value on the app.
- `launchpad.sizes` (not linked): what the size picker offers, as `GET /api/deploy/sizes`
  answers it. A linked app's `launchpad.app` has its `size`, `size_label`
  ("xo.medium, 1 CPU and 4 GB memory") and `storage` (`{"path", "size": "2Gi"}` or null).
- `launchpad.name_free` (not linked): false with `same_name: {app…, "linkable": true}`
  means an app with this name already builds this repo: offer Link; `linkable: false`
  means the name is taken: ask for another.

## POST /api/deploy/{project}: deploy

Body, every field optional:

```json
{
  "name": "my-app",
  "port": 3000,
  "health_path": "/health",
  "create_repo": true,
  "repo_name": "my-app",
  "use_repo": "owner/repo",
  "recreate": false,
  "rebuild": false,
  "size": "xo.small",
  "storage_path": "/data",
  "storage_size": 2,
  "env": {"DATABASE_URL": "postgres://…", "LOG_LEVEL": "info"},
  "sensitive": ["DATABASE_URL"]
}
```

- `name` 1-25 lowercase letters, digits, hyphens. Omitted: launchpad.json's, else the folder's.
- `port`, `health_path` (`""` = a port check, omitted = keep): change a linked app's too.
- `size`: the machine size for a **new** app, `xo.mini`, `xo.small`, `xo.medium` or
  `xo.large` (the short names `mini` … `large` work too). Omitted: launchpad.json's,
  else the template's default (`xo.medium` for github today). Launchpad fixes the size
  when the app is created: for a linked app a different size is not applied; the
  deploy says so in a `warning` ("Recreate the app to run it on …"). Offer only the
  template's `allowed` sizes; a size Launchpad refuses ends with `size_not_allowed`.
- `storage_path` (+ `storage_size`, 1-20 GB, default 2): a persistent disk for a
  **new** app, mounted at that absolute path. A linked app's disk can grow (a larger
  `storage_size` is applied with `app storage resize`), never shrink, and cannot be
  added after creation (both are warnings). `storage_size` without `storage_path`: 422.
- What Launchpad has no setting for: CPU or memory beyond the size, region, instance
  count (a running app has one), a size change after creation.
- Repo choice: nothing to send when the user administers origin (or launchpad.json
  names the repo). Otherwise `create_repo: true`, optionally with `repo_name`.
  Without it the deploy stops with `needs create_repo` before touching anything.
- `use_repo`: confirms launchpad.json's repo (`owner/repo`, exactly as `needs confirm_repo`'s
  `data.repo`) when it isn't one of the project's own remotes. Once pushed, the repo is added as a
  remote, so later deploys don't ask again.
- `recreate`: the linked app was deleted on Launchpad; create it again.
- `rebuild`: nothing new to push, build the same commit again (an empty commit).
- `env`: values for the app, set on Launchpad by this deploy. They go from this
  request to the script over stdin and to xo-launchpad through its environment,
  then to Launchpad. They are never logged (the server logs no bodies; the
  command log records argv, and Launchpad calls without their output), never
  stored by the Space, never written to launchpad.json, and never reach an agent.
  launchpad.json records only the names and `sensitive`. Names are marked
  sensitive when listed in `sensitive`, declared so, or named like a secret
  (`…_KEY`, `TOKEN`, `PASSWORD`, `DATABASE_URL`, …).

The answer is `text/event-stream`. One deploy runs per project: a POST while
one runs attaches to it (its body is ignored) and replays its events. The
deploy goes on when the client goes away; `/watch` re-attaches.

## The event stream (deploy and watch)

Each event is `data: {json}\n\n`. Every event has `type` and `ts` (ISO UTC). A
run's own events (all but `run` and `ping`) also carry `run_id` and `seq` (1, 2,
3… within the run), and their frame has the SSE id `id: <run_id>:<seq>`; the
first frame sets `retry: 3000`. Every 15 s of quiet: an SSE comment `: ping`
and `{"type": "ping"}`.

Resuming: a viewer that loses the stream reopens `GET /watch` with
`Last-Event-ID: <run_id>:<seq>` (a browser's EventSource sends it by itself) or
`?run=<run_id>&after=<seq>` (fetch). It gets only the events after that one
(`run.after` says from where). If it had already received the run's last event,
the answer is `204 No Content` (EventSource stops reconnecting on 204): close
the panel's stream on `done`/`error`/`needs`/`replaced` anyway. A run the server
no longer knows (it restarted) is not an error: the watch starts afresh.

| type | fields | meaning |
|---|---|---|
| `run` | `run_id`, `kind` (`deploy`/`watch`), `attached`, `started_at`, `after` | first event of every stream |
| `step` | `step`, `title`, `status` (`running`, `done`, `skipped`, `failed`, `waiting`), `message`, `elapsed` (s, on the closing event) | a step starts or ends |
| `progress` | `step`, `message`, `data` | inside a step (below) |
| `warning` | `step`, `message` | worth showing, not fatal (uncommitted changes, env names with no value, a 404 at /) |
| `status` | `app`, `release`, `live` | watch only: the app as it is when the watch starts |
| `done` | `url`, `http_status`, `app {id, name, slug}`, `repo`, `branch`, `commit`, `release`, `created` | the end: live |
| `error` | `step`, `code`, `message`, `fix`, `action`, `data` | the end: failed |
| `needs` | `step`, `code`, `message`, `fix`, `action`, `options [{id, label}]`, `data` | the end: a decision for the owner; deploy again with the answer |
| `replaced` | `by {run_id, kind: "deploy"}` | watch only, the end: a deploy started; follow it (the POST that started it streams it, or reopen `/watch`) |

The stream closes after `done`, `error`, `needs` or `replaced`.

`action` is what the panel offers, one of flow.json `actions` (each with a
sentence there): `connect_github`, `reconnect_github`, `sign_in_xo`,
`reinstall_space`, `ask_agent`, `retry`, `open_build_log`, `watch`,
`edit_settings`, `pull_changes`, `create_repo`, `rename_repo`, `link`,
`recreate_or_unlink`, `unlink`, `pick_app`, `deploy`, `manual` (show the
message and fix only), `none`. Every code in flow.json `failures` has one; HTTP
errors outside a stream carry it too (`detail.action`).

Steps, in order (titles in flow.json): `check`, `credentials`, `repo`, `app`,
`prepare`, `push`, `create` (new apps only), `env`, `build`, `release`, `url`.
Watch runs `build`, `release`, `url`. For a panel with six rows:
repo = `credentials`+`repo`; link = `app`+`create`; push = `prepare`+`push`;
build = `build`; release = `env`+`release`; url = `url`.

`progress.data` by step:
- `build`: `run_id`, `run_url` (the Actions run page), `status` (`queued`, `in_progress`, `completed`);
  the message names the job step running now ("Building: Build and push").
- `release`: `release` (number), `rollout` (`progressing`, `ready`, …), `app_status`,
  `ready` and `desired` (instances, n/m).

Timing: the build is asked about every 3 s and the release every 2 s
(flow.json `timeouts`), so the panel hears of each change within that.
- `create`: `app_id`, `url` once the app exists.

## Error and decision codes

`error` codes (message and fix in flow.json `failures`):

| code | when | data |
|---|---|---|
| `github_not_connected` | no GitHub token | |
| `github_token_invalid` | GitHub rejected the token | |
| `github_workflow_scope` | the token lacks `workflow` (or `repo`); also when Launchpad's workflow write is refused | `scopes`, `source` |
| `github_unreachable` | GitHub down or rate-limited | |
| `launchpad_key_missing` / `launchpad_key_invalid` | no XO key / rejected | |
| `launchpad_forbidden` | Launchpad refused the account (access or plan, HTTP 402/403) | |
| `launchpad_limit` | app limit (HTTP 429) | |
| `launchpad_unreachable` | Launchpad down | |
| `launchpad_cli_missing` | xo-launchpad not installed | |
| `needs_agent`, `not_deployable`, `config_invalid` | the project is not deployable as it is | `reasons` |
| `detached_head`, `no_commits`, `wrong_branch`, `name_invalid` | fix in the project folder | |
| `repo_unreachable`, `repo_no_push` | launchpad.json's repo is not usable with this account | `repo` |
| `remote_conflict` | a `launchpad` git remote already points elsewhere | |
| `push_rejected`, `remote_ahead` | GitHub has commits the folder lacks: pull first | `git` |
| `push_failed`, `commit_failed` | git failed | |
| `create_failed` | Launchpad refused to create the app; message carries Launchpad's words | |
| `env_failed` | setting env values failed | |
| `size_not_allowed` | an unknown size, or one the template doesn't allow | `size` |
| `build_not_started` | no Actions run appeared for the push | |
| `build_failed` | the Actions run failed | `run_url`, `failed_step`, `conclusion`, `log_tail` (last 40 log lines) |
| `build_timeout`, `release_timeout` | still going after 30 / 15 min; it goes on server-side | `run_url` |
| `health_check_failed` | the release failed Launchpad's check | `kind` (`port`/`health_path`), `port` (tried), `exposed_ports` (the image declares), `health_path`, `diagnosis` |
| `release_failed` | the release failed otherwise, or the app is stopped | `release`, `diagnosis` |
| `not_linked`, `app_not_found`, `already_linked`, `not_a_github_app` | watch / link | |
| `cancelled` | DELETE stopped the deploy | |
| `internal` | a bug; the message says what failed | |

`needs` codes and the answer (flow.json `decisions`):

| code | ask | answer |
|---|---|---|
| `confirm_repo` | launchpad.json names a repo that isn't the project's origin or a remote this machine added (`data.repo`, `data.origin`) | deploy with `use_repo: data.repo` if it is the owner's; else remove `repo` from launchpad.json |
| `create_repo` | "create private repo `<login>/<name>`?" (`data.repo`, `data.reason`) | deploy with `create_repo: true` (+ `repo_name`) |
| `repo_name_taken` | the name holds other code (`data.suggestion`) | deploy with `repo_name` |
| `link_existing` | an app with this name already builds this repo (`data.app`) | POST `/link {"app": data.app.id}`, then deploy |
| `name_taken` | another app has the name (`data.suggestion`) | deploy with `name` |
| `app_missing` | launchpad.json's app was deleted on Launchpad | deploy with `recreate: true`, or DELETE `/link` |
| `port_missing` | the port is unknown | deploy with `port` |

A user who is not admin on origin is not an error: preflight says `repo.mode: "new"`
with the reason, and the deploy asks `needs create_repo`.

## GET /api/deploy/{project}/watch

The same stream. If a deploy is running it attaches to it (replaying its events
from the start, or from after `Last-Event-ID` / `?run=&after=`, `run.attached: true`). Otherwise it starts a watch: the app's
current `status`, then the latest Actions build if one is running, the release
until it is ready, and the URL; one watch per project is shared by every viewer
and stops when the last one leaves; a deploy that starts ends it with `replaced`.
Not linked: one `error not_linked` event.

To show "a deploy is running" when the panel opens: `check.running.kind == "deploy"`,
then open `/watch`.

## Link and unlink

- `GET /api/deploy/{project}/apps` → `{"apps": [{"id", "name", "slug", "source",
  "status", "url", "repo", "branch", "port", "match": ["name", "repo"],
  "linkable": true}], "linked": "<app_id or null>", "name": "<the project's app name>"}`.
  The list is the XO account's Launchpad apps (`xo-launchpad app list`), the
  ones matching this project's name or repo first. Only `linkable` (GitHub-built)
  apps can be linked.
- `POST /api/deploy/{project}/link {"app": "<id or exact name>"}` → `{"config": {launchpad.json},
  "commit": "<sha or null>", "app": {…}}`. Writes and commits launchpad.json (not
  pushed), adds a git remote for the app's repo when the project lacks one.
  `409 already_linked` (unlink first), `404 app_not_found`, `400 not_a_github_app`,
  `409 deploy_running`.
- `DELETE /api/deploy/{project}/link` → `{"unlinked": {"app_id", "name"}, "forgot": false, "commit"}`.
  Drops `app_id`, `slug`, `url` and `workflow` from launchpad.json and commits it; the repo, port,
  health path and env names stay, so a later deploy creates a fresh app from the same
  repo. `?forget=true` deletes launchpad.json instead. The Launchpad app is never
  deleted. `404 not_linked`.

Recovery cases: launchpad.json deleted while the app lives → deploy stops with
`needs link_existing` (or preflight shows `same_name.linkable`) → link. App deleted
while launchpad.json remains → `needs app_missing` → recreate or unlink. Another
machine → the committed launchpad.json and the same XO key just work; a missing
`launchpad` remote is added on the first deploy.

## Connect or reconnect GitHub (the workflow scope)

A GitHub login with the `repo` and `workflow` scopes that this machine already
has is used as it is: Deploy reads `gh`'s own login (check's `github_source: "gh"`)
and nobody signs in twice. The connector routes:

- `GET /api/connectors/github/status` → as before; with nothing saved
  (`status: "needs_auth"`) it adds `gh_login: {"logged_in", "username", "scopes",
  "missing": ["workflow"], "usable"}` (never the token), so the card can offer
  "Use gh login (username)".
- `GET /api/connectors/github/cli/existing` → that same `gh_login` object.
- `POST /api/connectors/github/cli/import` → saves gh's login as the connection:
  `200` with the connection (`{"status": "connected", "username", "scopes", …,
  "imported": true}`), or `409 {"detail": {"code": "gh_not_logged_in" |
  "gh_missing_scopes" | "gh_token_invalid", "message", "missing", …}}`.
- `POST /api/connectors/github/cli/start` → when gh's login is usable it is
  imported instead of starting a device flow: the answer is the connection with
  `status: "connected"`, `imported: true`, `user_code: ""` and a `session_id`
  whose poll returns the connection once. Otherwise (no login, or one missing
  `workflow`) it starts the device flow as before:
  `{"session_id", "user_code", "verification_uri", "expires_in"}`; show the code
  and the link. `?fresh=true` always starts the device flow (to switch accounts).
- `POST /api/connectors/github/cli/poll {"session_id"}` every 2-3 s →
  `{"status": "pending", …}` until it returns the connection
  `{"status": "connected", "username", "scopes": "…workflow…", …}`;
  `404` = expired, start again; `502 {"status": "failed", "error"}`.
  `POST /api/connectors/github/cli/cancel {"session_id"}` aborts.
- A gh login missing `workflow` can also be fixed on the machine with
  `gh auth refresh -h github.com -s workflow`. Then call preflight again.

## Ask an agent to deploy

`POST /api/deploy/{project}/agent {"note": "optional words from the owner", "client_id": "optional, for retries"}`
→ `{"chat_id", "thread_ts", "already": false, "state": "needs-agent"}`.

It starts one thread, "Deploy to Launchpad", in the project's chat: its root
message is the owner's request, and XO briefs the agent with flow.json's rules
and this check. While that thread is still open, the same call returns it with
`already: true`. `503` when the engine is not running.

The agent only prepares (a Dockerfile, the port, launchpad.json without an app,
committed on its thread branch); it never deploys. Nothing deploys by itself when
the thread finishes: follow the thread through the chat API, and when it is done
(and its branch is merged into the project's branch), call `check` again; at
`ready`/`detectable` the Deploy button does the rest.

## What a deploy deploys

The branch checked out in the project folder (normally `main`), at its last
commit. Never a thread's worktree or branch: a thread's changes deploy once
they are merged into that branch. Uncommitted changes are not deployed (a
`warning` says how many). A Deploy button on a thread row deploys that
thread's project.
