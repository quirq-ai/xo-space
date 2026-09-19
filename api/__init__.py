"""The ``/api`` surface the Space UI and the xo-cowork frontend talk to.

Folder-based: ``routers/autoroutes.py`` walks this package at boot and the
folder is the URL, so ``api/files/routes.py`` serves ``/api/files/*`` and
``api/xo_projects/`` serves ``/api/xo-projects/*`` (underscores become
hyphens). Route modules declare paths relative to their folder; an empty
path is the folder itself. Helpers that are not routes are ``_private``
modules, which the loader never walks.
"""
