"""Routes of the coworker add-ons (``services/coworker``): views that exist
only for the xo-coworker UI, under ``/api/coworker/``, kept apart from
Space's core routes so the core stays small."""

from fastapi import APIRouter

from .versions import router as versions_router

coworker_routers: list[APIRouter] = [versions_router]
