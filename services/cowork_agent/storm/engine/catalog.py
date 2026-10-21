"""An agent's catalog: its models and the efforts each takes (design ch 10
§3.3), read from its runtime. Every model or effort storm passes to a CLI is
checked against it: the person's pins, a live session's change
(``Engine.update``) and the router's choice of a new thread's model and
effort (§0.105). Pure: the caller reads ``Runtime.models()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from services.cowork_agent.adapters.contract import Effort, FeatureList, FeatureSupport, ModelInfo

#: Every effort a runtime may offer, weakest first (the contract's ``Effort``).
EFFORTS: tuple[Effort, ...] = get_args(Effort)


@dataclass(frozen=True)
class Catalog:
    """A model takes the efforts it lists; one that lists none takes the
    feature list's, unless it takes no effort at all (``takes_effort``)."""

    agent: str
    models: tuple[ModelInfo, ...]
    feature_efforts: tuple[Effort, ...]

    @classmethod
    def of(cls, agent: str, models: list[ModelInfo], features: FeatureList) -> "Catalog":
        return cls(agent, tuple(models), features.features.get("effort", FeatureSupport()).values)

    def model(self, model_id: str | None) -> ModelInfo | None:
        return next((m for m in self.models if m.id == model_id), None)

    def efforts(self, model_id: str | None) -> tuple[Effort, ...]:
        """The efforts ``model_id`` takes; with no model named (or one not
        in the catalog), the feature list's."""
        found = self.model(model_id)
        if found is not None and not found.takes_effort:
            return ()
        return found.efforts if found is not None and found.efforts else self.feature_efforts

    def all_efforts(self) -> tuple[Effort, ...]:
        """Every effort some model takes (with no models, the feature
        list's), weakest first."""
        every = {e for m in self.models for e in self.efforts(m.id)} if self.models else set(self.feature_efforts)
        return tuple(e for e in EFFORTS if e in every)

    def problem(self, model: str | None, effort: str | None) -> str | None:
        """``unsupported_model`` or ``unsupported_effort`` when the catalog
        lacks the pair (the effort checked with the model), else None."""
        if model is not None and self.model(model) is None:
            return "unsupported_model"
        if effort is not None and effort not in self.efforts(model):
            return "unsupported_effort"
        return None

    def describe(self) -> str:
        """The catalog in one clause: its models by id (and name), and their
        efforts; a model that takes fewer, or none, is named with its own."""
        models = ", ".join(m.id if not m.name or m.name == m.id else f"{m.id} ({m.name})" for m in self.models)
        every = self.all_efforts()
        fewer = [f"{m.id} takes no effort" if not self.efforts(m.id) else f"{m.id} only {', '.join(self.efforts(m.id))}"
                 for m in self.models if self.efforts(m.id) != every]
        return (f"the models {models or 'none'}, at effort {', '.join(every) or 'none'}"
                + (f" ({'; '.join(fewer)})" if fewer else ""))
