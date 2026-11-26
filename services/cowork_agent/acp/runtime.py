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
from utils.commands import run


class AcpRuntime:
    contract_version = c.CONTRACT_VERSION
    modes = ("ask", "accept_edits", "auto", "deny_unasked")
    efforts = ()
    auth_method = None
    extra_env = ()
    auth_env = ()

    def __init__(self):
        self.version = None

    def binary(self):
        found = locate(self.path_env, self.binaries)
        if found:
            return found
        raise c.RuntimeFailure("cli_missing", f"Install {self.name} or configure {self.path_env}")

    async def probe(self, *args):
        result = await run([self.binary(), *args], timeout=10, log_output=False,
                           env=await asyncio.to_thread(base_env, names=self.extra_env + self.auth_env),
                           separate_stderr=True)
        if result.timed_out:
            raise c.RuntimeFailure("timeout", f"{self.name} CLI probe timed out")
        return result

    async def describe(self):
        version = await self.probe("--version")
        if not version.ok or not version.output.strip():
            raise c.RuntimeFailure("protocol", f"Unable to identify {self.name} CLI")
        self.version = version.output.strip().splitlines()[0]
        return c.RuntimeInfo(cli_path=self.binary(), cli_version=self.version, logged_in=await self.logged_in())

    def features(self):
        features = {key: c.FeatureSupport(support="unsupported") for key in (
            *(field.name for field in fields(c.SessionSpec)), "tool_input_delta", "tool_output_delta",
            "tools.deny", "runs.message", "runs.list", "models", "oneshot", "usage",
            "runs.read", "runs.stop", "orchestration", "context.replace_at", "send_while_running",
            "update.permission_mode", "update.effort", "update.model", "usage_read", "permissions.auto")}
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
        items.append(c.SupportItem(feature="permissions.mode", support="emulated" if mode_supported else "refused",
                                   enforced_by="hook" if mode_supported else "none", mechanism="Session tool hook"))
        if spec.mcp_servers:
            report("mcp_servers", True, "ACP session MCP servers")
        if coordinator:
            report("coordinator_policy", spec.tools.preset == "read_only" and not spec.tools.ask and
                   set(spec.tools.deny) <= {"edit", "create", "delete", "web_fetch", "web_search", "worktree", "schedule"},
                   "Read-only preset")
            for key in ("orchestration", "tools.deny"):
                items.append(c.SupportItem(feature=key, support="emulated", enforced_by="hook",
                                           mechanism="Session-private coordinator guard"))
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
        session = AcpSession(session_id, spec, host, report, process, guard=guard)
        try:
            await guard.listen(session.tool_hook)
            await session.start(self.auth_method, self.session_mode(spec), self.version, self.session_meta(spec),
                                identity=self.identity)
            if not await guard.activated():
                raise c.RuntimeFailure("spec_refused", "Native CLI did not activate its Storm tool guard")
        except AcpResponseError as exc:
            await session.close()
            if exc.rpc_code == -32000 and "auth" in exc.message.lower():
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
