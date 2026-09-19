"""MagicPath connector routes.

``MOUNT_ORDER`` pins this folder before ``api/connectors/vercel``: both
register ``GET /callback`` and the MagicPath dispatcher must match first
(see routes.py, "/callback ordering invariant").
"""

MOUNT_ORDER = -1
