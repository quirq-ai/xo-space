"""Native ChatGPT login presence; API-key auth is not a subscription login."""
import json

from .paths import codex_home


def chatgpt_connected() -> bool:
    try:
        auth = json.loads((codex_home() / "auth.json").read_text())
        tokens = auth.get("tokens") if isinstance(auth, dict) else None
        return (
            isinstance(tokens, dict)
            and isinstance(tokens.get("access_token"), str)
            and bool(tokens["access_token"].strip())
            and auth.get("auth_mode") != "apikey"
        )
    except (OSError, ValueError):
        return False
