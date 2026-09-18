"""The toolkit catalog for dynamic connectors: a TTL-cached, paginated view over
``client.list_catalog`` plus the curated **featured** set.

System-design constraint: never materialise the whole catalog (~1500 toolkits). Every
call carries a ``limit`` and a ``cursor``; pages are cached per query for a short TTL so
a repeated browse costs one upstream call, not 1500 rows. The default UI view is the
featured set (a tiny fixed list), which needs no catalog call at all.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from services.cowork_agent.connectors.composio import client as _client

_TTL = float(os.getenv("COMPOSIO_CATALOG_TTL", "3600"))
# {(search, category, cursor, limit): (payload, expires_at)}; the category list is
# cached under the reserved key _CATS_KEY in the same dict, so invalidate() covers both.
_cache: dict[tuple, tuple[Any, float]] = {}
_CATS_KEY = ("__categories__",)


def invalidate() -> None:
    """Drop the cache. Test hook, and the seam a key change would use."""
    _cache.clear()


def featured() -> list[str]:
    """The curated toolkits shown before the user searches. Cheap: no catalog call."""
    from services.cowork_agent.connectors.composio import service
    return list(service.TOOLKITS)


def categories() -> list[dict[str, Any]]:
    """All toolkit categories (``[{id, name}]``), TTL-cached. One upstream call feeds
    the browse chips; it is not re-fetched per page or per search."""
    now = time.monotonic()
    hit = _cache.get(_CATS_KEY)
    if hit and hit[1] > now:
        return hit[0]
    payload = _client.list_categories()
    _cache[_CATS_KEY] = (payload, now + _TTL)
    return payload


def page(*, search: Optional[str] = None, category: Optional[str] = None,
         cursor: Optional[str] = None, limit: int = 25) -> dict[str, Any]:
    """One cached page of the catalog (``{items, next_cursor}``)."""
    key = (search or None, category or None, cursor or None, int(limit))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[1] > now:
        return hit[0]
    payload = _client.list_catalog(search=search, category=category,
                                   cursor=cursor, limit=limit)
    _cache[key] = (payload, now + _TTL)
    return payload
