"""A sign-in's account listeners (plan §5.3): Codex's ``openai`` credential read
from its own file, what OpenClaw and Hermes set up from it, the dispatch in
``events.changed``, and Cursor's and Grok Build's local login checks. Temp
homes only; no CLI or vendor is called."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
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
    login, never on a key variable; the advisory asks their runtime rather than polling that CLI call."""

    def probe(self, lifecycle, output: str, *, ok: bool = True) -> list[dict]:
        envs = []

        def run_sync(argv, **kwargs):
            envs.append(kwargs["env"])
            return SimpleNamespace(ok=ok, output=output, stderr="")
        self.patch(lifecycle, "run_sync", run_sync)
        return envs

    def test_cursor_reads_status_and_lands_without_its_keys(self) -> None:
        self.env(CURSOR_API_KEY="k")
        envs = self.probe(cursor, json.dumps({"isAuthenticated": True}))
        self.assertTrue(cursor.logged_in())
        self.assertTrue(cursor.SPEC.sign_in.landed())
        self.assertEqual(["CURSOR_API_KEY" in env for env in envs], [True, False])
        self.probe(cursor, "not json")
        self.assertFalse(cursor.logged_in())

    def test_grok_reads_its_models(self) -> None:
        self.probe(grok, "Available models:\n* grok-4")
        self.assertTrue(grok.logged_in())
        self.probe(grok, "Error: not authenticated", ok=False)
        self.assertFalse(grok.logged_in())

    def test_the_advisory_asks_their_runtime(self) -> None:
        from services.advisory.runtimes import DescribedCli, LocalCli, cli_readers
        readers = cli_readers()
        self.assertIsInstance(readers["cursor"], DescribedCli)
        self.assertIsInstance(readers["grok_build"], DescribedCli)
        self.assertIsInstance(readers["codex"], LocalCli)


if __name__ == "__main__":
    unittest.main()
