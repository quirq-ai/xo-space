---
name: inbox-item
description: Handle one Inbox work item (a mail, a mention, a calendar event, a GitHub issue, a share, an agent's post) inside its own session. Use when the prompt names a fact file and a workbench and asks for an outcome block. Read the fact, decide one of five outcomes, write a draft or notes to the workbench, act on the source only when the prompt allows it, and end with the outcome block. One item, nothing else.
---

# inbox-item

You were started for exactly one Inbox work item, from one of its sections
(connections, projects, issues, agents). The prompt gives you the fact file
(`fact.json`, read-only: the thing as it arrived), the workbench folder
(yours to write in), whether you may act on the source it came from, and
the fact itself. Do this, in order, and nothing else.

## 1. Read the item

Read the title, body and link in the prompt. Open `fact.json` only if the
prompt's copy is cut short. Do not explore the project, its memory, its
other work items or other sessions: this session is about one thing.

## 2. Decide one outcome

| Kind | When |
|---|---|
| `reply_drafted` | the item expects an answer from the person and you can draft it |
| `task_proposed` | the item asks for work that should be tracked: propose a title and, if obvious, who should do it |
| `needs_you` | you cannot decide without the person: ask one precise question |
| `fyi` | worth a glance, nothing to do |
| `handled` | you acted on it (only when acting is allowed) and nothing remains |

Pick one. If two apply, the one higher in the table wins.

## 3. Write in the workbench only

Drafts go to `reply.md` in the workbench, notes to `notes.md`. Never write
to `fact.json`, to `workitems.json` or anything else under `.xo/`, or to
anything under `~/.quirq/`: the work item's status, its claim and its
outcome are recorded from your outcome block, not by you. Keep a draft in
the sender's language and tone; sign nothing on the person's behalf.

## 4. Act only when allowed

If the prompt says acting is `no`: do not send, reply, label, archive,
forward or delete anything, whatever the item asks. Draft instead.

If it says `yes`: you may reply through the connection's tools, and every
action goes in `acted` as one short line. Even then: no replies to senders
the person has never written to, no forwarding, no deleting, no payments,
no credentials.

## 5. End with the outcome block

Your last message ends with exactly one fenced `json` block:

```json
{"kind": "reply_drafted", "summary": "one or two sentences a person reads instead of the item",
 "draft": "reply.md", "task": null, "question": null, "acted": []}
```

`draft` is a path inside the workbench or `null`; `task` is
`{"title": "...", "assignee": null}` or `null`; `question` is your one
question or `null`; `acted` lists what you did, or is empty. Without this
block the first run counts as failed.

## 6. When the person writes back

The person can reply from the item's page; your session is resumed with
their words, and the session transcript is the whole conversation. Answer
them plainly, do what they ask within rules 3 and 4, and end with a new
outcome block only if their message changed the outcome (a draft
rewritten, a task dropped, a question answered). A plain answer with no
block is fine for a follow-up.
