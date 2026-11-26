"""Every runtime with a lifecycle: an adapter with a ``lifecycle`` capability
(its ``SPEC``) and the ``lifecycle`` block of its manifest."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any, Mapping

from services.cowork_agent.adapters.loader import list_capability_providers, load_capability
from services.cowork_agent.registry.agent_registry import get_agent
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


def installed(binary: str) -> bool:
    if os.path.isabs(binary):
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return shutil.which(binary) is not None


def runtimes() -> dict[str, Runtime]:
    return {name: get(name) for name in list_capability_providers("lifecycle")}


def get(name: str) -> Runtime:
    if name not in list_capability_providers("lifecycle"):
        raise LifecycleError("unknown_runtime", f"No runtime is named {name!r}; GET /api/chat/runtimes lists them.",
                             404)
    return Runtime(name, load_capability("lifecycle", agent=name).SPEC, get_agent(name).raw)
