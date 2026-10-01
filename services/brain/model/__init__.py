"""The brain's pluggable model (Brain.md §9).

A model is anything with one coroutine, ``complete(prompt, system=...)``
returning text, and optionally ``embed(texts)`` returning one vector per
text. ``BRAIN_MODEL`` picks it:

* ``none`` (the default): statistics only. Learning and recall work; the
  reasoning features (designs, analogy explanations, relation naming)
  answer 501 ``model_required``.
* ``agent``: the Space's active agent (``AGENT_NAME``), through the same
  dispatcher chat uses. ``agent:<name>`` picks any installed harness
  instead (``agent:codex``, ``agent:hermes``, any folder under
  ``config/agents/``), so the brain can reason with a different harness than
  the one you chat with. Nothing else to configure, but every call is a
  full agent turn and shows up in that agent's sessions.
* the name of a module in this folder, or a dotted import path to your
  own: ``BRAIN_MODEL=my_models.brain`` loads ``Model`` from that module,
  ``BRAIN_MODEL=my_models.brain:Local`` loads ``Local``.

Your own model subclasses :class:`BrainModel`::

    from services.brain.model import BrainModel

    class Model(BrainModel):
        name = "local"

        async def complete(self, prompt, *, system="", max_tokens=2000):
            ...  # call your model, return its text

        async def embed(self, texts):   # optional
            ...  # return [[float, ...], ...], or None

A module that fails to import is reported by ``/api/brain/status`` with the
error and the brain keeps running without a model; it is never silently
swapped for another one.
"""

from __future__ import annotations

import importlib
import logging
from typing import Optional

from services.brain import config

logger = logging.getLogger(__name__)


class ModelUnavailable(Exception):
    """Raised when a reasoning step needs a model and none is connected."""


class BrainModel:
    """The contract. ``complete`` is required for reasoning; ``embed`` is
    optional and makes meaning vectors dense instead of TF-IDF."""

    name = "none"

    @property
    def can_reason(self) -> bool:
        return type(self).complete is not BrainModel.complete

    @property
    def can_embed(self) -> bool:
        return type(self).embed is not BrainModel.embed

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2000) -> str:
        raise ModelUnavailable("No model is connected (BRAIN_MODEL=none).")

    async def embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        return None

    def describe(self) -> dict:
        return {"name": self.name, "reason": self.can_reason, "embed": self.can_embed}


class _Broken(BrainModel):
    """Stands in for a model whose module failed to load, carrying why."""

    def __init__(self, requested: str, error: str) -> None:
        self.name = requested
        self.error = error

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2000) -> str:
        raise ModelUnavailable(f"BRAIN_MODEL={self.name} did not load: {self.error}")

    @property
    def can_reason(self) -> bool:
        return False

    def describe(self) -> dict:
        return {"name": self.name, "reason": False, "embed": False, "error": self.error}


_cache: dict[str, BrainModel] = {}


def _resolve(requested: str) -> BrainModel:
    if requested in ("", "none"):
        return BrainModel()
    module_name, _, attr = requested.partition(":")
    if "." not in module_name:
        module_name = f"{__name__}.{module_name}"
    module = importlib.import_module(module_name)
    named = getattr(module, attr, None) if attr else None
    if attr and not isinstance(named, type):
        # "module:argument" for a module whose Model takes one: agent:codex.
        model = getattr(module, "Model")(attr)
    else:
        model = (named or getattr(module, "Model"))()
    if not isinstance(model, BrainModel):
        raise TypeError(f"{requested} is not a services.brain.model.BrainModel")
    return model


def load_model(requested: Optional[str] = None) -> BrainModel:
    """The model ``BRAIN_MODEL`` names, loaded once per name."""
    name = requested if requested is not None else config.model_name()
    if name not in _cache:
        try:
            _cache[name] = _resolve(name)
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            logger.error("brain: BRAIN_MODEL=%s failed to load: %s", name, exc)
            _cache[name] = _Broken(name, f"{type(exc).__name__}: {exc}")
    return _cache[name]


def reset_cache() -> None:
    _cache.clear()
