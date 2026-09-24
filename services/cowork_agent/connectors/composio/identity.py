"""Per-request identity for Composio, in bring-your-own-key mode.

There is no XO sign-in *ceremony* here: the request is gated by the browser guard (a
cross-site browser request is refused), and the Composio ``user_id`` is this backend's
XO account id (:mod:`.account_identity`), which is resolved once from xo-swarm-api and
cached. It is an account id, not a Space id, which is what lets a person connect Gmail
in one Space and turn it on in the next without connecting it again.

Two gates, and both fail closed: no Composio API key means no connectors at all, and no
known XO account id means there is nobody to connect *as*. The second is a 409
``composio_identity_required`` rather than a 401, matching ``composio_key_required``:
it is a state of this backend, not a rejected credential on the incoming request.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from routers.browser_guard import origin_allowed
from services.cowork_agent.connectors.composio import account_identity, byo_key

log = logging.getLogger(__name__)


def _identity_required() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "composio_identity_required",
            "detail": (
                "This Space does not know which XO account it acts for, so connectors "
                "are inactive. Sign in to XO (or set XO_API_KEY) and reload."
            ),
        },
    )


async def resolve_user(request: Request) -> str | None:
    """The Composio user id for this request, or None when Composio cannot run.

    None when no key is configured, and None when the XO account id is not known. Reads
    the cache only — never a round trip — because the soft paths (chat, ``/api/tools``)
    call this every turn and read None as "run without Composio tools".
    """
    if not byo_key.configured():
        return None
    return account_identity.account_id()


async def _account(request: Request) -> str | None:
    """The guard both dependencies share: origin, then the account id.

    **Resolves when the cache is cold**, rather than only reading it. The connector
    routes are where a Space discovers it has an identity at all, and leaving that to
    the boot sweep alone is how one ends up stuck at "key set, signed out": the sweep
    returns at its ``no_key`` gate (non-retryable, so the reconcile loop ends) and
    nothing afterwards resolves it. One round trip, then cached for the life of the pod
    and on disk across restarts; a failure is rate-limited inside
    :func:`account_identity.resolve`, so an outage costs one call per backoff window,
    not one per page load.
    """
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail="Cross-site request refused.")
    return account_identity.account_id() or await account_identity.resolve()


async def get_composio_user(request: Request) -> str:
    """FastAPI dependency for the Composio *action* routes.

    403s a cross-site browser request and 409s when the XO account id cannot be
    established. It does not require a key, so the key routes can report the "no key"
    state themselves rather than 403ing. Read routes that must render either way
    (``/toolkits``) use :func:`get_composio_user_optional`.
    """
    account = await _account(request)
    if not account:
        raise _identity_required()
    return account


async def get_composio_user_optional(request: Request) -> str | None:
    """Same guard, but an unknown account id is None instead of a 409.

    For the routes whose job is to *report* the state, which cannot report it from
    behind a 409.
    """
    return await _account(request)
