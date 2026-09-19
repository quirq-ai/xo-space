# Run a trained fly in XO Space

The Fly adapter runs a **Report Scout** exported by the training lab. Copy its
`.fly.json` into Space's state directory; Space supplies the executable runtime.
The JSON contains a learned file-ranking policy and a task template, never
executable code. Execution happens on the machine running Space, with no LLM,
provider key, or connection to the training app.

For example, Failure Scout chooses which JSON/CSV job records to inspect within
a read budget, then reports the recorded status and error code with file, row
or JSON location, and SHA-256 citations. It can surface evidence such as
`AUTH_EXPIRED` and `TOKEN_REFRESH_FAILED`. It does not infer root causes or repair
the workspace. The query influences file ranking; it is not a row filter or a
natural-language instruction interpreter.

## Install and select

Requires the normal Space Python environment and **Node.js 20+** on PATH.
`FLY_NODE_PATH` may name a trusted installed Node binary. The adapter does not
download or install a runtime. Linux, macOS, and WSL are supported.

Use the `QUIRQ_STATE_ROOT` and `XO_PROJECTS_ROOT` of your Space installation.
The installer normally sets the state root to `<launch directory>/.quirq`;
direct server runs without that setting use `~/.quirq`.

```bash
# From the Space checkout, with your installation's environment loaded:
node --version
mkdir -p "${QUIRQ_STATE_ROOT:-$HOME/.quirq}/flies"
cp "$HOME/Downloads/failure-scout.fly.json" \
  "${QUIRQ_STATE_ROOT:-$HOME/.quirq}/flies/failure-scout.fly.json"

# Persist AGENT_NAME=fly in your installation's .env, or select for this run:
AGENT_NAME=fly ./install.sh
```

For an already provisioned development checkout, use
`AGENT_NAME=fly venv/bin/python server.py` instead. Keep the same state and project
roots when restarting. Stop the previous server before starting a replacement.

Only top-level `flies/*.fly.json` files are deployments. GET `/api/fly/catalog`
or POST `/api/fly/reload` validates them and returns accepted policies plus
per-file errors. GET `/api/models` exposes a model ID `fly/<artifact.fly.id>`.
GET `/api/agents` lists existing projects; a project's ID goes in `agent_id`,
separately from the model ID.

With one valid deployed fly, select a project in Space's chat and send `/run`.
The current browser chat does not send an explicit model selection: when
deploying multiple flies, use the chat API below with the desired `model`.

## Chat API

Use your configured server address; the examples use the default local port.
Replace `failure-demo` with an existing folder directly under `XO_PROJECTS_ROOT`
and `fly/<id-from-catalog>` with an accepted model ID.

```bash
curl -s http://127.0.0.1:5002/api/fly/catalog

curl -s http://127.0.0.1:5002/api/chat/prompt \
  -H 'Content-Type: application/json' \
  -d '{"agent_name":"fly","agent_id":"failure-demo","model":"fly/<id-from-catalog>","text":"/run"}'

# The response contains stream_id and session_id. Attach to start execution:
curl -N 'http://127.0.0.1:5002/api/chat/stream?stream_id=<stream_id>'
```

The standard SSE events are `session-created`, `model-loading`, `text-delta`,
`agent-error`, and one `done`. The response includes a link to
`GET /api/fly/runs/<run_id>` for the complete structured report, decision trace,
and provenance. A chat table previews at most 50 records and marks shortened
cell values; the persisted JSON holds the full result within the runtime's size
limit. `GET /api/messages?session_id=<session_id>` returns the transcript.

Resume by sending `{"session_id":"<session_id>","text":"/run"}` to the prompt
endpoint. Each new turn inspects the current project files with the same pinned
checkpoint; conversation history does not update the policy. Use `/help` for
command instructions without reading project files.

Only these task overrides are accepted:

```text
/run {"query":"workspace job failures","fields":["job","status","error_code"],"maxReads":4}
```

`fields` supports safe column names, dotted JSON paths, or JSON pointers. A
record is included only when every requested field is present. `query` is at
most 240 characters, `fields` contains 1–12 unique names, and `maxReads` is 1–64.
Overrides cannot supply a path, executable, tool name, or extra capability.
General prose is rejected with the supported command format.

Cancel with the existing endpoint:

```bash
curl -s http://127.0.0.1:5002/api/chat/abort \
  -H 'Content-Type: application/json' -d '{"stream_id":"<stream_id>"}'
```

Concurrent turns in one session are rejected. Duplicate SSE connections do not
execute twice. Disconnecting the executing stream cancels its turn; a completed
turn is available from the transcript rather than replayed on reconnect. A
prompt with no stream consumer expires after five minutes. Cancellation stops
between bounded operations; an already executing Node call is allowed to exit
or hit its 15-second timeout before cleanup.

## Try the synthetic failure demo

The PR includes the actual lab export and its synthetic held-out workspace in
`tests/fixtures/fly/`. No private workspace data or real credentials are in these
fixtures. From the checkout, with the same root environment as your server:

```bash
venv/bin/python - <<'PY'
import json, os
from pathlib import Path

fixtures = Path('tests/fixtures/fly')
projects = Path(os.environ.get('XO_PROJECTS_ROOT', '~/xo-projects')).expanduser()
state = Path(os.environ.get('QUIRQ_STATE_ROOT', '~/.quirq')).expanduser()
project = projects / 'failure-demo'
project.mkdir(parents=True, exist_ok=False)  # refuses to replace an existing project
workspace = json.loads((fixtures / 'workspace.json').read_text())
for record in workspace['files']:
    target = project / record['path']
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(record['content'], encoding='utf-8')
flies = state / 'flies'
flies.mkdir(parents=True, exist_ok=True)
with (flies / 'failure-scout.fly.json').open('xb') as target:
    target.write((fixtures / 'failure-scout.fly.json').read_bytes())
artifact = json.loads((fixtures / 'failure-scout.fly.json').read_text())
print('Project:', project)
print('Model:', 'fly/' + artifact['fly']['id'])
PY
```

Reload the catalog, select `failure-demo`, and send `/run`. The learned policy
reads four of twelve files and extracts three failure records. The report stays
**partial** because eight eligible files remain unread. Inspect the citations:
two jobs record `AUTH_EXPIRED`; the daily report records `TOKEN_REFRESH_FAILED`.
These observations can guide a human investigation of expired credentials and
token refresh. They do not prove the underlying cause. Healthy and irrelevant
records elsewhere in the fixture test whether the policy prioritizes useful
files within its budget.

The parity test checks every decision feature, score, probability, chosen path,
value, and citation against the training app's golden result.

## Architecture and state

The adapter uses the existing discovery and capability system. All executable
Fly-specific code is under `services/cowork_agent/adapters/fly/`, and its manifest
is under `config/agents/fly/`. The shared chat router is unchanged.

1. `artifacts.py` validates the export with the installed JavaScript core,
   including schema, policy family, feature/runtime versions, and canonical
   SHA-256 integrity. It pins a content-addressed checkpoint before selection.
2. `adapter.py` binds a session to one existing project and one checkpoint.
   `/run` resolves its saved task or explicit overrides.
3. `tools.py` scans file names and stat metadata. `runtime.py` gives that metadata
   to the installed core, opens only the selected file, then supplies the
   observation for the next decision. Unopened file contents are never features.
4. `node/core.mjs` is byte-for-byte the training app's core. `provenance.json`
   pins its SHA-256, verified by the bridge before every call. There is no second
   Python implementation of inference and no evaluation oracle in host runs.
5. `sessions.py` persists the run, report, trace, citations, transcript, and
   provenance before final chat output. `chat.py` translates events and follows
   the existing abort lifecycle. A nonblocking file lock prevents two executing
   turns from changing the same session.

```text
<QUIRQ_STATE_ROOT>/
  flies/*.fly.json                  deployed exports
  flies/versions/<digest>.json      immutable checkpoints for pinned sessions
  fly-runtime/catalog.json         accepted deployments
  fly-runtime/sessions/<uuid>.json  session bindings, transcript, run index
  fly-runtime/runs/<uuid>.json      full reports, traces, provenance
  fly-runtime/locks/<uuid>.lock     executing-turn locks
  projects/<pid>/sessions/          normal Space session index entries
```

Changing a deployed policy requires a higher `fly.revision`. A malformed
replacement keeps the last accepted checkpoint with a visible catalog error;
duplicate identities are quarantined. Removing a deployment removes it from
new-session discovery while existing sessions can still load their snapshots.
Resuming never silently changes the fly, project, or revision. Start a new
session to choose a newer checkpoint. An interrupted run is reconciled without
automatically reading files again. State is machine-local; the adapter writes
neither transcripts nor reports into project `.xo/` folders.

The policy is frozen during execution. Training belongs in the lab: train,
evaluate, export a new revision, copy it into `flies/`, and reload.

## Boundaries and limits

- Only visible, single-link regular UTF-8 `.json` and `.csv` files in the selected
  project are eligible. Hidden paths (including `.git`, `.xo`, `.quirq`, `.env`),
  symlinks, hard-linked files, dependency folders, and secret-named paths are
  excluded. Descriptor-relative opens and metadata checks reject changed files.
- Discovery accepts at most 64 eligible files, 256,000 bytes per file, and
  4,000,000 bytes total. Scans stop with an explicit error beyond 4,096 entries,
  16 directories deep, or three seconds. Choose a smaller dedicated data project
  if these bounds are exceeded; discovery never silently truncates results.
- The host checks a 90-second run budget between operations; the adapter applies
  a 120-second deadline with bounded cleanup. There are at most two concurrent
  Node calls per server process. Requests/results through the bridge are capped
  at 24 MB, and each session accepts at most 100 turns, including `/help`.
- Read/parse failures and unread eligible files keep a report partial. Zero
  eligible files means only that the bounded eligible set is empty; it does not
  mean the project is healthy. The report exposes excluded/discovered counts.
- Artifact capabilities are a compatibility declaration, not filesystem grants.
  Host code supplies the project scope and read-only skills. No arbitrary shell,
  file edits, connector calls, network fetching, or autonomous repairs are exposed.
- Space's API access controls remain the deployment boundary. This does not add
  tenant isolation or authentication to its existing chat API. Keep the normal
  loopback default or provide your deployment's authentication boundary. Names
  are only an exclusion heuristic: ordinary eligible files can contain sensitive
  values, which become part of the local report/transcript. Bridge result data
  uses private temporary files so record values do not enter the shared command
  journal. Stored reports remain available until you remove the local state.

## Verify changes

```bash
venv/bin/python -m unittest tests.test_fly_artifacts tests.test_fly_runtime tests.test_fly_chat -q
venv/bin/python scripts/check_route_parity.py
```

Tests use temporary state/project roots and real Node inference; they do not
touch deployed flies or contact an LLM. Update the vendored core, provenance,
and parity fixtures together when deliberately changing the export contract.
