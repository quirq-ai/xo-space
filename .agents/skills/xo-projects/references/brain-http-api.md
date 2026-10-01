# Knowledge brain HTTP API

The Space's knowledge brain has learned the projects the person added on **Projects → Brain**: every concept is stored once, with a note per project on how that project uses it, typed links between concepts, and the file and lines each one came from. Any agent can use it over HTTP; nothing here depends on which agent you are.

Base URL: `http://${HOST:-localhost}:${PORT:-5002}/api/brain`

## When to use it

- Before you search a large codebase by hand, or when the person asks about another project ("how does the other project do retries?", "tell me about forge ai"): **recall** first, then open the files it points at.
- Before you build something, check whether another project already solved it: recall the goal and look at the patterns and analogies.
- After you used several recalled pieces together for real work, say so with **use**: the brain links them more strongly.
- When something you built worked or failed, record an **experience**: future rankings learn from it.

## Recall

```
POST /api/brain/recall
{"cue": "how are failed payments retried", "context_source": null, "limit": 10, "explore": false, "answer": false}
```

- `cue` (required): the question or need, 1 to 2000 chars.
- `context_source`: a source id from `GET /api/brain/sources` to favour one project. Leave it out to let the brain pick the project the cue names (`context_detected` in the reply says it did).
- `explore`: also walk predicted links. What they reach comes back under `hypotheses`, never under `results`: treat it as a guess.
- `answer`: leave `false`. You are a model already; read the evidence yourself. `true` makes the brain's own model write an answer, which costs a model call (with `BRAIN_MODEL=agent`, a whole agent turn).

The reply:

```
{"cue": "...", "context_source": "<pid>|null", "context_name": "shop", "context_detected": true, "gap": false,
 "results": [{"piece": {"id": 42, "name": "exponential backoff", "description": "..."},
              "score": 0.81, "explanation": "cue matched ... → ... ",
              "note": {"source_id": "...", "note": "how this project uses it"},
              "notes": [{"source": "shop", "note": "..."}, {"source": "mail", "note": "..."}],
              "evidence": [{"source": "shop", "path": "README.md", "start_line": 5, "end_line": 8, "quote": "..."}]}],
 "hypotheses": [], "answer": null}
```

`evidence` is the trail: project folder name, path and line range. Open those lines before you rely on a result, and cite them to the person. `gap: true` means nothing known matched; the cue is recorded as missing knowledge.

## Other calls you may need

| Call | Use |
|---|---|
| `GET /api/brain/status` | counts, the connected model, whether learning is running |
| `GET /api/brain/sources` | learned projects (`sources`) and every project that could be (`projects`) |
| `GET /api/brain/pieces/{id}` | one piece: notes per project, evidence, links, guesses apart |
| `GET /api/brain/patterns`, `GET /api/brain/analogies` | groups of pieces that recur across projects, and what one project could borrow from another |
| `POST /api/brain/use` `{"piece_ids": [42, 57]}` | the pieces were used together (2 to 50 ids) |
| `POST /api/brain/experiences` `{"goal": "...", "result": "worked", "lessons": "...", "piece_ids": [42]}` | record an outcome: `worked`, `partial` or `failed` |
| `POST /api/brain/learn` `{"source_id": "<pid>"}` | learn a project again after you changed it (same machine only) |

Bodies are strict: an unknown key is a 422. Errors come back as `{"detail": {"code", "message"}}`; `local_only` (403) means the call has to come from this machine.

## Rules

- A `hypotheses` entry, or anything reached through one, is a guess. Never present it as a fact.
- The brain only knows what was learned. When it says `gap: true`, or the evidence does not say something, look in the files or ask the person instead of filling in.
- Do not read or write `~/.quirq/brain/brain.db` directly; use these calls.
