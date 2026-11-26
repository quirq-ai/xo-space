"""Every runtime with a lifecycle: an adapter with a ``lifecycle`` capability
(its ``SPEC``) and the ``lifecycle`` block of its manifest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.cowork_agent.adapters.loader import list_capability_providers, load_capability
from services.cowork_agent.registry.agent_registry import UnknownRuntime, runtime
from services.runtime_lifecycle.contract import LifecycleError, LifecycleSpec

ACTIONS = ("install", "update", "uninstall", "sign_out")


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


def runtimes() -> dict[str, Runtime]:
    return {name: get(name) for name in list_capability_providers("lifecycle")}


def get(name: str) -> Runtime:
    try:
        manifest = runtime(name)
    except UnknownRuntime as exc:
        raise LifecycleError("unknown_runtime", str(exc), 404) from None
    if name not in list_capability_providers("lifecycle"):
        raise LifecycleError("unsupported", f"{name} has no lifecycle.", 501)
    return Runtime(name, load_capability("lifecycle", agent=name).SPEC, manifest.raw)
