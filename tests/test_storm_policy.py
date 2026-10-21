"""The policy engine's pieces (storm ch 15 §2–§4, ch 16 §2.1): the parser's
refusals, where a path lands, rules and their limits, the order of checks,
grants, coordinators, and failing closed. The bypass corpus is
``test_storm_policy_corpus.py``.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.contract import PermissionRequest
from services.cowork_agent.registry import agent_registry
from services.cowork_agent.storm.review import (
    DEFAULT_RULES,
    Action,
    Grant,
    Match,
    PolicyContext,
    Rule,
    SessionRef,
    Tier,
    action_sha256,
    decide,
    render_sentence,
    request_sha256,
    scope_refusal,
)
from services.cowork_agent.storm.review import policy
from services.cowork_agent.storm.review.options import Spec, read_options
from services.cowork_agent.storm.review.paths import Place, Places, cli_homes, declared_project_config_paths
from services.cowork_agent.storm.review.shell import MAX_COMMAND_CHARS, ShellRefused, canonical_command, parse
from services.cowork_agent.storm.review.tiers import _sed_only_prints

NOW = "2026-10-15T12:00:00Z"


class _Tree(unittest.TestCase):
    """A real home, project and worktree on disk, so symlinks resolve."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.project = self.home / "xo-projects" / "app"
        self.worktree = self.project / ".xo" / "worktrees" / "thread-1"
        (self.worktree / "src").mkdir(parents=True)
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "id_rsa").write_text("not a real key")
        self.places = Places(
            session_folder=self.worktree, home=self.home, state_root=self.home / ".quirq", worktree=self.worktree,
            project_folder=self.project,
            worktrees_root=self.project / ".xo" / "worktrees", git_common_dir=self.project / ".git",
            cli_homes=(self.home / ".claude",), project_config_paths=(".claude/**", ".mcp.json"))
        self.thread = SessionRef(id="cse_t", role="thread", cwd=self.worktree, project_pid="pid", thread_ts="t1",
                                 branch="xo/thread-abc123")

    def ctx(self, **kw) -> PolicyContext:
        return PolicyContext(places=self.places, now=NOW, **kw)

    def run_command(self, command: str, session: SessionRef | None = None, **kw):
        return decide(Action(kind="command", tool="Bash", session=session or self.thread, command=command,
                             raw={"command": command}), self.ctx(**kw))


class ParserTests(unittest.TestCase):
    def test_refuses_what_it_cannot_see_through(self) -> None:
        for text, why in (("echo $(id)", "substitution"), ("echo `id`", "substitution"), ("diff <(a) b", "process"),
                          ("cat <<EOF\nx\nEOF", "heredoc"), ("eval ls", "eval"), ("if x; then y; fi", "control flow"),
                          ("echo 'open", "quoting"), ("ls >", "no target"), ("x" * (MAX_COMMAND_CHARS + 1), "longer")):
            with self.subTest(text=text[:20]), self.assertRaises(ShellRefused) as caught:
                parse(text)
            self.assertIn(why, caught.exception.reason)

    def test_splits_every_compound_form(self) -> None:
        commands = parse("A=1 a x; b && c || d | e & (f) {\ng\n}")
        self.assertEqual([c.name for c in commands], list("abcdefg"))
        self.assertEqual(commands[0].assignments, ("A=1",))

    def test_redirections(self) -> None:
        cmd, = parse("cmd arg 2>err.log >> out.txt 2>&1 < in.txt")
        self.assertEqual(cmd.argv, ("cmd", "arg"))
        self.assertEqual([(r.op, r.target, r.is_file, r.writes) for r in cmd.redirects], [
            (">", "err.log", True, True), (">>", "out.txt", True, True), (">&", "1", False, True),
            ("<", "in.txt", True, False)])


class ActionHashTests(unittest.TestCase):
    """The canonical action hash (ch 15 §6.3): whitespace the shell ignores
    is the same action; a quoted operator, or other arguments, is not."""

    @staticmethod
    def digest(command: str) -> str:
        return action_sha256(kind="command", tool="Bash", command=command)

    def test_whitespace_variants_hash_the_same(self) -> None:
        base = self.digest("git push origin main")
        for variant in ("git push  origin main", "  git push origin main ", "git\tpush origin   main",
                        "git push origin main\t"):
            with self.subTest(variant=variant):
                self.assertEqual(self.digest(variant), base)
        self.assertEqual(self.digest("a  &&  b"), self.digest("a && b"))

    def test_quoted_and_unquoted_operators_differ(self) -> None:
        for quoted, real in (("echo a '&&' b", "echo a && b"), ('echo a "&&" b', "echo a && b"),
                             ("echo a '|' rm -rf x", "echo a | rm -rf x"), ("echo a \\; rm x", "echo a ; rm x"),
                             ("echo a ';' rm x", "echo a ; rm x")):
            with self.subTest(quoted=quoted):
                self.assertNotEqual(self.digest(quoted), self.digest(real))

    def test_different_arguments_differ(self) -> None:
        base = self.digest("git push origin main")
        for other in ("git push origin dev", "git push origin 'main '", "git push origin main --force",
                      'git push "origin  main"', "git push origin\\ main"):
            with self.subTest(other=other):
                self.assertNotEqual(self.digest(other), base)
        self.assertNotEqual(self.digest('echo "a  b"'), self.digest('echo "a b"'))     # quoted spaces are kept
        self.assertNotEqual(self.digest("ls 2>f"), self.digest("ls 2 > f"))          # the descriptor is an argument

    def test_a_carriage_return_is_part_of_a_word(self) -> None:
        """bash reads CR as a word character, so it never collapses."""
        self.assertNotEqual(self.digest("rm\r-rf\rbuild"), self.digest("rm -rf build"))
        self.assertNotEqual(self.digest("ls\r  x"), self.digest("ls x"))

    def test_ansi_c_quoting_is_hashed_as_given(self) -> None:
        """``\\'`` is an escaped quote inside ``$'…'``, which the scan doesn't
        model: a line with ``$'`` is hashed raw."""
        for raw in ("echo $'a\\'  b' # '", "echo $'a  b'"):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_command(raw), raw)
        self.assertNotEqual(self.digest("echo $'a\\'  b' # '"), self.digest("echo $'a\\' b' # '"))

    def test_a_line_the_parser_refuses_is_hashed_as_given(self) -> None:
        for raw in ("echo $(id)  x", "echo ${x:-a  b}", "echo 'open  "):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_command(raw), raw)
        self.assertNotEqual(self.digest("echo $(id)  x"), self.digest("echo $(id) x"))


class SedScriptTests(unittest.TestCase):
    def test_scripts_that_only_print_or_replace(self) -> None:
        for script in ("p", "1,5p", "/error/p", "s/foo/bar/g", "s|a/b|c|", "$d", "1!d", "/a/,/b/p;s/x/y/2p",
                       "s/\\/w/x/", "=", "10q"):
            with self.subTest(script):
                self.assertTrue(_sed_only_prints(script))

    def test_anything_else_may_write_or_run(self) -> None:
        for script in ("w out", "1w .mcp.json", "s/x/y/w .mcp.json", "s/x/date/e", "e ls", "W f", "r f",
                       "s/a/b", "y/ab/cd/", "1,", "s/a/b/gw f", "{p}", "a text"):
            with self.subTest(script):
                self.assertFalse(_sed_only_prints(script))


class OptionsTests(unittest.TestCase):
    def test_every_spelling_a_program_accepts_reads_the_same(self) -> None:
        spec = Spec(short_valued="o", long_valued=frozenset({"--output"}))
        for args in (["-o", "f", "u"], ["-of", "u"], ["-sof", "u"], ["u", "-o", "f"], ["--output", "f", "u"],
                     ["--output=f", "u"]):
            with self.subTest(args=args):
                options = read_options(args, spec)
                self.assertEqual((options.value("-o", "--output"), options.operands), ("f", ("u",)))

    def test_clusters_end_of_options_and_optional_values(self) -> None:
        options = read_options(["-sO", "u", "--", "-x"], Spec())
        self.assertEqual((sorted(options.names()), options.operands), (["-O", "-s"], ("u", "-x")))
        sed = read_options(["-Ei.bak", "s/a/b/", "f"], Spec(short_optional="i"))
        self.assertEqual((sed.value("-i"), sed.operands), (".bak", ("s/a/b/", "f")))
        self.assertEqual(read_options(["a", "-r"], Spec(permute=False)).operands, ("a", "-r"))


class PlacesTests(_Tree):
    def test_where_a_path_lands(self) -> None:
        cases = {"src/app.py": Place.INSIDE, "../thread-2/x": Place.OTHER_WORKTREE, "/tmp/x": Place.OUTSIDE,
                 "~/.ssh/config": Place.SECRET, ".env.local": Place.SECRET, "~/.quirq/engine/xo.db": Place.STATE_ROOT,
                 "~/.claude/settings.json": Place.CLI_HOME, "~/.claude.json": Place.CLI_HOME,
                 "/proc/1/environ": Place.SECRET}
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(self.places.place(path, self.worktree), expected)

    def test_the_common_credential_files_in_the_home_are_secrets(self) -> None:
        """go/no-go 2, S6."""
        for path in ("~/.netrc", "~/.git-credentials", "~/.docker/config.json", "~/.npmrc", "~/.pypirc",
                     "~/.kube/config", "~/.config/gcloud/credentials.db", "~/.azure/msal_token_cache.json",
                     "~/.gnupg/private-keys-v1.d/x.key", "~/.pgpass", "~/.cargo/credentials.toml", "~/.vault-token"):
            with self.subTest(path=path):
                self.assertEqual(self.places.place(path, self.worktree), Place.SECRET)
        for path in ("~/.docker/buildx/instances", "~/.kube/cache/discovery", "~/.config/nvim/init.lua"):
            with self.subTest(path=path):
                self.assertEqual(self.places.place(path, self.worktree), Place.HOME_CONFIG)

    def test_a_read_of_everything_under_a_folder_holding_a_secret_place_is_a_secret_read(self) -> None:
        """go/no-go 2, S6: ``grep -r KEY ~`` reads ``~/.ssh`` as surely as ``cat`` does."""
        coordinator = SessionRef(id="cse_c", role="coordinator", cwd=self.home)
        secret = ("grep -r KEY ~", "grep -rn KEY /", "grep --directories=recurse KEY ~", "egrep -R KEY ~/..",
                  "rg --hidden KEY ~", "rg -uu KEY ~", "rg KEY ~/.config", "diff -r ~ /tmp/x")
        for command in secret:
            for session in (self.thread, coordinator):
                with self.subTest(command=command, role=session.role):
                    verdict = self.run_command(command, session)
                    self.assertEqual((verdict.effect, verdict.tier), ("ask", Tier.DESTRUCTIVE), verdict)
        # A folder with no secret place in it, or a read that skips dot folders, reads no credentials.
        self.assertEqual(self.run_command("grep -rn TODO src").effect, "allow")
        self.assertEqual(self.run_command("rg KEY ~").tier, Tier.NOTABLE)
        self.assertEqual(self.run_command("grep KEY ~/notes.txt").tier, Tier.NOTABLE)
        # With no folder named, it reads the working folder: a coordinator's may be the home.
        self.assertEqual(self.run_command("grep -r KEY", coordinator).tier, Tier.DESTRUCTIVE)
        self.assertEqual(self.run_command("grep -r KEY").effect, "allow")
        search = decide(Action(kind="search", tool="Grep", session=coordinator, paths=(str(self.home),)), self.ctx())
        self.assertEqual((search.effect, search.tier), ("ask", Tier.DESTRUCTIVE))
        listing = decide(Action(kind="list", tool="LS", session=coordinator, paths=(str(self.home),)), self.ctx())
        self.assertEqual(listing.effect, "allow")

    def test_a_symlink_to_a_secret_is_a_secret(self) -> None:
        (self.worktree / "notes").symlink_to(self.home / ".ssh" / "id_rsa")
        self.assertEqual(self.places.place("notes", self.worktree), Place.SECRET)
        self.assertEqual(self.run_command("cat notes").tier, Tier.DESTRUCTIVE)

    def test_protected_paths_are_relative_to_the_worktree(self) -> None:
        self.assertIsNotNone(self.places.protected(".claude/settings.json", self.worktree))
        self.assertIsNotNone(self.places.protected("src/../.mcp.json", self.worktree))
        self.assertIsNone(self.places.protected("src/.claude/notes.md", self.worktree))
        self.assertIsNotNone(self.places.protected(str(self.project / ".git" / "config"), self.worktree))

    def test_an_unknown_working_folder_places_nothing(self) -> None:
        self.assertIsNone(self.places.place("x", None))
        self.assertEqual(self.places.place("/tmp/x", None), Place.OUTSIDE)

    def test_a_manifest_s_project_config_must_be_a_list_of_globs(self) -> None:
        for value in ('".made-up/**"', '[""]', '[1]', '{"a": 1}'):
            with self.subTest(value=value):
                manifest = self.home / "manifest.json"
                manifest.write_text('{"name": "made_up", "binary": "made-up", "home_dir": "~/.made-up", "env_file": '
                                    '"x", "config_file": "x", "agents_dir": "x", "project_config_paths": '
                                    f'{value}}}')
                with self.assertRaises(ValueError):
                    agent_registry._build_manifest(manifest)

    def test_each_agent_declares_its_project_config_in_its_manifest(self) -> None:
        """ch 16 §2.1 (go/no-go 2, S5): watched whichever agent runs, one with no runtime included."""
        declared = {"codex": (".codex/**",), "antigravity": (".agents/**",)}
        for name, globs in declared.items():
            with self.subTest(agent=name):
                self.assertEqual(agent_registry.get_agent(name).project_config_paths, globs)
                self.assertTrue(set(globs) <= set(declared_project_config_paths()))

    def test_cli_homes_come_from_the_manifests(self) -> None:
        # The registry expands each manifest's home_dir against the real home.
        homes = cli_homes()
        self.assertIn(Path.home() / ".claude", homes)
        self.assertIn(Path.home() / ".gemini", homes, "a home_dir below a dot folder protects the whole folder")


class RuleTests(unittest.TestCase):
    def test_limits_by_tier(self) -> None:
        exact, prefix = Match(command=("git", "push", "origin", "main")), Match(prefix=("git", "push"))
        self.assertIsNone(scope_refusal(Tier.DESTRUCTIVE, "thread", exact))
        self.assertIsNotNone(scope_refusal(Tier.DESTRUCTIVE, "thread", prefix))
        self.assertIsNotNone(scope_refusal(Tier.DESTRUCTIVE, "project", exact))
        self.assertIsNone(scope_refusal(Tier.NOTABLE, "project", prefix))
        self.assertIsNotNone(scope_refusal(Tier.NOTABLE, "workspace", prefix))
        self.assertIsNone(scope_refusal(Tier.ROUTINE, "workspace", prefix))

    def test_the_sentence_the_person_sees(self) -> None:
        self.assertEqual(render_sentence("allow", "project", "command", Match(prefix=("npm", "test")), "billing-api"),
                         "In billing-api, run `npm test` without asking")
        self.assertEqual(render_sentence("deny", "thread", "command", Match(command=("git", "push")), None),
                         "In this thread, never run exactly `git push`")
        self.assertEqual(render_sentence("allow", "project", "edit_file", Match(path_glob="src/**"), "app"),
                         "In app, edit files matching `src/**` without asking")

    def test_default_rules_are_listed_and_cover_categories(self) -> None:
        self.assertEqual(len({r.id for r in DEFAULT_RULES}), len(DEFAULT_RULES))
        self.assertTrue(all(r.id.startswith("default:") and r.sentence for r in DEFAULT_RULES))


class OrderTests(_Tree):
    def rule(self, effect: str, **kw) -> Rule:
        fields = dict(id=f"rule_{effect}", scope="project", effect=effect, kind="command",
                      match=Match(prefix=("npm", "test")), sentence="npm test rule", tier=Tier.NOTABLE,
                      project_pid="pid")
        return Rule(**{**fields, **kw})

    def test_deny_beats_allow(self) -> None:
        verdict = self.run_command("npm test", rules=(self.rule("allow"), self.rule("deny")))
        self.assertEqual((verdict.effect, verdict.rules_used), ("deny", ("rule_deny",)))
        self.assertIn("npm test rule", verdict.message)

    def test_an_allow_rule_answers_and_is_counted(self) -> None:
        verdict = self.run_command("npm test -- --watch=false", rules=(self.rule("allow"),))
        self.assertEqual((verdict.effect, verdict.rules_used), ("allow", ("rule_allow",)))

    def test_a_rule_holds_only_in_its_scope_and_time(self) -> None:
        other_thread = replace(self.thread, thread_ts="t2")
        for rule, session in ((self.rule("allow", project_pid="other"), self.thread),
                              (self.rule("allow", scope="thread", thread_ts="t1"), other_thread),
                              (self.rule("allow", expires_at="2026-10-15T11:59:59Z"), self.thread)):
            with self.subTest(rule=rule.scope):
                self.assertEqual(self.run_command("npm test", session=session, rules=(rule,)).effect, "ask")

    def test_a_grant_is_bound_to_session_tool_and_exact_input(self) -> None:
        command = "npm install p-retry@6"
        grant = Grant(id="g1", session_id="cse_t", tool="Bash",
                      input_sha256=action_sha256(kind="command", tool="Bash", command=command),
                      expires_at="2026-10-15T12:30:00Z")
        self.assertEqual(self.run_command(command, grants=(grant,)).grant_id, "g1")
        for variant in (replace(grant, used_at="2026-10-15T11:00:00Z"), replace(grant, expires_at=NOW),
                        replace(grant, session_id="cse_other"), replace(grant, tool="Write")):
            with self.subTest(variant=variant):
                self.assertEqual(self.run_command(command, grants=(variant,)).effect, "ask")
        self.assertEqual(self.run_command("npm install p-retry@7", grants=(grant,)).effect, "ask")

    def test_a_grant_is_spent_by_its_whitespace_variant_and_nothing_else(self) -> None:
        grant = Grant(id="g1", session_id="cse_t", tool="Bash",
                      input_sha256=action_sha256(kind="command", tool="Bash", command="npm install p-retry@6"),
                      expires_at="2026-10-15T12:30:00Z")
        for variant in ("npm  install p-retry@6", " npm install\tp-retry@6 "):
            with self.subTest(variant=variant):
                self.assertEqual(self.run_command(variant, grants=(grant,)).grant_id, "g1")
        for other in ("npm install 'p-retry@6 '", "npm install p-retry@6 '&&' x", "npm install p-retry@6 && x",
                      "npm install p-retry@6 --force", "npm install \"p-retry@6\""):
            with self.subTest(other=other):
                self.assertIsNone(self.run_command(other, grants=(grant,)).grant_id)

    def test_a_write_without_the_runtimes_digest_binds_its_raw_input(self) -> None:
        """A runtime that leaves ``content_sha256`` empty fails safe: the hash
        binds the write's raw input, so a grant for one content is never
        spent by another."""
        def write(content: str, **kw) -> Action:
            return Action(kind="create_file", tool="Write", session=self.thread, paths=("src/new.py",),
                          raw={"file_path": "src/new.py", "content": content}, **kw)
        safe, evil = write("safe"), write("evil")
        self.assertNotEqual(safe.sha256, evil.sha256)
        grant = Grant(id="g1", session_id="cse_t", tool="Write", input_sha256=safe.sha256,
                      expires_at="2026-10-15T12:30:00Z")
        self.assertEqual(decide(safe, self.ctx(grants=(grant,))).grant_id, "g1")
        verdict = decide(evil, self.ctx(grants=(grant,)))
        self.assertEqual((verdict.effect, verdict.grant_id), ("ask", None))
        # The same through the request, and an edit too; a runtime's digest still wins.
        def request(kind: str, inp: dict, digest: str | None = None) -> PermissionRequest:
            return PermissionRequest(request_id="r", run=None, call_id=None, tool="Edit", kind=kind, summary="edit",
                                     input=inp, cwd=None, paths=("src/a.py",), content_sha256=digest)
        self.assertNotEqual(request_sha256(request("edit_file", {"new_string": "y"})),
                            request_sha256(request("edit_file", {"new_string": "z"})))
        self.assertEqual(request_sha256(request("edit_file", {"new_string": "y"}, "d1")),
                         request_sha256(request("edit_file", {"new_string": "z", "note": "x"}, "d1")))

    def test_protected_paths_beat_grants_and_rules(self) -> None:
        command = "echo x > .mcp.json"
        grant = Grant(id="g1", session_id="cse_t", tool="Bash",
                      input_sha256=action_sha256(kind="command", tool="Bash", command=command),
                      expires_at="2026-10-15T12:30:00Z")
        verdict = self.run_command(command, grants=(grant,))
        self.assertEqual((verdict.effect, verdict.grant_id), ("deny", None))
        self.assertIn(".mcp.json", verdict.message)

    def test_a_revoked_default_rule_asks(self) -> None:
        self.assertEqual(self.run_command("git status").effect, "allow")
        verdict = self.run_command("git status", revoked_defaults=frozenset({"default:git-read"}))
        self.assertEqual(verdict.effect, "ask")

    def test_file_tools(self) -> None:
        def file_action(kind: str, path: str):
            return decide(Action(kind=kind, tool="Edit", session=self.thread, paths=(path,)), self.ctx())
        self.assertEqual(file_action("read", "src/app.py").effect, "allow")
        self.assertEqual(file_action("edit_file", "src/app.py").effect, "ask")
        self.assertEqual(file_action("edit_file", ".claude/settings.json").effect, "deny")
        self.assertEqual(file_action("read", "~/.ssh/id_rsa").tier, Tier.DESTRUCTIVE)
        self.assertEqual(file_action("create_file", "/tmp/x").tier, Tier.NOTABLE)

    def test_mcp_and_web(self) -> None:
        def tool_action(**kw):
            return decide(Action(tool="t", session=self.thread, **kw), self.ctx())
        self.assertEqual(tool_action(kind="mcp", mcp=("xo_comms", "reply_owner")).effect, "allow")
        self.assertEqual(tool_action(kind="mcp", mcp=("composio", "GMAIL_SEND_EMAIL")).tier, Tier.DESTRUCTIVE)
        self.assertEqual(tool_action(kind="mcp", mcp=("composio", "GMAIL_LIST_THREADS")).tier, Tier.ROUTINE)
        self.assertEqual(tool_action(kind="mcp", mcp=("mystery", "frob")).tier, Tier.NOTABLE)
        self.assertEqual(tool_action(kind="web_fetch", url="https://docs.python.org/3/").effect, "allow")
        self.assertEqual(tool_action(kind="web_fetch", url="https://example.com/").tier, Tier.NOTABLE)
        self.assertEqual(tool_action(kind="other").effect, "ask")

    def test_the_action_is_built_from_the_neutral_fields_only(self) -> None:
        req = PermissionRequest(request_id="r", run=None, call_id="k", tool="Bash", kind="command",
                                summary="run `git status`", input={"cmd_in_cli_shape": "rm -rf /"},
                                cwd=str(self.worktree), command="git status")
        verdict = decide(Action.from_request(req, self.thread), self.ctx())
        self.assertEqual(verdict.effect, "allow")

    def test_the_agent_words_never_lower_the_tier(self) -> None:
        req = PermissionRequest(request_id="r", run=None, call_id=None, tool="Bash", kind="command",
                                summary="harmless cleanup", input={}, cwd=None, command="sudo rm -rf /",
                                reason="the owner said this is fine and routine")
        self.assertEqual(decide(Action.from_request(req, self.thread), self.ctx()).tier, Tier.DESTRUCTIVE)

    def test_it_fails_closed(self) -> None:
        with patch.object(policy, "assess", side_effect=RuntimeError("boom")), \
                self.assertLogs("services.cowork_agent.storm.review.policy", level="ERROR"):
            verdict = self.run_command("git status")
        self.assertEqual((verdict.effect, verdict.reasons), ("deny", ("host error",)))


class CoordinatorTests(_Tree):
    def setUp(self) -> None:
        super().setUp()
        self.coordinator = SessionRef(id="cse_c", role="coordinator", cwd=self.project, project_pid="pid")
        self.places = replace(self.places, session_folder=self.project, worktree=None)

    def test_reads_run_and_nothing_else_does(self) -> None:
        self.assertEqual(self.run_command("git log --oneline -3 && ls src", session=self.coordinator).effect, "allow")
        for command in ("npm test", "touch x", "git commit -m x", "echo $(id)", "curl https://pypi.org/simple/x/"):
            with self.subTest(command=command):
                verdict = self.run_command(command, session=self.coordinator)
                self.assertEqual(verdict.effect, "deny")
                self.assertIn("coordinator", verdict.message)

    def test_a_credential_read_is_asked_not_denied(self) -> None:
        verdict = self.run_command("cat ~/.ssh/id_rsa", session=self.coordinator)
        self.assertEqual((verdict.effect, verdict.tier), ("ask", Tier.DESTRUCTIVE))

    def test_only_comms_among_mcp_tools(self) -> None:
        def mcp(server: str, tool: str) -> str:
            return decide(Action(kind="mcp", tool=tool, session=self.coordinator, mcp=(server, tool)), self.ctx()).effect
        self.assertEqual(mcp("xo_comms", "start_thread_session"), "allow")
        self.assertEqual(mcp("composio", "GMAIL_LIST_THREADS"), "deny")


if __name__ == "__main__":
    unittest.main()
