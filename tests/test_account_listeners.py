"""A sign-in's account listeners: Codex's ``openai`` credential read
from its own file, what OpenClaw and Hermes set up from it, the dispatch in
``events.changed``, and Cursor's and Grok Build's local login checks. Temp
homes only; no CLI or vendor is called."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.cowork_agent.adapters.claude_code import lifecycle as claude
from services.cowork_agent.adapters.claude_code.lifecycle import login as claude_login
from services.cowork_agent.adapters.claude_code.lifecycle import signin as claude_signin
from services.cowork_agent.adapters.codex.lifecycle import login as codex_login
from services.cowork_agent.adapters.cursor import lifecycle as cursor
from services.cowork_agent.adapters.grok_build import lifecycle as grok
from services.cowork_agent.adapters.hermes import account_listeners as hermes
from services.cowork_agent.adapters.openclaw import account_listeners as openclaw
from services.runtime_lifecycle import events
from services.runtime_lifecycle.contract import Credential


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class TempHome(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def patch(self, target, attribute: str, value) -> None:
        patcher = mock.patch.object(target, attribute, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **values: str) -> None:
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)


class CodexCredentialTest(TempHome):
    def test_the_chatgpt_login_with_its_email_and_expiry_and_release_deletes_it(self) -> None:
        self.env(CODEX_HOME=str(self.home))
        auth = self.home / "auth.json"
        auth.write_text(json.dumps({"tokens": {"access_token": "acc", "refresh_token": "ref",
                                               "id_token": _jwt({"email": "a@b.test", "exp": 100})}}))
        found = codex_login.credential()
        self.assertEqual((found.access, found.refresh, found.email, found.expires_ms),
                         ("acc", "ref", "a@b.test", 100_000))
        self.assertTrue(codex_login.signed_in())
        found.release()
        self.assertFalse(auth.exists())
        self.assertIsNone(codex_login.credential())

    def test_an_api_key_login_has_no_credential(self) -> None:
        self.env(CODEX_HOME=str(self.home))
        (self.home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk"}))
        self.assertTrue(codex_login.signed_in())
        self.assertIsNone(codex_login.credential())

    def test_a_key_variable_logs_codex_in_by_api_key_but_lands_no_sign_in(self) -> None:
        from services.cowork_agent.adapters.codex.lifecycle import SPEC
        self.env(CODEX_HOME=str(self.home), OPENAI_API_KEY="sk", CODEX_API_KEY="")
        self.assertEqual((SPEC.logged_in(), SPEC.via_api_key(), SPEC.sign_in.landed()), (True, True, False))
        self.assertTrue(SPEC.account().startswith("acct-"))


class OpenClawTest(TempHome):
    async def test_an_openai_sign_in_becomes_the_gateway_profile_the_agent_profile_and_the_env(self) -> None:
        config = self.home / "openclaw.json"
        config.write_text(json.dumps({"auth": {"profiles": {"x": {}}}, "agents": {"defaults": {}}}))
        upserts = []
        self.patch(openclaw, "OPENCLAW_JSON", config)
        self.patch(openclaw, "AGENTS_DIR", self.home / "agents")
        self.patch(openclaw, "upsert_env_entry", lambda k, v, path: upserts.append((k, v, path)))
        self.patch(openclaw, "get_active_agent", lambda: SimpleNamespace(env_file=self.home / ".env"))
        await openclaw.ON_SIGN_IN["openai"]("codex", Credential("acc", "ref", "a@b.test", 5000))
        self.assertEqual(upserts, [("OPENAI_CODEX_ACCESS_TOKEN", "acc", self.home / ".env")])
        data = json.loads(config.read_text())
        self.assertEqual(set(data["auth"]["profiles"]), {"x", "openai:a@b.test"})
        self.assertEqual(data["agents"]["defaults"]["model"]["primary"], openclaw.PRIMARY_MODEL)
        profiles = json.loads((self.home / "agents/main/agent/auth-profiles.json").read_text())["profiles"]
        self.assertEqual(profiles["openai-codex:a@b.test"]["refresh"], "ref")

    async def test_a_broken_config_is_left_alone_and_no_credential_does_nothing(self) -> None:
        config = self.home / "openclaw.json"
        config.write_text("{broken")
        self.patch(openclaw, "OPENCLAW_JSON", config)
        self.patch(openclaw, "AGENTS_DIR", self.home / "agents")
        self.patch(openclaw, "upsert_env_entry", lambda k, v, path: None)
        self.patch(openclaw, "get_active_agent", lambda: SimpleNamespace(env_file=self.home / ".env"))
        await openclaw.openai_signed_in("codex", Credential("acc", email="a@b.test"))
        self.assertEqual(config.read_text(), "{broken")
        self.assertFalse((self.home / "agents").exists())       # no refresh token: no agent profile
        await openclaw.openai_signed_in("codex", None)


class HermesTest(TempHome):
    async def test_an_openai_sign_in_becomes_the_provider_then_the_cli_copy_is_released(self) -> None:
        (self.home / "auth.json").write_text(json.dumps({"credential_pool": {"openai-codex": [1], "other": [2]}}))
        self.patch(hermes, "HERMES_DIR", self.home)
        self.patch(hermes, "get_agent", lambda name: SimpleNamespace(binary="hermes"))
        ran, released = [], []

        async def run(argv, **kwargs):
            ran.append(argv[1:])
            return SimpleNamespace(ok=True, output="")

        async def restart(action, timeout_s):
            ran.append([action])
            return 0, ""
        self.patch(hermes, "run", run)
        from services.cowork_agent.adapters.hermes import routes
        self.patch(routes, "_run_hermes_sh", restart)
        await hermes.ON_SIGN_IN["openai"]("codex", Credential("acc", "ref", release=lambda: released.append(1)))
        data = json.loads((self.home / "auth.json").read_text())
        self.assertEqual(data["active_provider"], "openai-codex")
        self.assertEqual(data["providers"]["openai-codex"]["tokens"], {"access_token": "acc", "refresh_token": "ref"})
        self.assertEqual(data["credential_pool"], {"other": [2]})
        self.assertEqual(ran, [["config", "set", k, v] for k, v in hermes.MODEL.items()] + [["restart"]])
        self.assertEqual(released, [1])


class DispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_space_agents_listener_runs_after_the_rest_and_a_failure_hides_nothing(self) -> None:
        heard = []

        def broken(runtime, credential):
            heard.append(("sign_in", runtime, credential))
            raise RuntimeError("boom")
        module = SimpleNamespace(ON_SIGN_IN={"openai": broken})
        self.addCleanup(events._on_change.remove, heard.append)
        events.on_change(heard.append)
        with mock.patch.object(events, "try_load_capability", return_value=module), \
                self.assertLogs(events.log, "ERROR"):
            await events.changed("codex", signed_in="openai", credential="c")
            await events.changed("codex", signed_in="anthropic")
        self.assertEqual(heard, ["codex", ("sign_in", "codex", "c"), "codex"])


class ClaudeConfigDirTest(TempHome):
    """With ``CLAUDE_CONFIG_DIR`` set, claude's sign-in lands on, marks and names the login in that folder, as the
    CLI keeps it there; never in the home folder."""

    def test_the_sign_in_follows_the_config_dir(self) -> None:
        home, config = self.home / "home", self.home / "config"
        home.mkdir(), config.mkdir()
        self.env(HOME=str(home), CLAUDE_CONFIG_DIR=str(config), **dict.fromkeys(claude_login.TOKEN_VARS, ""))
        self.assertFalse(claude.SPEC.sign_in.landed())
        (home / ".claude").mkdir()
        (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"refreshToken": "r"}}))
        self.assertFalse(claude.SPEC.sign_in.landed())          # the home folder's login isn't the CLI's now
        (config / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"refreshToken": "r"}}))
        self.assertTrue(claude.SPEC.sign_in.landed())
        (config / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "u-1"}}))
        claude_signin.mark_onboarded()
        written = json.loads((config / ".claude.json").read_text())
        self.assertEqual(written, {"oauthAccount": {"accountUuid": "u-1"}, "hasCompletedOnboarding": True})
        self.assertFalse((home / ".claude.json").exists())
        self.assertEqual(claude.SPEC.account(),
                         claude_login.account_fingerprint(config, config, {}))


class CliLoginTest(TempHome):
    """Cursor and Grok Build read their login as their runtime does, with the CLI; a sign-in lands only on its own
    login, never on a key variable; the advisory asks their runtime rather than polling that CLI call. Each check
    starts the CLI, so its answer is kept until the files its sign-in writes (or the key variables) change."""

    def setUp(self) -> None:
        super().setUp()
        self.env(XDG_CONFIG_HOME=str(self.home / "config"), CURSOR_CONFIG_DIR=str(self.home / ".cursor"),
                 GROK_HOME=str(self.home / ".grok"))
        for lifecycle in (cursor, grok):
            lifecycle.forget_login()
            self.addCleanup(lifecycle.forget_login)

    def probe(self, lifecycle, output: str, *, ok: bool = True, timed_out: bool = False) -> list[dict]:
        envs = []

        def run_sync(argv, **kwargs):
            envs.append(kwargs["env"])
            return SimpleNamespace(ok=ok, output=output, stderr="", timed_out=timed_out)
        self.patch(lifecycle, "run_sync", run_sync)
        return envs

    def sign_in(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_cursor_reads_status_and_lands_without_its_keys(self) -> None:
        self.env(CURSOR_API_KEY="k")
        envs = self.probe(cursor, json.dumps({"isAuthenticated": True}))
        self.assertTrue(cursor.logged_in())
        self.assertTrue(cursor.SPEC.sign_in.landed())
        self.assertEqual(["CURSOR_API_KEY" in env for env in envs], [True, False])
        self.probe(cursor, "not json")
        self.assertTrue(cursor.logged_in(), "kept: nothing it reads changed")
        self.sign_in(self.home / "config" / "cursor" / "auth.json", "{}")
        self.assertFalse(cursor.logged_in())

    def test_grok_reads_its_models(self) -> None:
        self.probe(grok, "Available models:\n* grok-4")
        self.assertTrue(grok.logged_in())
        self.probe(grok, "Error: not authenticated", ok=False)
        self.sign_in(self.home / ".grok" / "auth.json", "{}")
        self.assertFalse(grok.logged_in())

    def test_a_login_check_runs_once_until_what_it_reads_changes(self) -> None:
        envs = self.probe(cursor, json.dumps({"isAuthenticated": True}))
        for _ in range(3):
            self.assertTrue(cursor.logged_in())
        self.assertEqual(len(envs), 1)
        self.sign_in(self.home / ".cursor" / "cli-config.json", "{}")      # a sign-in writes its account here
        self.assertTrue(cursor.logged_in())
        self.env(CURSOR_API_KEY="other")                                    # a key variable changes it too
        self.assertTrue(cursor.logged_in())
        self.assertEqual(len(envs), 3)
        cursor.forget_login()                                               # a session found it signed out
        self.assertTrue(cursor.logged_in())
        self.assertEqual(len(envs), 4)

    def test_a_check_that_timed_out_is_asked_again_after_a_while(self) -> None:
        now = [0.0]
        self.patch(grok._login._kept, "_clock", lambda: now[0])
        envs = self.probe(grok, "", ok=False, timed_out=True)
        self.assertFalse(grok.logged_in())
        self.assertFalse(grok.logged_in())
        self.assertEqual(len(envs), 1)          # a CLI that hangs isn't started on every request
        now[0] += 31
        self.probe(grok, "Available models:\n* grok-4")
        self.assertTrue(grok.logged_in())

    def test_a_check_that_failed_is_never_kept_as_signed_out(self) -> None:
        # A network error or a vendor outage says nothing about the login: kept a short while, then asked again.
        for lifecycle, failed, answer in (
                (cursor, ("Error: getaddrinfo ENOTFOUND api2.cursor.sh", False), json.dumps({"isAuthenticated": True})),
                (grok, ("Error: 503 Service Unavailable", False), "Available models:\n* grok-4")):
            with self.subTest(runtime=lifecycle.__name__):
                now = [0.0]
                self.patch(lifecycle._login._kept, "_clock", lambda: now[0])
                envs = self.probe(lifecycle, failed[0], ok=failed[1])
                self.assertFalse(lifecycle.logged_in())
                self.assertFalse(lifecycle.logged_in())
                self.assertEqual(len(envs), 1)
                now[0] += 31
                self.probe(lifecycle, answer)
                self.assertTrue(lifecycle.logged_in())

    def test_a_definite_signed_out_is_kept_until_what_it_reads_changes(self) -> None:
        for lifecycle, answer in ((cursor, json.dumps({"isAuthenticated": False})),
                                  (grok, "Error: not authenticated")):
            with self.subTest(runtime=lifecycle.__name__):
                now = [0.0]
                self.patch(lifecycle._login._kept, "_clock", lambda: now[0])
                envs = self.probe(lifecycle, answer, ok=lifecycle is cursor)
                self.assertFalse(lifecycle.logged_in())
                now[0] += 3600
                self.assertFalse(lifecycle.logged_in())
                self.assertEqual(len(envs), 1)

    def test_a_kept_login_is_asked_again_once_its_token_lapses(self) -> None:
        now = time.time()
        envs = self.probe(grok, "Available models:\n* grok-4")
        auth = self.home / ".grok" / "auth.json"
        self.sign_in(auth, json.dumps({"https://auth.x.ai::u": {"expires_at": "2026-01-01T00:00:00Z"}}))
        with mock.patch("services.cowork_agent.adapters.grok_build.lifecycle.datetime") as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = datetime.fromtimestamp(1_700_000_000, timezone.utc)     # before it lapses
            self.assertTrue(grok.logged_in())
            self.assertTrue(grok.logged_in())
            self.assertEqual(len(envs), 1)
            clock.now.return_value = datetime.fromtimestamp(now, timezone.utc)              # after: asked again
            self.probe(grok, "Error: not authenticated", ok=False)
            self.assertFalse(grok.logged_in())

    def test_cursor_reads_only_its_tokens_expiry(self) -> None:
        def token(exp: float) -> str:
            payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
            return f"h.{payload}.s"
        auth = self.home / "config" / "cursor" / "auth.json"
        env = {"XDG_CONFIG_HOME": str(self.home / "config")}
        self.assertIsNone(cursor._lapsed(env))                       # no login: nothing to lapse
        self.sign_in(auth, json.dumps({"accessToken": token(time.time() + 3600), "refreshToken": "r"}))
        self.assertFalse(cursor._lapsed(env))
        self.sign_in(auth, json.dumps({"accessToken": token(time.time() - 1), "refreshToken": "r"}))
        self.assertTrue(cursor._lapsed(env))

    async def test_an_action_through_xo_drops_the_kept_login_before_anyone_reads_it(self) -> None:
        envs = self.probe(cursor, json.dumps({"isAuthenticated": True}))
        self.assertTrue(cursor.logged_in())
        heard = []
        self.patch(events, "_on_change", [lambda name: heard.append((name, cursor._login._kept._kept))])
        await events.changed("cursor")                               # a sign-out through XO, say
        self.assertEqual(heard, [("cursor", None)])
        self.probe(cursor, json.dumps({"isAuthenticated": False}))
        self.assertFalse(cursor.logged_in())
        self.assertEqual(len(envs), 1)                               # the second fake answered the second check

    async def test_a_check_that_started_before_a_change_is_not_shared_after_it(self) -> None:
        """Round 6: ``describe_once`` handed a caller after a sign-in the check that started before it."""
        import asyncio
        from services.cowork_agent.adapters import describe_once as once
        gates, started = [asyncio.Event(), asyncio.Event()], []

        class Runtime:
            async def describe(self):
                n = len(started)
                started.append(n)
                await gates[n].wait()
                return f"answer {n}"
        rt = Runtime()
        before = asyncio.ensure_future(once.describe_once("fake_rt", rt))
        for _ in range(5):
            await asyncio.sleep(0)
        self.patch(events, "_on_change", [])
        await events.changed("fake_rt")                              # a sign-in through XO
        after = asyncio.ensure_future(once.describe_once("fake_rt", rt))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(started, [0, 1])                            # its own check, not the one from before
        gates[0].set()
        gates[1].set()
        self.assertEqual((await before, await after), ("answer 0", "answer 1"))
        self.assertNotIn("fake_rt", once._running)

    def test_the_advisory_asks_their_runtime(self) -> None:
        from services.advisory.runtimes import DescribedCli, LocalCli, cli_readers
        readers = cli_readers()
        self.assertIsInstance(readers["cursor"], DescribedCli)
        self.assertIsInstance(readers["grok_build"], DescribedCli)
        self.assertIsInstance(readers["codex"], LocalCli)


if __name__ == "__main__":
    unittest.main()
