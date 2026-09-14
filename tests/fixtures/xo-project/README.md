# Sample xo-project: the canonical `.xo/`

This folder is the golden sample of a project's portable metadata: exactly what
`.xo/` holds right after XO Space first meets a folder, however the folder got
into the projects root. `tests/test_xo_structure.py` compares every creation
path against it, and the code that creates it is `services/xo_structure.py`.

```
sample-project/
└── .xo/
    ├── .gitignore       "*": .xo/ stays out of the project's own git history
    ├── project.json     identity and description
    ├── todos.json       session-scoped todos
    ├── workitems.json   durable work items, optionally adopted from GitHub issues
    └── peers.json       who the project is shared with
```

## What each file is

| File | Created by | Written afterwards by | Schema |
|---|---|---|---|
| `.gitignore` | `services/xo_structure.py` | nobody | none |
| `project.json` | the identity sink (`visualizer/sinks/project_json.fill_identity`), then `project_layout.seed_project_metadata` | the identity sink (`schema`, `pid`, `name`, `owner_user_id`, `created_at`); `project_layout._upsert_metadata` (`display_name`, `description`); the git refresher (`git`) | `project.schema.json` |
| `todos.json` | `services/xo_structure.py`, empty | the todos API only (`visualizer/todos_store.py`) | `todos.schema.json` |
| `workitems.json` | `services/xo_structure.py`, empty | the workitems API only (`visualizer/workitems_store.py`) | `workitems.schema.json` |
| `peers.json` | `services/xo_structure.py`, empty | the peers API only (`visualizer/peers_store.py`) | `peers.schema.json` |

Schemas live in `services/cowork_agent/visualizer/schema/`. Every store document
is stamped with `$schema` and `schema` from birth, so a reader can dispatch on
the version without guessing.

`agent.json` is the one optional member. An agent adapter writes it when the
folder is attached to an agent backend; its presence is the signal, and XO Space
never creates it on its own.

## When a project gets this structure

- **Scaffolded**: `project_layout.scaffold_project`, used by
  `POST /api/files/mkdir` with `scaffold: true` and by agent creation. The
  template adds the work tier (`AGENTS.md`, `PROJECT.md`, `memory/`, …) beside it.
- **Cloned through the API**: `POST /api/xo-projects`.
- **Auto-cloned by project sharing**, when a repository shared with this Space
  is cloned into the root.
- **Cloned or copied by hand** into the projects root: the watcher adds `.xo/`
  on its next tick.

## The rules

- **Additive only.** Missing files are created; an existing file is never
  rewritten, repaired or reformatted, including one that does not parse.
- **Never blocks.** A read-only folder or a symlinked `.xo` is reported and
  skipped; nothing raises into the caller.
- **Only `.xo/`.** A cloned repository gains `.xo/` and nothing else.
- **A former projects root is not a project.** A `.xo/` that holds `space.json`
  or `projects.json` is left alone.

## What differs between projects

`pid`, `owner_user_id` and `created_at` are minted per project; this sample uses
placeholders, and the test checks their shape before comparing. `name` and
`display_name` are the folder name unless a person typed a display name at
creation. `description` is empty unless one was typed. A `git` block
(`remote_url`, `default_branch`) is added to `project.json` once the git
refresher has recorded the project's provenance.

## What is not here

Machine-local, re-derivable state (stats, the timeline, the session index, the
GitHub issue mirror, work-item claims) lives outside the project under
`~/.quirq/projects/<pid>/`, keyed by the `pid` in `project.json`.

## Changing the structure

1. Change `services/xo_structure.py`, and the owning store and schema if a
   document's shape changes.
2. Change this sample to match. `tests/test_xo_structure.py` fails until the
   code, the sample and the schemas agree.
3. Existing projects gain a new file automatically on their next check. An
   existing file is never rewritten, so changing the shape of a document that
   already exists needs a migration in its store.

## Tracking this sample in git

The repository's `.gitignore` ignores `.xo/` directories, and this sample's own
`.xo/.gitignore` ignores its contents, which is the point of that file. Add the
sample with `git add -f tests/fixtures/xo-project/.xo`. Once tracked, edits show
up in `git status` as usual; only a newly added file needs `-f` again.
