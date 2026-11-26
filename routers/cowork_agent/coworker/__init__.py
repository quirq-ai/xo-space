"""Routes of the coworker add-ons (``services/coworker``): views that exist
only for the xo-coworker UI, under ``/api/coworker/``, kept apart from
Space's core routes so the core stays small. The GitHub page's routes keep
the ``/api/xo-projects/{id}/github/`` URLs of the core's issue list, which
the page reads beside them."""

from fastapi import APIRouter

from .finder import router as finder_router
from .github import router as github_router
from .versions import router as versions_router

coworker_routers: list[APIRouter] = [versions_router, finder_router, github_router]
