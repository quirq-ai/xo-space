from __future__ import annotations

import asyncio
from dataclasses import fields
from pathlib import Path

from .history import read_page
from .session import AcpSession
from .transport import AcpResponseError
from .guard import ToolGuard
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.cli_status import locate
from services.cowork_agent.adapters.env_whitelist import base_env
from services.cowork_agent.adapters.kept_check import KeptCheck, stamp
from utils.commands import run_sync

#: How long one probe of the CLI may run (seconds).
PROBE_TIMEOUT_S = 10


def _answered(result):
    """A probe that ran and printed something: an answer to keep."""
    return result.ok and bool(result.output.strip())


class AcpRuntime:
    contract_version = c.CONTRACT_VERSION
    modes = ("ask", "accept_edits", "auto", "deny_unasked")
    efforts = ()
    auth_method = None
    extra_env = ()
    auth_env = ()
    #: False: the CLI runs no hook over ACP, so its sessions open without XO's tool guard (a known, accepted gap).
    tool_guard = True

    #: The adapter's lifecycle module: its ``logged_in()``, ``login_decided()``, ``login_stamp()`` and
    #: ``login_result()`` (``kept_check.KeptLogin``) and ``forget_login()``. The runtime reads its login
    #: through it, so there is one check, not two.
    lifecycle = None
    #: The arguments that list its models (``parse_models`` reads the result). None: its login check is
    #: the run that lists them (``login_result()``), so it isn't started twice.
    models_args = ()

    # What a CLI adds to ACP, read by its sessions (``AcpSession``); the defaults add nothing.
    #: Its own tool names (lower case) and the kind XO reviews each as, over the common ones.
    tool_kinds = {}
    #: ``async steer(session, text) -> bool``: join a message sent mid-turn to the running turn. False
    #: (it failed, timed out or wasn't taken): the send fails as retryable and the engine holds the
    #: message for the turn's end, steering nothing more into that turn. None: no message mid-turn.
    steer = None

    def requests(self):
        """The requests it sends beyond ACP's own: method → ``async handler(session, params) -> result``.
        Any other is answered "method not found", as before."""
        return {}

    def tool_name(self, call):
        """A ``tool_call`` update's machine name of the tool, where the CLI says it."""
        return call.get("name")

    def __init__(self):
        self.version = None
        # Each CLI run kept until what it reads changes (``kept_check``): ``--version`` the binary, the model
        # list the binary and the login. One that gave no answer is asked again after ``RETRY_AFTER_S``.
        self._version = KeptCheck(_answered)
        self._models = KeptCheck(_answered)

    def binary(self):
        found = locate(self.path_env, self.binaries)
        if found:
            return found
        raise c.RuntimeFailure("cli_missing", f"Install {self.name} or configure {self.path_env}")

    def _run(self, binary, *args):
        """``binary *args`` as every probe runs it. Blocking."""
        return run_sync([binary, *args], timeout=PROBE_TIMEOUT_S, log_output=False, separate_stderr=True,
                        env=base_env(names=self.extra_env + self.auth_env))

    async def describe(self):
        """The CLI, its version and its login. Each is a CLI run, so each is
        kept until what it reads changes (``kept_check``): the binary for the
        version; the binary, the sign-in files and the key variables for the
        login. Asking again (Settings, the advisory, each cold start) starts
        nothing."""
        binary = self.binary()
        version = await asyncio.to_thread(
            lambda: self._version.get(stamp([binary]), lambda: self._run(binary, "--version")))
        if version.timed_out:
            raise c.RuntimeFailure("timeout", f"{self.name} CLI probe timed out")
        if not version.ok or not version.output.strip():
            raise c.RuntimeFailure("protocol", f"Unable to identify {self.name} CLI")
        self.version = version.output.strip().splitlines()[0]
        return c.RuntimeInfo(cli_path=binary, cli_version=self.version, logged_in=await self.logged_in())

    async def logged_in(self):
        """Its lifecycle's login check: the one check Settings, the advisory
        and a session's open all read, kept until the login changes."""
        return await asyncio.to_thread(self._lifecycle().logged_in)

    async def models(self):
        """Its models, as ``models_args`` lists them; kept until the binary or
        the login changes (another account may offer others)."""
        binary = self.binary()
        if self.models_args is None:
            result = await asyncio.to_thread(self._lifecycle().login_result)
            if result is None:
                raise c.RuntimeFailure("protocol", f"Unable to list {self.name}'s models")
        else:
            result = await asyncio.to_thread(
                lambda: self._models.get((stamp([binary]), self._lifecycle().login_stamp()),
                                         lambda: self._run(binary, *self.models_args)))
        if result.timed_out:
            raise c.RuntimeFailure("timeout", f"{self.name} CLI probe timed out")
        return self.parse_models(result)

    def parse_models(self, result):
        """The ``ModelInfo`` list in ``result``, the run of ``models_args``."""
        raise NotImplementedError

    def _lifecycle(self):
        if self.lifecycle is None:
            raise NotImplementedError(f"{type(self).__name__} names no lifecycle module, which holds its login check")
        return self.lifecycle

    def features(self):
        features = {key: c.FeatureSupport(support="unsupported") for key in (
            *(field.name for field in fields(c.SessionSpec)), "tool_input_delta", "tool_output_delta",
            "tools.deny", "runs.message", "runs.list", "models", "oneshot", "usage",
            "runs.read", "runs.stop", "orchestration", "context.replace_at", "send_while_running",
            "update.permission_mode", "update.effort", "update.model", "usage_read", "permissions.auto")}
        features["send_while_running"] = (
            c.FeatureSupport(support="native", behaviour="steered", note="a message sent mid-turn joins the turn")
            if self.steer else c.FeatureSupport(support="unsupported", behaviour="queued",
                                                note="a message sent mid-turn waits for the next turn"))
        for key in ("interrupt", "mcp_servers", "resume", "input.images", "cwd", "model", "models", "persist",
                    "runs.list", "runs.read", "tool_output_delta"):
            features[key] = c.FeatureSupport(support="native")
        features["orchestration"] = c.FeatureSupport(support="emulated", note="Requires active read-only CLI hook")
        features["tools.deny"] = c.FeatureSupport(support="emulated", note="Coordinator tool restrictions")
        features["permissions.auto"] = c.FeatureSupport(support="emulated", note="Storm policy checks native tool hooks")
        features["permissions.mode"] = c.FeatureSupport(support="emulated", values=self.modes)
        features["system_prompt"] = c.FeatureSupport(support="emulated", note="Appended session context")
        features["effort"] = c.FeatureSupport(support="native" if self.efforts else "unsupported", values=self.efforts)
        features["project_config_paths"] = c.FeatureSupport(values=self.config_globs)
        features["main_checkout_config_paths"] = c.FeatureSupport(values=self.config_globs)
        features["auth_env"] = c.FeatureSupport(values=self.auth_env)
        return c.FeatureList(agent=self.name, cli_version=self.version, contract_version=self.contract_version,
                             features=features)

    def check(self, spec):
        items = []
        def report(key, supported, mechanism):
            items.append(c.SupportItem(feature=key, support="native" if supported else "refused",
                                       enforced_by="cli" if supported else "none", mechanism=mechanism))
        coordinator = spec.orchestration == "coordinator"
        mode_supported = (spec.permissions.mode in self.modes and
                          ((spec.permissions.mode == "deny_unasked") == coordinator))
        hook = "Session tool hook" if self.tool_guard else "Session tool hook, which this CLI doesn't run (unguarded)"
        items.append(c.SupportItem(feature="permissions.mode", support="emulated" if mode_supported else "refused",
                                   enforced_by="hook" if mode_supported else "none", mechanism=hook))
        if spec.mcp_servers:
            report("mcp_servers", True, "ACP session MCP servers")
        if coordinator:
            report("coordinator_policy", spec.tools.preset == "read_only" and not spec.tools.ask and
                   set(spec.tools.deny) <= {"edit", "create", "delete", "web_fetch", "web_search", "worktree", "schedule"},
                   "Read-only preset")
            for key in ("orchestration", "tools.deny"):
                items.append(c.SupportItem(feature=key, support="emulated", enforced_by="hook",
                                           mechanism="Session-private coordinator guard" if self.tool_guard else hook))
        elif spec.tools.preset not in ("all", "standard") or spec.tools.allow or spec.tools.ask or spec.tools.deny:
            report("tools.deny", False, "Custom tool policy unavailable")
        if spec.permissions.sandbox or spec.permissions.writable_roots or spec.permissions.network is not None:
            report("permissions.sandbox", False, "Explicit sandbox constraints unavailable")
        if spec.effort and spec.effort not in self.efforts:
            report("effort", False, "Unsupported effort")
        if spec.system_prompt:
            items.append(c.SupportItem(feature="system_prompt", support="emulated", enforced_by="prompt",
                                       mechanism="ACP text context"))
        incompatible = {
            "add_dirs": spec.add_dirs, "isolated": spec.isolation != "user", "fork": spec.fork or spec.fork_at,
            "ephemeral": not spec.persist, "output_schema": spec.output_schema is not None,
            "host_tools": spec.host_tools, "agents": spec.agents, "limits": spec.limits != c.CliLimits(),
            "extensions": spec.extensions, "replace_prompt": spec.system_prompt and spec.system_prompt.mode == "replace",
        }
        for key, requested in incompatible.items():
            if requested:
                report(key, False, "Unavailable on this runtime")
        result = c.SupportReport(items=tuple(items))
        refused = [i.feature for i in items if i.support == "refused" or (spec.strict and i.support != "native")]
        if refused:
            raise c.SpecRefused(f"{self.name} cannot enforce {', '.join(refused)}", result)
        return result

    async def open(self, spec, host, *, session_id):
        report = self.check(spec)
        binary = self.binary()
        if self.auth_method and not await self.logged_in():
            if not await asyncio.to_thread(self._lifecycle().login_decided):
                # The check failed (a network error, a vendor outage): that says nothing about the login.
                raise c.RuntimeFailure("protocol", f"{self.name} couldn't say whether it is signed in; "
                                       "try again shortly", retryable=True)
            raise c.RuntimeFailure("auth_required", f"Sign in using {self.login_command}")
        env = await asyncio.to_thread(base_env, names=self.extra_env + self.auth_env)
        env.update(spec.env)
        guard = ToolGuard(plugin=self.plugin)
        try:
            argv = [binary, *self.arguments(spec, guard)]
            process = await host.launcher.spawn(argv, cwd=spec.cwd, env=env, label=self.name)
        except BaseException:
            await guard.close()
            raise
        session = AcpSession(session_id, spec, host, report, process, guard=guard, dialect=self)
        try:
            await guard.listen(session.tool_hook)
            await session.start(self.auth_method, self.session_mode(spec), self.version, self.session_meta(spec),
                                identity=self.identity)
            if self.tool_guard and not await guard.activated():
                cli = f"{self.name} {self.version}" if self.version else self.name
                raise c.RuntimeFailure("spec_refused", f"{cli} didn't run XO's tool-guard hook, so its tool calls "
                                       "can't be checked; the session was not opened")
        except AcpResponseError as exc:
            await session.close()
            if exc.rpc_code == -32000 and "auth" in exc.message.lower():
                await asyncio.to_thread(self._lifecycle().forget_login)    # its kept login said otherwise
                raise c.RuntimeFailure("auth_required", f"Sign in using {self.login_command}") from exc
            raise
        except BaseException:
            await session.close()
            raise
        return session

    async def usage(self):
        return c.UsageSnapshot()

    async def list_sessions(self, *, cwd=None, limit=50):
        def listed():
            sessions = [c.SessionInfo(native_id=native, cwd=folder)
                        for native, folder, _ in self.history.files() if cwd is None or cwd == folder]
            return sessions[:limit]
        return await asyncio.to_thread(listed)

    async def read_session(self, native_id, *, after=None, limit=50):
        def page():
            for native, _, path in self.history.files():
                if native == native_id:
                    return read_page(native, path, self.history.decode, after=after, limit=limit)
            raise c.RuntimeFailure("session_not_found", "Native session unavailable")
        return await asyncio.to_thread(page)

    def session_mode(self, spec):
        return None

    def session_meta(self, spec):
        return {}

    async def list_runs(self, native_id):
        def listed():
            return [c.RunInfo(run_id=key, parent_run_id=None, spawn_call_id=None, kind="subagent",
                              agent_type=None, title="Subagent", background=False, depth=1)
                    for key, _ in self.history.runs(native_id)]
        return await asyncio.to_thread(listed)

    async def read_run(self, native_id, run_id, *, after=None, limit=50):
        def page():
            for key, path in self.history.runs(native_id):
                if key == run_id:
                    return read_page(key, path, self.history.decode, after=after, limit=limit)
            raise c.RuntimeFailure("session_not_found", "Run transcript unavailable")
        return await asyncio.to_thread(page)
