"""The model selector lists deployed policies; no fictional LLM catalog."""

import asyncio

from .artifacts import list_flies


def list_models() -> list[dict]:
    # The core /api/models route is synchronous and runs in FastAPI's worker
    # thread. Async adapter routes call artifacts.list_flies directly.
    return [{
        "id": fly["model"], "name": fly["name"], "provider_id": "fly",
        "capabilities": {"function_calling": False, "vision": False,
                         "reasoning": False, "json_output": True},
        "pricing": {"prompt": 0, "completion": 0},
        "metadata": {"fly_id": fly["id"], "revision": fly["revision"],
                     "digest": fly["digest"], "task_family": fly["taskFamily"],
                     "local_execution": True, "token_billing": False},
    } for fly in asyncio.run(list_flies())]
