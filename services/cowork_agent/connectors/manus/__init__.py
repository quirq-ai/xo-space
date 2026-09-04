"""
Manus connector — API-key based.

The key lands in token.json under the provider key "manus" (see the
package-level ``token_store``).
"""

from .connector import (
    MANUS_API,
    delete_manus_key,
    get_manus_key,
    get_status,
    save_manus_key,
    validate_key,
)

__all__ = [
    "MANUS_API",
    "delete_manus_key",
    "get_manus_key",
    "get_status",
    "save_manus_key",
    "validate_key",
]
