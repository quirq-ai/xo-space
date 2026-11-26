"""Every runtime with a lifecycle: an adapter with a ``lifecycle`` capability
(its ``SPEC``) and the ``lifecycle`` block of its manifest."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from services.cowork_agent.adapters.loader import list_capability_providers, load_capability
from services.cowork_agent.registry.agent_registry import UnknownRuntime, get_agent, runtime
from services.runtime_lifecycle.contract import LifecycleError, LifecycleSpec
from utils.commands import run

ACTIONS = ("install", "update", "uninstall", "sign_out")
VERSION_TIMEOUT_S = 30.0
_VERSION = re.compile(r"(\d+\.\d+\.\d+\S*)")


@dataclass(frozen=True)
class Runtime:
    name: str
    spec: LifecycleSpec
    manifest: Mapping[str, Any]

    @property
    def package(self) -> str:
        """The CLI's name, as people know it (the manifest's ``binary``)."""
        return str(self.manifest.get("binary") or self.name)

    @property
    def account_kind(self) -> str | None:
        return self.manifest.get("account_kind")

    def command(self, action: str) -> Mapping[str, Any] | None:
        """The manifest's entry for ``action`` (``command``, ``needs``, ``keeps``), or None."""
        found = (self.manifest.get("lifecycle") or {}).get(action)
        return found if isinstance(found, Mapping) and found.get("command") else None

    def legacy_paths(self, kind: str) -> list[str]:
        return list((self.manifest.get("legacy_paths") or {}).get(kind) or ())


async def version_of(binary: str) -> str | None:
    result = await run([binary, "--version"], timeout=VERSION_TIMEOUT_S, log_output=False)
    found = _VERSION.search(result.output) if result.ok else None
    return found.group(1) if found else None


def runtimes() -> dict[str, Runtime]:
    return {name: get(name) for name in list_capability_providers("lifecycle")}


def get(name: str) -> Runtime:
    """Any agent shipping a ``lifecycle`` (exceptions too); else 404 for a non-runtime, 501 for a runtime."""
    if name in list_capability_providers("lifecycle"):
        return Runtime(name, load_capability("lifecycle", agent=name).SPEC, get_agent(name).raw)
    try:
        runtime(name)
    except UnknownRuntime as exc:
        raise LifecycleError("unknown_runtime", str(exc), 404) from None
    raise LifecycleError("unsupported", f"{name} has no lifecycle.", 501)
