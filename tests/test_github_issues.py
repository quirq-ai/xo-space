"""The GitHub issue mirror: its schema (W1b) and its client (W4).

Two things are being defended here, and they fail differently.

**The schema has to reject.** ``docs/workitems-plan.md`` W1 says it outright —
"a schema that accepts everything is worse than one that is stale" — so every
accept case below is paired with rejections that name the specific mistake
they catch: an invented status, a rate object with half a budget in it, an
issue map keyed by something that is not a node id.

**The pinned query is a cost contract.** §6.2 measured ``first:100`` with
``assignees(first:5)`` at **1 GraphQL point**, and ``+ labels(first:10)`` at
**2** — which halves the ceiling from ~83 continuously-polled repos to ~41.
That ceiling is invisible until it is breached, so :class:`CostContractTests`
makes it visible at review time: it enumerates the connections in the query
and fails if a third appears. It runs **offline**, because a guard that only
works when the network does is not a guard. :class:`LiveGitHubTests` then
confirms the real number against GitHub when ``gh`` happens to be
authenticated, and *skips* otherwise — it is corroboration, never the gate.

The subprocess tests drive a **real** ``/bin/sh`` stand-in for ``gh`` rather
than patching the runner. Everything interesting in the failure paths is in
the seam — exit codes, which stream carries the body, whether the token
reached the environment instead of the argument vector — and a mock of
``_run_gh`` would assert only that the classifier classifies.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:  # pragma: no cover - exercised by its absence, not by a branch
    from jsonschema import Draft7Validator
except ImportError:  # pragma: no cover
    Draft7Validator = None  # type: ignore[assignment]

from services.cowork_agent.connectors import github_issues as gh_issues
from services.cowork_agent.connectors.github_issues import (
    ERROR_KINDS,
    ISSUES_QUERY,
    MAX_QUERY_COST,
    IssuesResult,
    RateLimit,
    RepoRef,
    fetch_open_issues,
    fetch_open_issues_for_remote,
    parse_remote_url,
    parse_repo_slug,
    query_connections,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    ROOT / "services" / "cowork_agent" / "visualizer" / "schema"
    / "github-issues.schema.json"
)

_SKIP_JSONSCHEMA = (
    "jsonschema is not installed — "
    "uv pip install --python venv/bin/python -r requirements-dev.txt"
)

#: The connections the pinned query is *allowed* to contain, and the page
#: size each was measured at. Written here rather than imported from the
#: module on purpose: if this literal lived next to the query, one edit would
#: move both and the contract would enforce nothing. Changing the query now
#: requires changing this line too — which is the review moment the ~83-repo
#: ceiling needs.
EXPECTED_CONNECTIONS = (("assignees", "5"), ("issues", "$first"))

#: A response body in exactly the shape GitHub returns, captured from a live
#: call. Two issues, one assigned and one not, one with a state reason.
OK_BODY = {
    "data": {
        "rateLimit": {
            "limit": 5000, "cost": 1, "remaining": 4978,
            "resetAt": "2026-09-08T08:17:15Z",
        },
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": True, "endCursor": "Y3Vyc29yOnYyOpK0"},
                "nodes": [
                    {
                        "id": "I_kwDON4t-Ns8AAAABQGjnyA",
                        "number": 2047,
                        "title": "[BUG] Vulkan ErrorDeviceLost",
                        "state": "OPEN",
                        "stateReason": None,
                        "url": "https://github.com/cjpais/Handy/issues/2047",
                        "updatedAt": "2026-09-08T07:00:33Z",
                        "assignees": {"nodes": []},
                    },
                    {
                        "id": "I_kwDON4t-Ns8AAAABDLM18Q",
                        "number": 1428,
                        "title": "List Handy in the Windows Store?",
                        "state": "OPEN",
                        "stateReason": "REOPENED",
                        "url": "https://github.com/cjpais/Handy/issues/1428",
                        "updatedAt": "2026-08-01T00:40:14Z",
                        "assignees": {"nodes": [
                            {"login": "reverse-flash01",
                             "avatarUrl": "https://avatars.githubusercontent.com/u/1?v=4"},
                        ]},
                    },
                ],
            }
        },
    }
}

#: What gh actually printed for a repository that does not exist: HTTP 200,
#: a partial body, ``errors[0].type == "NOT_FOUND"`` — and a readable
#: ``rateLimit``, because the point was spent regardless.
NOT_FOUND_BODY = {
    "data": {
        "rateLimit": {
            "limit": 5000, "cost": 1, "remaining": 4974,
            "resetAt": "2026-09-08T07:10:55Z",
        },
        "repository": None,
    },
    "errors": [{
        "type": "NOT_FOUND",
        "path": ["repository"],
        "message": "Could not resolve to a Repository with the name 'a/b'.",
    }],
}

#: An auth failure never reaches GraphQL: gh prints the REST error envelope.
BAD_CREDENTIALS_BODY = {
    "message": "Bad credentials",
    "documentation_url": "https://docs.github.com/rest",
    "status": "401",
}


def _fake_gh(
    directory: Path,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    sleep: float = 0.0,
) -> str:
    """A shell script that impersonates ``gh``, byte-for-byte.

    Dumps its argument vector and the ``GH_TOKEN`` it was handed to the file
    named by ``XO_TEST_ARGV_DUMP`` when that variable is set, which is how
    the token-handling test observes that the secret travelled in the
    environment and never in ``argv`` (where ``ps`` would show it).
    """
    path = directory / "gh"
    script = (
        "#!/bin/sh\n"
        'if [ -n "$XO_TEST_ARGV_DUMP" ]; then\n'
        '  : > "$XO_TEST_ARGV_DUMP"\n'
        '  for a in "$@"; do printf \'%s\\0\' "$a" >> "$XO_TEST_ARGV_DUMP"; done\n'
        '  printf \'GH_TOKEN=%s\\0\' "${GH_TOKEN-}" >> "$XO_TEST_ARGV_DUMP"\n'
        "fi\n"
        f"sleep {sleep}\n"
        "cat <<'XO_OUT_EOF'\n" + stdout + "\nXO_OUT_EOF\n"
        "cat >&2 <<'XO_ERR_EOF'\n" + stderr + "\nXO_ERR_EOF\n"
        f"exit {exit_code}\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


# ── the schema ────────────────────────────────────────────────────────────────


@unittest.skipIf(Draft7Validator is None, _SKIP_JSONSCHEMA)
class MirrorSchemaTests(unittest.TestCase):
    """``github-issues.schema.json`` — what it takes and what it refuses."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.validator = Draft7Validator(cls.schema)

    def document(self, **overrides) -> dict:
        doc = {
            "$schema": "xo/github-issues.schema.json",
            "schema": 1,
            "repo": "cjpais/Handy",
            "fetched_at": "2026-09-08T07:00:00Z",
            "since": "2026-09-08T06:59:00Z",
            "rate": {"limit": 5000, "cost": 1, "remaining": 4978,
                     "reset_at": "2026-09-08T08:17:15Z"},
            "error": None,
            "issues": {
                "I_kwDON4t-Ns8AAAABQGjnyA": {
                    "node_id": "I_kwDON4t-Ns8AAAABQGjnyA",
                    "number": 2047,
                    "title": "[BUG] Vulkan ErrorDeviceLost",
                    "state": "open",
                    "state_reason": None,
                    "assignees": [{"login": "dwivedi-ai", "avatar_url": None}],
                    "url": "https://github.com/cjpais/Handy/issues/2047",
                    "updated_at": "2026-09-08T07:00:33Z",
                },
            },
        }
        doc.update(overrides)
        return doc

    def assertAccepted(self, doc: object, label: str) -> None:
        errors = sorted(self.validator.iter_errors(doc), key=lambda e: list(e.path))
        if errors:
            detail = "\n".join(
                f"  at /{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                for e in errors
            )
            self.fail(f"{label} must validate but does not:\n{detail}")

    def assertRejected(self, doc: object, label: str) -> None:
        if self.validator.is_valid(doc):
            self.fail(f"{label} was accepted, but the schema must reject it")

    # --- it is a schema at all -------------------------------------------

    def test_it_is_a_valid_draft7_schema(self) -> None:
        Draft7Validator.check_schema(self.schema)

    def test_it_follows_the_id_convention_the_writers_stamp(self) -> None:
        self.assertEqual(self.schema["$id"], "xo/github-issues.schema.json")
        self.assertEqual(
            self.schema["$schema"], "http://json-schema.org/draft-07/schema#"
        )

    # --- accepts ----------------------------------------------------------

    def test_a_full_document_validates(self) -> None:
        self.assertAccepted(self.document(), "the reference document")

    def test_the_plan_5_2_example_validates(self) -> None:
        """§5.2's literal example, including the fields the poller leaves null.

        The plan is the specification; a schema that its own worked example
        fails is a schema that was written against a different design.
        """
        self.assertAccepted(
            {
                "schema": 1,
                "repo": "dwivedi-ai/xo-cowork-api",
                "fetched_at": "2026-09-08T12:00:00Z",
                "since": "2026-09-08T11:59:00Z",
                "rate": {"remaining": 4783, "reset_at": "2026-09-08T13:00:00Z"},
                "error": None,
                "issues": {
                    "I_kwDOABCD1234": {
                        "node_id": "I_kwDOABCD1234",
                        "number": 42,
                        "title": "Rate-limit the GitHub poller",
                        "state": "open",
                        "state_reason": None,
                        "assignees": [{"login": "peer-login", "avatar_url": "..."}],
                        "labels": ["bug"],
                        "url": "https://github.com/dwivedi-ai/xo-cowork-api/issues/42",
                        "updated_at": "2026-09-08T11:00:00Z",
                    }
                },
            },
            "the §5.2 example document",
        )

    def test_an_empty_mirror_validates(self) -> None:
        """The first poll of a repo with no open issues, and the state a
        freshly-created mirror is in. Neither is an error."""
        self.assertAccepted(
            self.document(issues={}, rate=None, since=None), "an empty mirror"
        )

    def test_a_row_without_labels_validates(self) -> None:
        """Every row the poller writes is this shape: labels are not fetched
        (§6.2), and their absence must not be an error."""
        doc = self.document()
        row = next(iter(doc["issues"].values()))
        self.assertNotIn("labels", row)
        self.assertAccepted(doc, "a poller-written row")

    def test_a_structured_error_validates(self) -> None:
        self.assertAccepted(
            self.document(
                rate=None,
                error={"kind": "not_authenticated",
                       "message": "GitHub CLI is not authenticated.",
                       "at": "2026-09-08T07:00:00Z"},
            ),
            "a document carrying a failure",
        )

    def test_every_error_kind_the_client_can_emit_is_accepted(self) -> None:
        """The client's vocabulary and the schema's enum are one vocabulary.

        Checked as a set equality rather than a subset: a kind declared here
        and never emitted is dead UI, and a kind emitted and not declared
        fails validation at the worst possible moment — while something is
        already broken.
        """
        declared = self.schema["properties"]["error"]["oneOf"][1][
            "properties"]["kind"]["enum"]
        self.assertEqual(sorted(declared), sorted(ERROR_KINDS))
        for kind in ERROR_KINDS:
            with self.subTest(kind=kind):
                self.assertAccepted(
                    self.document(error={"kind": kind, "message": "x", "at": None}),
                    f"error kind {kind}",
                )

    def test_both_closed_state_reasons_validate(self) -> None:
        for reason in ("completed", "not_planned", "reopened", None):
            with self.subTest(reason=reason):
                doc = self.document()
                row = next(iter(doc["issues"].values()))
                row["state"] = "closed"
                row["state_reason"] = reason
                self.assertAccepted(doc, f"state_reason {reason}")

    # --- rejects ----------------------------------------------------------

    def test_a_missing_required_key_is_rejected(self) -> None:
        for key in ("schema", "repo", "fetched_at", "issues"):
            with self.subTest(missing=key):
                doc = self.document()
                doc.pop(key)
                self.assertRejected(doc, f"a document without {key}")

    def test_an_undeclared_top_level_key_is_rejected(self) -> None:
        """``additionalProperties: false`` is what caught the two schemas T16
        found being violated by their own writers."""
        self.assertRejected(
            self.document(issues_count=87), "a document with an undeclared key"
        )

    def test_a_future_schema_version_is_rejected(self) -> None:
        self.assertRejected(self.document(schema=2), "schema: 2")

    def test_a_repo_that_is_not_owner_slash_name_is_rejected(self) -> None:
        for repo in ("Handy", "cjpais/Handy/extra", "", "cjpais / Handy",
                     "https://github.com/cjpais/Handy"):
            with self.subTest(repo=repo):
                self.assertRejected(self.document(repo=repo), f"repo {repo!r}")

    def test_issues_as_a_list_is_rejected(self) -> None:
        """The mirror is keyed by node_id (§5.2). A list has no key, and the
        O-C collision class starts with a container that lost one."""
        doc = self.document()
        doc["issues"] = list(doc["issues"].values())
        self.assertRejected(doc, "issues as a list")

    def test_an_issue_key_that_is_not_a_node_id_is_rejected(self) -> None:
        for key in ("", "cjpais/Handy#42", "42 ", "a b"):
            with self.subTest(key=key):
                doc = self.document()
                doc["issues"] = {key: next(iter(doc["issues"].values()))}
                self.assertRejected(doc, f"issue key {key!r}")

    def test_an_invented_status_is_rejected(self) -> None:
        """§5.4/D7: ``open``/``closed`` and nothing else. ``in_progress`` is
        derived from a live claim and must never be storable here — a stored
        one is a flag that lies the moment the process holding it dies."""
        for state in ("in_progress", "blocked", "OPEN", "cancelled", None):
            with self.subTest(state=state):
                doc = self.document()
                next(iter(doc["issues"].values()))["state"] = state
                self.assertRejected(doc, f"state {state!r}")

    def test_an_invented_state_reason_is_rejected(self) -> None:
        doc = self.document()
        next(iter(doc["issues"].values()))["state_reason"] = "cancelled"
        self.assertRejected(doc, "state_reason 'cancelled'")

    def test_a_number_that_is_not_a_positive_integer_is_rejected(self) -> None:
        for number in ("42", 0, -1, 4.2, None):
            with self.subTest(number=number):
                doc = self.document()
                next(iter(doc["issues"].values()))["number"] = number
                self.assertRejected(doc, f"number {number!r}")

    def test_an_undeclared_issue_field_is_rejected(self) -> None:
        doc = self.document()
        next(iter(doc["issues"].values()))["body"] = "..."
        self.assertRejected(doc, "an issue carrying a body")

    def test_an_issue_missing_a_required_field_is_rejected(self) -> None:
        for key in ("node_id", "number", "title", "state", "url", "updated_at"):
            with self.subTest(missing=key):
                doc = self.document()
                next(iter(doc["issues"].values())).pop(key)
                self.assertRejected(doc, f"an issue without {key}")

    def test_an_assignee_without_a_login_is_rejected(self) -> None:
        """The login IS the assignment (D1). An assignee row without one is
        an assignment to nobody wearing the shape of one."""
        for assignees in ([{"avatar_url": "x"}], [{"login": ""}], ["dwivedi-ai"],
                          {"login": "dwivedi-ai"}):
            with self.subTest(assignees=assignees):
                doc = self.document()
                next(iter(doc["issues"].values()))["assignees"] = assignees
                self.assertRejected(doc, f"assignees {assignees!r}")

    def test_labels_must_be_strings_when_present(self) -> None:
        doc = self.document()
        next(iter(doc["issues"].values()))["labels"] = [{"name": "bug"}]
        self.assertRejected(doc, "labels as objects")

    def test_a_half_known_rate_is_rejected(self) -> None:
        """A budget is observed or it is not. ``remaining`` without
        ``reset_at`` cannot drive a backoff, and ``remaining: null`` reads as
        a number to every consumer that does not check — so the honest
        encoding of 'unknown' is the whole object being null."""
        for rate in ({"remaining": 4783}, {"reset_at": "2026-09-08T08:00:00Z"},
                     {"remaining": None, "reset_at": None}, {}):
            with self.subTest(rate=rate):
                self.assertRejected(self.document(rate=rate), f"rate {rate!r}")

    def test_a_rate_with_an_undeclared_key_is_rejected(self) -> None:
        self.assertRejected(
            self.document(rate={"remaining": 1, "reset_at": "x", "used": 4999}),
            "rate carrying `used`",
        )

    def test_a_negative_remaining_is_rejected(self) -> None:
        self.assertRejected(
            self.document(rate={"remaining": -1, "reset_at": "x"}),
            "rate.remaining = -1",
        )

    def test_a_bare_string_error_is_rejected(self) -> None:
        """The UI has to tell 'connect GitHub' from 'wait, the network is
        down'. A string cannot be branched on without parsing prose."""
        self.assertRejected(self.document(error="Bad credentials"), "a string error")

    def test_an_unrecognised_error_kind_is_rejected(self) -> None:
        self.assertRejected(
            self.document(error={"kind": "sad", "message": "x"}), "kind 'sad'"
        )

    def test_an_error_without_a_message_is_rejected(self) -> None:
        self.assertRejected(
            self.document(error={"kind": "network"}), "an error with no message"
        )


# ── the cost contract ─────────────────────────────────────────────────────────


class CostContractTests(unittest.TestCase):
    """§6.2's measurement, turned into something that fails at review time.

    Every assertion here is offline. The live confirmation is
    :class:`LiveGitHubTests`, which skips rather than fails when there is no
    ``gh`` session — a cost guard that needs the network is a guard that is
    off exactly when someone is working on a plane.
    """

    def test_the_query_contains_exactly_the_measured_connections(self) -> None:
        """The whole guard, in one assertion.

        Connections are the only construct that adds to a query's point
        cost. ``issues(first:100)`` + ``assignees(first:5)`` measured at 1
        point; adding ``labels(first:10)`` measured at 2, which halves the
        ceiling from ~83 continuously-polled repos to ~41 (§6.2, D6). If
        this fails you have changed the cost of every poll on every
        installation — go and re-measure with ``rateLimit { cost }`` before
        updating the literal.
        """
        self.assertEqual(
            query_connections(ISSUES_QUERY),
            EXPECTED_CONNECTIONS,
            "the pinned query's connections changed — the poll's cost, and "
            "with it the ~83-repo ceiling, is no longer what §6.2 measured",
        )

    def test_the_guard_notices_an_added_connection(self) -> None:
        """A positive control. Without it, a broken extractor that returns
        an empty tuple would make the test above pass forever."""
        drifted = ISSUES_QUERY.replace(
            "assignees(first: 5) { nodes { login avatarUrl } }",
            "assignees(first: 5) { nodes { login avatarUrl } }\n"
            "        labels(first: 10) { nodes { name } }",
        )
        self.assertNotEqual(drifted, ISSUES_QUERY)  # the replace actually fired
        found = query_connections(drifted)
        self.assertIn(("labels", "10"), found)
        self.assertNotEqual(found, EXPECTED_CONNECTIONS)

    def test_the_guard_ignores_reformatting(self) -> None:
        """Whitespace and field order are free; connections are not. The
        contract is about cost, and it should not fire on a tidy-up."""
        reflowed = " ".join(ISSUES_QUERY.split())
        self.assertNotEqual(reflowed, ISSUES_QUERY)
        self.assertEqual(query_connections(reflowed), EXPECTED_CONNECTIONS)

    def test_the_variable_declaration_is_not_mistaken_for_a_connection(self) -> None:
        """``$first: Int!`` declares a variable; ``issues(first: $first)``
        opens a connection. Counting the first as the second would make the
        expected tuple wrong from the start."""
        self.assertIn("$first: Int!", ISSUES_QUERY)
        self.assertNotIn("query", [name for name, _ in query_connections()])

    def test_labels_are_not_in_the_poll_query(self) -> None:
        """Stated separately from the connection tuple because this is the
        specific edit D6 says must not happen."""
        self.assertNotIn("labels", ISSUES_QUERY)

    def test_the_query_reads_issues_and_never_pull_requests(self) -> None:
        """§6.1: PRs cannot appear because ``repository.issues`` and
        ``repository.pullRequests`` are separate connections. That is why
        there is no ``pull_request`` filter anywhere in the module — a
        filter would tell the next reader that PRs can arrive."""
        self.assertIn("issues(", " ".join(ISSUES_QUERY.split()).replace(" (", "("))
        self.assertNotIn("pullRequest", ISSUES_QUERY)
        source = (
            ROOT / "services" / "cowork_agent" / "connectors" / "github_issues.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("pull_request\"", source)
        self.assertNotIn("'pull_request'", source)

    def test_the_budget_is_requested_inside_the_query(self) -> None:
        """``gh api rate_limit`` was measured reporting a full budget
        regardless of consumption on a real token (§6.2). The inline
        ``rateLimit`` field is the reliable source and costs no extra call."""
        flat = " ".join(ISSUES_QUERY.split())
        self.assertIn("rateLimit { limit cost remaining resetAt }", flat)

    def test_the_declared_ceiling_is_one_point(self) -> None:
        self.assertEqual(MAX_QUERY_COST, 1)

    def test_the_query_asks_for_open_issues_newest_first(self) -> None:
        flat = " ".join(ISSUES_QUERY.split())
        self.assertIn("states: [OPEN]", flat)
        self.assertIn("orderBy: { field: UPDATED_AT, direction: DESC }", flat)


# ── remote URL parsing ────────────────────────────────────────────────────────


class RemoteParsingTests(unittest.TestCase):
    """``project.json:git.remote_url`` in all three shapes it really takes.

    ``git_provenance.sanitize_remote_url`` hands each of these through: the
    scp-short form untouched, the ssh form still carrying ``git@`` because
    stripping it yields a URL that is not clone-able, and the https form with
    any credential already removed.
    """

    def test_https_remote(self) -> None:
        ref = parse_remote_url("https://github.com/quirq-ai/xo-space.git")
        self.assertEqual(
            (ref.host, ref.owner, ref.name), ("github.com", "quirq-ai", "xo-space")
        )
        self.assertEqual(ref.slug, "quirq-ai/xo-space")
        self.assertTrue(ref.is_github_com)

    def test_ssh_url_remote_keeps_working_despite_the_git_username(self) -> None:
        self.assertEqual(
            parse_repo_slug("ssh://git@github.com/quirq-ai/xo-space.git"),
            "quirq-ai/xo-space",
        )

    def test_scp_short_remote(self) -> None:
        """The default for an SSH clone, and the shape with no ``//`` — a
        naive urlsplit reads the whole string as a path and yields nothing,
        which would silently disable polling for every SSH-cloned project."""
        self.assertEqual(
            parse_repo_slug("git@github.com:quirq-ai/xo-space.git"),
            "quirq-ai/xo-space",
        )

    def test_a_credentialed_https_remote_still_parses(self) -> None:
        """Upstream strips these, but this module does not depend on the
        order of two modules to avoid returning None for a real repo."""
        self.assertEqual(
            parse_repo_slug("https://user:ghp_secret@github.com/o/r.git"), "o/r"
        )

    def test_trailing_slash_and_missing_dot_git(self) -> None:
        for url in ("https://github.com/o/r", "https://github.com/o/r/",
                    "git://github.com/o/r.git"):
            with self.subTest(url=url):
                self.assertEqual(parse_repo_slug(url), "o/r")

    def test_an_enterprise_host_parses_but_is_flagged(self) -> None:
        ref = parse_remote_url("https://github.example.com/o/r.git")
        self.assertEqual(ref.slug, "o/r")
        self.assertFalse(ref.is_github_com)

    def test_unparseable_remotes_return_none(self) -> None:
        for url in (None, "", "   ", "/home/coder/repos/thing", "not a url",
                    "https://github.com/o", "https://github.com/o/r/sub",
                    "https://github.com/", "https://github.com/o/r r"):
            with self.subTest(url=url):
                self.assertIsNone(parse_remote_url(url))

    def test_a_bare_slug_is_accepted_where_a_url_is(self) -> None:
        """Callers hold both spellings: the poller has project.json's remote
        URL, a route or a fixture has the slug it read out of the mirror."""
        self.assertEqual(gh_issues._coerce_ref("cjpais/Handy").slug, "cjpais/Handy")

    def test_a_slug_shaped_string_with_an_at_sign_is_not_read_as_a_slug(self) -> None:
        """``git@host:o/r`` must reach the scp grammar, not the slug one."""
        self.assertEqual(
            gh_issues._coerce_ref("git@github.com:o/r.git").slug, "o/r"
        )


# ── degradation ───────────────────────────────────────────────────────────────


class DegradationTests(unittest.IsolatedAsyncioTestCase):
    """Every failure the poller will actually meet, as a reportable state.

    ``git_provenance`` is the house reference: a subprocess wrapper that
    never raises and never logs. The bar here is higher than "does not
    crash" — each of these has a different remedy, so each must arrive as a
    different ``error_kind``, and none of them may lose the rate-limit
    reading, because a failed poll still spent a point.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # No inherited GH_TOKEN, and no reading of the real mcp-tokens.json.
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
        self._token = patch.object(gh_issues, "get_github_token", return_value=None)
        self._token.start()
        self.addCleanup(self._token.stop)

    def gh(self, **kwargs) -> str:
        return _fake_gh(self.tmp, **kwargs)

    async def test_a_healthy_response_becomes_mirror_rows(self) -> None:
        result = await fetch_open_issues(
            "cjpais/Handy", gh_bin=self.gh(stdout=json.dumps(OK_BODY))
        )
        self.assertTrue(result.ok)
        self.assertIsNone(result.error_kind)
        self.assertEqual(result.repo, "cjpais/Handy")
        self.assertEqual([i["number"] for i in result.issues], [2047, 1428])
        self.assertEqual(result.issues[0]["state"], "open")
        self.assertEqual(result.issues[1]["state_reason"], "reopened")
        self.assertEqual(
            result.issues[1]["assignees"],
            [{"login": "reverse-flash01",
              "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4"}],
        )
        self.assertTrue(result.has_next_page)
        self.assertEqual(result.end_cursor, "Y3Vyc29yOnYyOpK0")
        self.assertEqual(result.rate.cost, 1)
        self.assertEqual(result.rate.remaining, 4978)

    async def test_rows_carry_no_labels_key(self) -> None:
        """Absence, not ``[]``. An empty array is the claim 'this issue has
        no labels', which a query that never asked cannot establish."""
        result = await fetch_open_issues(
            "cjpais/Handy", gh_bin=self.gh(stdout=json.dumps(OK_BODY))
        )
        for row in result.issues:
            self.assertNotIn("labels", row)

    async def test_gh_missing_is_no_cli_not_an_exception(self) -> None:
        result = await fetch_open_issues(
            "cjpais/Handy", gh_bin=str(self.tmp / "definitely-not-installed")
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "no_cli")
        self.assertIn("cli.github.com", result.error)

    async def test_not_authenticated_via_exit_code_4(self) -> None:
        """gh's documented status for 'authentication required' — what a
        machine with no session and no token in the environment returns."""
        result = await fetch_open_issues("cjpais/Handy", gh_bin=self.gh(
            stderr="To get started with GitHub CLI, please run:  gh auth login",
            exit_code=4,
        ))
        self.assertEqual(result.error_kind, "not_authenticated")

    async def test_not_authenticated_via_the_rest_error_envelope(self) -> None:
        """A revoked token never reaches GraphQL: gh prints the REST error
        body on **stdout** and exits 1. Measured, not assumed."""
        result = await fetch_open_issues("cjpais/Handy", gh_bin=self.gh(
            stdout=json.dumps(BAD_CREDENTIALS_BODY),
            stderr="gh: Bad credentials (HTTP 401)",
            exit_code=1,
        ))
        self.assertEqual(result.error_kind, "not_authenticated")
        self.assertIn("Bad credentials", result.error)

    async def test_a_deleted_repo_is_not_found_and_keeps_the_rate_reading(self) -> None:
        """GraphQL answers 200 with a partial body, so the point was spent
        and ``rateLimit`` is still readable. The poller's global budget has
        to account for a failed poll or it will overrun."""
        result = await fetch_open_issues("cjpais/Handy", gh_bin=self.gh(
            stdout=json.dumps(NOT_FOUND_BODY),
            stderr="gh: Could not resolve to a Repository with the name 'a/b'.",
            exit_code=1,
        ))
        self.assertEqual(result.error_kind, "not_found")
        self.assertEqual(result.rate.remaining, 4974)
        self.assertEqual(result.rate.cost, 1)
        self.assertEqual(result.error_document()["kind"], "not_found")

    async def test_a_null_repository_without_errors_is_not_found(self) -> None:
        body = {"data": {"rateLimit": None, "repository": None}}
        result = await fetch_open_issues(
            "cjpais/Handy", gh_bin=self.gh(stdout=json.dumps(body))
        )
        self.assertEqual(result.error_kind, "not_found")

    async def test_primary_rate_limit(self) -> None:
        body = {"data": {"rateLimit": None},
                "errors": [{"type": "RATE_LIMITED",
                            "message": "API rate limit exceeded"}]}
        result = await fetch_open_issues(
            "cjpais/Handy", gh_bin=self.gh(stdout=json.dumps(body), exit_code=1)
        )
        self.assertEqual(result.error_kind, "rate_limited")

    async def test_secondary_rate_limit(self) -> None:
        """Different mechanism, same remedy: back off and honour resetAt."""
        result = await fetch_open_issues("cjpais/Handy", gh_bin=self.gh(
            stdout=json.dumps({
                "message": "You have exceeded a secondary rate limit",
                "status": "403",
            }),
            stderr="gh: You have exceeded a secondary rate limit (HTTP 403)",
            exit_code=1,
        ))
        self.assertEqual(result.error_kind, "rate_limited")

    async def test_forbidden_is_distinct_from_unauthenticated(self) -> None:
        """403 on a repo means this token cannot see *that* repo. Telling the
        user to log in again would be the wrong instruction."""
        result = await fetch_open_issues("cjpais/Handy", gh_bin=self.gh(
            stdout=json.dumps({"message": "Resource not accessible",
                               "status": "403"}),
            exit_code=1,
        ))
        self.assertEqual(result.error_kind, "forbidden")

    async def test_network_down(self) -> None:
        result = await fetch_open_issues("cjpais/Handy", gh_bin=self.gh(
            stderr='error connecting to api.github.com: dial tcp: lookup '
                   'api.github.com: no such host',
            exit_code=1,
        ))
        self.assertEqual(result.error_kind, "network")

    async def test_a_server_error_is_reported_as_network(self) -> None:
        result = await fetch_open_issues("cjpais/Handy", gh_bin=self.gh(
            stdout=json.dumps({"message": "Server Error", "status": "502"}),
            exit_code=1,
        ))
        self.assertEqual(result.error_kind, "network")

    async def test_a_hung_gh_is_killed_and_reported_as_timeout(self) -> None:
        """A poll must not outlive its interval. The subprocess is really
        spawned and really killed here — the timeout path has a reap in it
        that a mocked runner would not exercise."""
        result = await fetch_open_issues(
            "cjpais/Handy", gh_bin=self.gh(sleep=30), timeout_s=0.3
        )
        self.assertEqual(result.error_kind, "timeout")
        self.assertIn("0.3", result.error)

    async def test_a_non_json_body_is_bad_response(self) -> None:
        result = await fetch_open_issues(
            "cjpais/Handy", gh_bin=self.gh(stdout="<html>proxy login</html>")
        )
        self.assertEqual(result.error_kind, "bad_response")

    async def test_a_repository_without_an_issues_connection_is_bad_response(self) -> None:
        body = {"data": {"rateLimit": None, "repository": {}}}
        result = await fetch_open_issues(
            "cjpais/Handy", gh_bin=self.gh(stdout=json.dumps(body))
        )
        self.assertEqual(result.error_kind, "bad_response")

    async def test_an_unclassifiable_failure_is_unknown_not_a_crash(self) -> None:
        result = await fetch_open_issues(
            "cjpais/Handy", gh_bin=self.gh(stderr="gh: something new", exit_code=1)
        )
        self.assertEqual(result.error_kind, "unknown")
        self.assertIn("something new", result.error)

    async def test_a_non_github_remote_never_spawns_gh(self) -> None:
        """The budget is global (§6.3), so a GitLab origin must not cost a
        point every minute to rediscover. Pointed at gitlab.com this query
        returns ``DateTime isn't a defined input type`` — a different schema
        entirely, which no retry fixes."""
        marker = self.tmp / "spawned"
        gh_bin = _fake_gh(self.tmp, stdout=json.dumps(OK_BODY))
        Path(gh_bin).write_text(
            f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8"
        )
        Path(gh_bin).chmod(0o755)
        result = await fetch_open_issues_for_remote(
            "https://gitlab.com/group/project.git", gh_bin=gh_bin
        )
        self.assertEqual(result.error_kind, "bad_remote")
        self.assertFalse(marker.exists())

    async def test_no_remote_at_all_is_bad_remote(self) -> None:
        for url in (None, "", "/home/coder/repos/thing"):
            with self.subTest(url=url):
                result = await fetch_open_issues_for_remote(url)
                self.assertEqual(result.error_kind, "bad_remote")

    async def test_every_error_kind_is_declared(self) -> None:
        """Whatever the paths above produced, it came from the vocabulary."""
        results = [
            await fetch_open_issues("cjpais/Handy",
                                    gh_bin=str(self.tmp / "missing")),
            await fetch_open_issues("cjpais/Handy",
                                    gh_bin=self.gh(stderr="x", exit_code=1)),
            await fetch_open_issues_for_remote(None),
        ]
        for result in results:
            self.assertIn(result.error_kind, ERROR_KINDS)

    # --- the argument vector ---------------------------------------------

    async def _argv(self, **kwargs) -> list[str]:
        dump = self.tmp / "argv"
        with patch.dict(os.environ, {"XO_TEST_ARGV_DUMP": str(dump)}):
            await fetch_open_issues(
                kwargs.pop("repo", "cjpais/Handy"),
                gh_bin=self.gh(stdout=json.dumps(OK_BODY)),
                **kwargs,
            )
        return [p for p in dump.read_bytes().decode().split("\0") if p]

    async def test_the_call_is_gh_api_graphql_with_the_pinned_query(self) -> None:
        argv = await self._argv()
        self.assertEqual(argv[:3], ["api", "graphql", "-f"])
        self.assertIn(f"query={ISSUES_QUERY}", argv)
        self.assertIn("owner=cjpais", argv)
        self.assertIn("name=Handy", argv)
        self.assertIn("first=100", argv)

    async def test_since_is_omitted_rather_than_passed_empty(self) -> None:
        """A null GraphQL variable disables ``filterBy``; an empty string is
        a malformed DateTime and fails the whole query."""
        self.assertFalse(any(a.startswith("since=") for a in await self._argv()))
        self.assertIn("since=2026-09-08T00:00:00Z",
                      await self._argv(since="2026-09-08T00:00:00Z"))

    async def test_after_is_passed_only_when_paging(self) -> None:
        self.assertFalse(any(a.startswith("after=") for a in await self._argv()))
        self.assertIn("after=CURSOR", await self._argv(after="CURSOR"))

    async def test_the_page_size_is_clamped_to_githubs_maximum(self) -> None:
        """GitHub rejects ``first > 100`` outright rather than clamping, so
        an over-large page would fail the poll instead of shrinking it."""
        self.assertIn("first=100", await self._argv(first=500))
        self.assertIn("first=1", await self._argv(first=0))
        self.assertIn("first=1", await self._argv(first=-7))

    async def test_an_enterprise_host_is_routed_with_hostname(self) -> None:
        argv = await self._argv(repo=RepoRef("github.corp.example", "o", "r"))
        self.assertIn("--hostname", argv)
        self.assertIn("github.corp.example", argv)

    async def test_github_com_is_not_given_a_hostname_flag(self) -> None:
        self.assertNotIn("--hostname", await self._argv())

    async def test_the_token_travels_in_the_environment_never_in_argv(self) -> None:
        """A token in the argument vector is a token in ``ps``. It is also
        never logged and never returned in an error message."""
        dump = self.tmp / "argv"
        self._token.stop()
        with patch.object(gh_issues, "get_github_token", return_value="ghp_SECRET"), \
             patch.dict(os.environ, {"XO_TEST_ARGV_DUMP": str(dump)}):
            await fetch_open_issues(
                "cjpais/Handy", gh_bin=self.gh(stdout=json.dumps(OK_BODY))
            )
        self._token.start()
        fields = [p for p in dump.read_bytes().decode().split("\0") if p]
        argv, env = fields[:-1], fields[-1]
        self.assertNotIn("ghp_SECRET", "\n".join(argv))
        self.assertEqual(env, "GH_TOKEN=ghp_SECRET")

    async def test_an_environment_token_wins_over_the_stored_one(self) -> None:
        """Overriding an operator's explicit ``GH_TOKEN`` would be the
        surprising direction."""
        dump = self.tmp / "argv"
        self._token.stop()
        with patch.object(gh_issues, "get_github_token", return_value="ghp_STORED"), \
             patch.dict(os.environ, {"XO_TEST_ARGV_DUMP": str(dump),
                                     "GH_TOKEN": "ghp_ENV"}):
            await fetch_open_issues(
                "cjpais/Handy", gh_bin=self.gh(stdout=json.dumps(OK_BODY))
            )
        self._token.start()
        fields = [p for p in dump.read_bytes().decode().split("\0") if p]
        self.assertEqual(fields[-1], "GH_TOKEN=ghp_ENV")

    async def test_a_broken_token_store_does_not_break_the_poll(self) -> None:
        self._token.stop()
        with patch.object(gh_issues, "get_github_token",
                          side_effect=OSError("mcp-tokens.json is a directory")):
            result = await fetch_open_issues(
                "cjpais/Handy", gh_bin=self.gh(stdout=json.dumps(OK_BODY))
            )
        self._token.start()
        self.assertTrue(result.ok)


# ── result shaping ────────────────────────────────────────────────────────────


class ResultShapeTests(unittest.TestCase):
    """The small conversions W6 will lean on, exercised without a subprocess."""

    def row(self, node_id: str, updated: str) -> dict:
        return {"node_id": node_id, "number": 1, "title": "t", "state": "open",
                "state_reason": None, "assignees": [], "url": "u",
                "updated_at": updated}

    def test_high_water_mark_is_the_newest_row(self) -> None:
        result = IssuesResult(
            ok=True, repo="o/r", fetched_at="2026-09-08T07:00:00Z",
            issues=[self.row("a", "2026-09-01T00:00:00Z"),
                    self.row("b", "2026-09-08T07:00:33Z")],
        )
        self.assertEqual(result.high_water_mark, "2026-09-08T07:00:33Z")

    def test_high_water_mark_of_an_empty_page_is_none(self) -> None:
        """Not the epoch, and not 'now': a quiet repo must not have its mark
        rewound, and a mark from the local clock is not GitHub's."""
        self.assertIsNone(
            IssuesResult(ok=True, repo="o/r",
                         fetched_at="2026-09-08T07:00:00Z").high_water_mark
        )

    def test_issues_are_keyed_by_node_id(self) -> None:
        result = IssuesResult(
            ok=True, repo="o/r", fetched_at="x",
            issues=[self.row("I_a", "1"), self.row("I_b", "2")],
        )
        self.assertEqual(sorted(result.issues_by_node_id()), ["I_a", "I_b"])

    def test_a_successful_result_has_no_error_document(self) -> None:
        self.assertIsNone(
            IssuesResult(ok=True, repo="o/r", fetched_at="x").error_document()
        )

    def test_an_unknown_rate_serialises_to_null_not_a_half_object(self) -> None:
        self.assertIsNone(RateLimit().as_document())
        self.assertIsNone(RateLimit(remaining=10).as_document())
        self.assertIsNone(RateLimit(reset_at="x").as_document())

    def test_a_known_rate_serialises_with_what_it_knows(self) -> None:
        self.assertEqual(
            RateLimit(remaining=10, reset_at="x").as_document(),
            {"remaining": 10, "reset_at": "x"},
        )
        self.assertEqual(
            RateLimit(limit=5000, cost=1, remaining=10, reset_at="x").as_document(),
            {"limit": 5000, "cost": 1, "remaining": 10, "reset_at": "x"},
        )

    def test_an_unparseable_issue_node_is_dropped_not_guessed(self) -> None:
        """A row that cannot be keyed, numbered or given a state has nothing
        the mirror can do with it, and a placeholder would be a fabricated
        issue. Row-level, so one bad node does not lose the other 99."""
        for node in ({}, {"id": "", "number": 1, "state": "OPEN"},
                     {"id": "I_a", "number": "1", "state": "OPEN"},
                     {"id": "I_a", "number": 1, "state": "MERGED"},
                     {"id": "I_a", "number": True, "state": "OPEN"},
                     "not a dict", None):
            with self.subTest(node=node):
                self.assertIsNone(gh_issues._issue_row(node))

    def test_an_unrecognised_state_reason_becomes_null(self) -> None:
        """GitHub has added reasons since §5.4 was written (``DUPLICATE``).
        Coercing one onto its nearest neighbour would put a claim in the
        mirror that GitHub never made — absent beats wrong (§5.3)."""
        row = gh_issues._issue_row({
            "id": "I_a", "number": 1, "title": "t", "state": "CLOSED",
            "stateReason": "DUPLICATE", "url": "u", "updatedAt": "t",
            "assignees": {"nodes": []},
        })
        self.assertEqual(row["state"], "closed")
        self.assertIsNone(row["state_reason"])

    def test_a_malformed_assignee_is_dropped_and_the_row_survives(self) -> None:
        row = gh_issues._issue_row({
            "id": "I_a", "number": 1, "title": "t", "state": "OPEN",
            "url": "u", "updatedAt": "t",
            "assignees": {"nodes": [{"login": None}, "x",
                                    {"login": "real", "avatarUrl": None}]},
        })
        self.assertEqual(row["assignees"], [{"login": "real", "avatar_url": None}])

    def test_gh_available_never_raises(self) -> None:
        self.assertFalse(gh_issues.gh_available("definitely-not-a-binary-xyz"))

    def test_the_error_document_matches_the_schema_shape(self) -> None:
        doc = IssuesResult(
            ok=False, repo="o/r", fetched_at="2026-09-08T07:00:00Z",
            error_kind="network", error="down",
        ).error_document()
        self.assertEqual(sorted(doc), ["at", "kind", "message"])
        self.assertIn(doc["kind"], ERROR_KINDS)


# ── the client's output, against the schema ───────────────────────────────────


@unittest.skipIf(Draft7Validator is None, _SKIP_JSONSCHEMA)
class ClientOutputValidatesTests(unittest.IsolatedAsyncioTestCase):
    """The half of W1 that keeps working: the *writer* is checked, not just
    a hand-written example. A client that starts emitting an undeclared key
    fails here on the next run rather than years later."""

    async def test_a_mirror_assembled_from_a_real_response_validates(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(gh_issues, "get_github_token", return_value=None):
            result = await fetch_open_issues("cjpais/Handy", gh_bin=_fake_gh(
                Path(tmp.name), stdout=json.dumps(OK_BODY)
            ))
        document = {
            "$schema": "xo/github-issues.schema.json",
            "schema": 1,
            "repo": result.repo,
            "fetched_at": result.fetched_at,
            "since": result.high_water_mark,
            "rate": result.rate.as_document(),
            "error": result.error_document(),
            "issues": result.issues_by_node_id(),
        }
        validator = Draft7Validator(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        )
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
        self.assertEqual(
            [f"/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
             for e in errors],
            [],
        )

    async def test_a_failed_polls_document_also_validates(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(gh_issues, "get_github_token", return_value=None):
            result = await fetch_open_issues("cjpais/Handy", gh_bin=_fake_gh(
                Path(tmp.name), stdout=json.dumps(NOT_FOUND_BODY), exit_code=1,
            ))
        document = {
            "schema": 1, "repo": result.repo, "fetched_at": result.fetched_at,
            "rate": result.rate.as_document(),
            "error": result.error_document(),
            "issues": {},
        }
        Draft7Validator(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        ).validate(document)


# ── live corroboration ────────────────────────────────────────────────────────


def _live_repo() -> str:
    return os.environ.get("XO_GITHUB_COST_TEST_REPO") or "quirq-ai/xo-space"


@unittest.skipIf(shutil.which("gh") is None, "gh is not installed")
class LiveGitHubTests(unittest.IsolatedAsyncioTestCase):
    """Confirms the measurement against GitHub itself — and never gates on it.

    Every assertion is preceded by a skip when the fetch did not succeed, so
    an unauthenticated machine, a dropped network or a repo that moved
    produces a *skip*, not a red suite. The offline guard in
    :class:`CostContractTests` is what actually holds the line; this is the
    thing that would have caught GitHub changing its own scoring.

    Costs one GraphQL point per run, out of 5,000/hr, and zero REST points —
    which is the property D5 was chosen for.
    """

    async def _fetch(self) -> IssuesResult:
        result = await fetch_open_issues(_live_repo(), timeout_s=25)
        if not result.ok:
            raise unittest.SkipTest(
                f"no live GitHub answer for {_live_repo()} "
                f"({result.error_kind}: {result.error})"
            )
        return result

    async def test_the_measured_cost_is_still_one_point(self) -> None:
        result = await self._fetch()
        if result.rate.cost is None:
            self.skipTest("GitHub did not report a cost")
        self.assertLessEqual(
            result.rate.cost, MAX_QUERY_COST,
            f"the poll now costs {result.rate.cost} points, not {MAX_QUERY_COST} "
            f"— the ~83-repo ceiling of §6.2/D6 has changed",
        )

    async def test_the_budget_is_the_graphql_one(self) -> None:
        """5,000 points/hr, separate from the core REST 5,000/hr the rest of
        this system spends. That separation is the whole reason for D5."""
        result = await self._fetch()
        self.assertEqual(result.rate.limit, 5000)
        self.assertIsNotNone(result.rate.reset_at)

    async def test_not_one_pull_request_comes_back(self) -> None:
        """§6.1, checked the only way it can be from outside: GraphQL's
        ``Issue.url`` is always ``/issues/<n>``, and a ``/pull/`` URL would
        mean the query had stopped being an issues query."""
        result = await self._fetch()
        self.assertTrue(
            all("/issues/" in row["url"] for row in result.issues),
            [row["url"] for row in result.issues if "/issues/" not in row["url"]],
        )

    async def test_live_rows_are_the_mirror_shape(self) -> None:
        result = await self._fetch()
        if not result.issues:
            self.skipTest(f"{_live_repo()} has no open issues right now")
        for row in result.issues:
            self.assertEqual(
                sorted(row),
                ["assignees", "node_id", "number", "state", "state_reason",
                 "title", "updated_at", "url"],
            )
            self.assertEqual(row["state"], "open")
            self.assertIsInstance(row["node_id"], str)

    async def test_a_deleted_repo_degrades_rather_than_raising(self) -> None:
        """The one live failure path worth exercising for real: it is the
        difference between a poller that skips a project and one that dies."""
        result = await fetch_open_issues(
            "quirq-ai/this-repository-does-not-exist-xo", timeout_s=25
        )
        if result.error_kind in ("not_authenticated", "network", "timeout", "no_cli"):
            self.skipTest(f"no live GitHub answer ({result.error_kind})")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "not_found")
        self.assertEqual(result.issues, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
