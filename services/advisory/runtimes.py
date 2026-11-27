"""What the advisory reads from the runtimes, discovered, never named.

* **Usage:** every adapter that ships a runtime (capability ``runtime``, the
  adapter contract) and lists the ``usage_read`` feature as native (contract
  1.1, ``UsageReader``) can read its account's usage with no model call.
* **Its CLI:** a runtime with a lifecycle (capability ``lifecycle``) is read
  locally: its binary on PATH and its own login check, which reads files and
  calls no vendor, so it is ``polled``. Its version is read again only when
  the binary's file changes. A runtime without one, or whose lifecycle's
  login check runs its CLI (``login_polled`` false), is read through its
  ``describe()``, only when someone asks.

Adding an agent needs no change here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol

from services.advisory.model import RuntimeFacts
from services.cowork_agent.adapters.contract import RuntimeFailure, UsageReader
from services.cowork_agent.adapters.describe_once import describe_once
from services.cowork_agent.adapters.loader import list_capability_providers, try_load_capability
from services.runtime_lifecycle.contract import LifecycleSpec
from services.runtime_lifecycle.registry import version_of

log = logging.getLogger(__name__)

FEATURE = "usage_read"
#: ``describe()``'s failure that means the CLI itself isn't installed.
MISSING = "cli_missing"


def usage_readers() -> Mapping[str, UsageReader]:
    """Each such runtime, by agent. One whose module fails to load is left out
    and logged: its usage stays what its turns report."""
    found: dict[str, UsageReader] = {}
    for agent in list_capability_providers("runtime"):
        try:
            module = try_load_capability("runtime", agent=agent)
            runtime = module.runtime() if module is not None else None
            feature = runtime.features().features.get(FEATURE) if runtime is not None else None
        except Exception:  # noqa: BLE001 - one broken adapter must not hide the others' reads
            log.exception("advisory: %s's runtime couldn't be loaded; its usage isn't read", agent)
            continue
        if feature is not None and feature.support == "native" and callable(getattr(runtime, "read_usage", None)):
            found[agent] = runtime
    return found


class CliReader(Protocol):
    polled: bool        # read every few seconds; else only when someone asks

    async def read(self) -> RuntimeFacts | None: ...       # None: it can't be read now


#: Each binary's version, by its path and modification time: read again only when the file changes. One
#: entry per path: a new modification time replaces the old one.
_versions: dict[str, tuple[float, str]] = {}


def _local(name: str, spec: LifecycleSpec) -> tuple[RuntimeFacts, tuple[str, float] | None, str]:
    """The facts besides the version, the binary's (path, mtime) when installed, and the binary."""
    binary = spec.binary()
    path = binary if os.path.isabs(binary) else shutil.which(binary)
    try:
        stamp = (path, os.stat(path).st_mtime) if path and os.access(path, os.X_OK) else None
    except OSError:
        stamp = None
    if stamp is None:
        return RuntimeFacts(name, installed=False), None, binary
    signed_in = spec.logged_in()
    return RuntimeFacts(name, True, signed_in, spec.account() if signed_in else None,
                        via_api_key=signed_in and spec.via_api_key()), stamp, binary


@dataclass(frozen=True)
class LocalCli:
    name: str
    spec: LifecycleSpec
    polled = True

    async def read(self) -> RuntimeFacts:
        facts, stamp, binary = await asyncio.to_thread(_local, self.name, self.spec)
        if stamp is None:
            return facts
        path, mtime = stamp
        known = _versions.get(path)
        version = known[1] if known is not None and known[0] == mtime else await version_of(binary)
        if version is not None:
            _versions[path] = (mtime, version)
        return replace(facts, version=version, update=self.spec.update_note(version))


@dataclass(frozen=True)
class DescribedCli:
    name: str
    runtime: Any
    polled = False

    async def read(self) -> RuntimeFacts | None:
        try:
            info = await describe_once(self.name, self.runtime)
        except RuntimeFailure as exc:
            return RuntimeFacts(self.name, installed=False) if exc.code == MISSING else None
        update = info.probes.get("update")
        return RuntimeFacts(self.name, True, info.logged_in, info.account if info.logged_in else None,
                            info.cli_version, update if isinstance(update, str) else None,
                            via_api_key=info.logged_in and info.via_api_key)


def cli_readers() -> Mapping[str, CliReader]:
    """Each runtime's reader, by name. One whose module fails to load is left
    out and logged."""
    found: dict[str, CliReader] = {}
    for name in dict.fromkeys([*list_capability_providers("lifecycle"), *list_capability_providers("runtime")]):
        try:
            lifecycle = try_load_capability("lifecycle", agent=name)
            if lifecycle is not None and lifecycle.SPEC.login_polled:
                found[name] = LocalCli(name, lifecycle.SPEC)
            elif (module := try_load_capability("runtime", agent=name)) is not None:
                found[name] = DescribedCli(name, module.runtime())
            elif lifecycle is not None:
                found[name] = LocalCli(name, lifecycle.SPEC)
        except Exception:  # noqa: BLE001 - one broken adapter must not hide the others
            log.exception("advisory: %s couldn't be loaded; its CLI isn't read", name)
    return found
