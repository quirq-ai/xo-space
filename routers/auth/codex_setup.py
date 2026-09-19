"""
OpenAI Codex CLI device-code login.

Flow:
  POST /connect/codex  -> SSE stream
    1. Check if `codex` CLI (@openai/codex) is installed.
       If not, run `npm install -g @openai/codex` and stream install progress.
    2. Run `codex login --device-auth` via PTY.
    3. Stream output — CLI shows a short alphanumeric user code and verification URL.
    4. User visits the URL and enters the code; CLI polls until auth completes.
    5. Emit {type: "done", returncode: 0} on success.

SSE event types emitted:
  {type: "installing", package: "@openai/codex"}   — npm install starting
  {type: "install_log", line: "..."}               — npm install output
  {type: "stdout", line: "..."}                    — codex login output
  {type: "stderr", line: "..."}                    — codex login stderr (pipe mode only)
  {type: "done", returncode: N}                    — CLI exited
  {type: "error", error: "..."}                    — failure

No callback endpoint needed — device auth is handled entirely by the CLI.
"""

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

try:
    import fcntl
    import struct
    import termios
    import pty as _pty

    _HAS_PTY = hasattr(_pty, "openpty") and sys.platform != "win32"
except ImportError:
    _HAS_PTY = False

from fastapi import APIRouter
from fastapi.responses import StreamingResponse


CODEX_NPM_PACKAGE = "@openai/codex"
CODEX_SETUP_TIMEOUT_SECONDS = int(os.getenv("CODEX_SETUP_TIMEOUT", "900"))
_NPM_INSTALL_TIMEOUT_SECONDS = int(os.getenv("CODEX_NPM_INSTALL_TIMEOUT", "300"))
_SSE_HEARTBEAT_INTERVAL = 15

_TOKEN_ENV_KEYS = ["OPENAI_CODEX_ACCESS_TOKEN"]

_OPENCLAW_DEFAULT_PRIMARY_MODEL = "openai/gpt-5.5"


def _strip_ansi(line: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Zm]?", "", line)


def _project_env_path() -> str:
    # auth/ → routers/ → repo root. This module lives one level deeper than
    # routers/space.py, so `.parent.parent` (which was correct before the file
    # moved into routers/auth/) resolved to routers/.env — a file nothing loads.
    return os.getenv("DOTENV_PATH") or str(Path(__file__).resolve().parents[2] / ".env")


def _agent_env_path() -> str:
    """The active agent's ``.env`` (resolved from its manifest — e.g. the
    openclaw gateway's ``~/.openclaw/.env``)."""
    from services.cowork_agent.registry.agent_registry import get_active_agent
    return str(get_active_agent().env_file)


def _upsert_env_key(env_path: str, key: str, value: str) -> None:
    """Insert or update a single KEY="value" in a .env file. Creates file if missing."""
    if not os.path.isfile(env_path):
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        # 0600 on creation: the only keys written here are access tokens, and a
        # plain open() would inherit the umask and leave the new file
        # world-readable. An existing file keeps whatever mode its owner chose
        # (setup.sh already creates both .env files 0600).
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f'{key}="{value}"\n')
        return
    lines: list[str] = []
    found = False
    with open(env_path, "r") as f:
        for raw in f:
            if raw.strip().startswith(f"{key}="):
                lines.append(f'{key}="{value}"\n')
                found = True
            else:
                lines.append(raw)
    if not found:
        lines.append(f'\n{key}="{value}"\n')
    with open(env_path, "w") as f:
        f.writelines(lines)


def _persist_token_to_env_files(token: str) -> None:
    """Write OPENAI_CODEX_ACCESS_TOKEN to the project .env and the active
    agent's .env (resolved from its manifest)."""
    for env_path in [_project_env_path(), _agent_env_path()]:
        for key in _TOKEN_ENV_KEYS:
            try:
                _upsert_env_key(env_path, key, token)
            except OSError as e:
                print(f"[codex-setup] failed to write {key} to {env_path}: {e}")


def _decode_jwt_payload(jwt: str) -> Optional[dict]:
    """Decode a JWT's payload segment. No signature verification — caller trusts source."""
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return None
        seg = parts[1]
        pad = "=" * (-len(seg) % 4)
        decoded = base64.urlsafe_b64decode(seg + pad)
        payload = json.loads(decoded)
        return payload if isinstance(payload, dict) else None
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _read_codex_credentials() -> Optional[dict]:
    """
    After login succeeds the CLI writes credentials to a known location.
    Try common paths and return a dict if found:
        {"token": str,
         "email": Optional[str],
         "refresh": Optional[str],
         "expires_ms": Optional[int]}
    Email/refresh/expires_ms are only populated when reading the codex CLI's
    chatgpt-mode auth.json (which carries an id_token JWT and refresh_token).
    """
    xdg_data = os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    xdg_config = os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    candidates = [
        Path(xdg_data) / "codex" / "auth.json",
        Path(xdg_config) / "codex" / "auth.json",
        Path.home() / ".codex" / "auth.json",
        Path.home() / ".config" / "codex" / "auth.json",
        # openclaw auth store (written by our own write_auth_credentials previously)
        Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            token: Optional[str] = None
            email: Optional[str] = None
            refresh: Optional[str] = None
            expires_ms: Optional[int] = None
            if isinstance(data, dict):
                # codex CLI ChatGPT/device-auth mode: {"tokens": {"access_token": ..., "id_token": ..., "refresh_token": ..., "account_id": ...}, ...}
                tokens_obj = data.get("tokens")
                if isinstance(tokens_obj, dict):
                    token = tokens_obj.get("access_token") or tokens_obj.get("id_token")
                    rt = tokens_obj.get("refresh_token")
                    if isinstance(rt, str) and rt:
                        refresh = rt
                    id_token = tokens_obj.get("id_token")
                    if isinstance(id_token, str):
                        payload = _decode_jwt_payload(id_token)
                        if isinstance(payload, dict):
                            claim = payload.get("email")
                            if isinstance(claim, str) and claim:
                                email = claim
                            exp = payload.get("exp")
                            if isinstance(exp, (int, float)):
                                expires_ms = int(exp) * 1000
                # codex CLI API-key mode: {"auth_mode": "...", "OPENAI_API_KEY": "sk-..."}
                if not token:
                    api_key = data.get("OPENAI_API_KEY")
                    if isinstance(api_key, str):
                        token = api_key
                # legacy flat format: {"access_token": "..."}
                if not token:
                    token = data.get("access_token") or data.get("accessToken")
                # openclaw auth-profiles format
                if not token:
                    profiles = data.get("profiles", {})
                    for profile in profiles.values():
                        if isinstance(profile, dict):
                            token = profile.get("access") or profile.get("access_token")
                            if token:
                                break
            if token and isinstance(token, str) and len(token) > 20:
                print(f"[codex-setup] credentials read from {path}")
                return {
                    "token": token,
                    "email": email,
                    "refresh": refresh,
                    "expires_ms": expires_ms,
                }
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return None


# NOTE: the two writes below target the openclaw gateway's own credential store
# — openclaw.json and the main agent's auth-profiles.json — whose schemas are
# openclaw-specific. They are intentionally NOT routed through the active-agent
# manifest (that would write openclaw-shaped keys into another agent's config),
# and for the same reason they run ONLY when openclaw is the active agent; see
# the dispatch in codex_setup(). Old but needed; covered by the documented
# modularity allowlist (DEVELOPING.md §6).
def _openclaw_config_path() -> str:
    return str(Path.home() / ".openclaw" / "openclaw.json")


def _upsert_openclaw_config(email: str) -> None:
    """
    Update ~/.openclaw/openclaw.json post-login:
      1. auth.profiles["openai:<email>"] = {provider, mode, email}
      2. agents.defaults.model.primary = _OPENCLAW_DEFAULT_PRIMARY_MODEL (always overwrite)

    Best-effort: missing/malformed file is logged and skipped, never raised.
    Atomic write via temp file + os.replace so a crash mid-write cannot corrupt the file.
    """
    path = _openclaw_config_path()
    if not os.path.isfile(path):
        print(f"[codex-setup] openclaw.json not found at {path}, skipping config upsert")
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[codex-setup] failed to read {path} ({e}); skipping config upsert")
        return
    if not isinstance(data, dict):
        print(f"[codex-setup] {path} is not a JSON object; skipping config upsert")
        return

    auth = data.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        data["auth"] = auth
    profiles = auth.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        auth["profiles"] = profiles
    profile_key = f"openai:{email}"
    profiles[profile_key] = {
        "provider": "openai",
        "mode": "oauth",
        "email": email,
    }

    agents = data.get("agents")
    if not isinstance(agents, dict):
        agents = {}
        data["agents"] = agents
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        agents["defaults"] = defaults
    model = defaults.get("model")
    if not isinstance(model, dict):
        model = {}
        defaults["model"] = model
    model["primary"] = _OPENCLAW_DEFAULT_PRIMARY_MODEL

    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
        print(
            f"[codex-setup] openclaw.json updated: profile={profile_key}, "
            f"primary={_OPENCLAW_DEFAULT_PRIMARY_MODEL}"
        )
    except OSError as e:
        print(f"[codex-setup] failed to write {path} ({e}); skipping config upsert")
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _agent_auth_profiles_path() -> str:
    return str(Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json")


def _upsert_agent_auth_profile(
    email: str,
    access: str,
    refresh: str,
    expires_ms: int,
) -> None:
    """
    Upsert the email-keyed openai-codex oauth entry into the main agent's
    auth-profiles.json — the credential store openclaw consults at runtime.

    Schema matches the existing email-keyed entries:
        profiles["openai-codex:<email>"] = {
            "type": "oauth", "provider": "openai-codex",
            "access": <access_token>, "refresh": <refresh_token>,
            "expires": <ms epoch from JWT exp>, "email": <email>
        }

    Creates the file (and parent dirs) with `{"version": 1, "profiles": {...}}`
    if it doesn't exist. A malformed existing file is logged and skipped to
    avoid clobbering hand-edits in flight. Always overwrites an existing entry
    for this email so re-login refreshes tokens.
    """
    path = _agent_auth_profiles_path()
    created = False
    if not os.path.isfile(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError as e:
            print(f"[codex-setup] failed to create dir for {path} ({e}); skipping agent profile upsert")
            return
        data: dict = {"version": 1, "profiles": {}}
        created = True
    else:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[codex-setup] failed to read {path} ({e}); skipping agent profile upsert")
            return
        if not isinstance(data, dict):
            print(f"[codex-setup] {path} is not a JSON object; skipping agent profile upsert")
            return

    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        data["profiles"] = profiles
    profile_key = f"openai-codex:{email}"
    profiles[profile_key] = {
        "type": "oauth",
        "provider": "openai-codex",
        "access": access,
        "refresh": refresh,
        "expires": expires_ms,
        "email": email,
    }

    tmp_path = path + ".tmp"
    try:
        # This file holds a live access + refresh token — write it 0600 like the
        # hermes path does (_persist_token_to_hermes_auth). A plain open() here
        # inherited the process umask and left it world-readable (0644).
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)  # tighten a pre-existing 0644 file too
        except OSError:
            pass
        action = "created" if created else "updated"
        print(f"[codex-setup] agent auth-profiles.json {action}: profile={profile_key}")
    except OSError as e:
        print(f"[codex-setup] failed to write {path} ({e}); skipping agent profile upsert")
        try:
            os.remove(tmp_path)
        except OSError:
            pass


_HERMES_CODEX_PRIMARY_MODEL = "gpt-5.4"  # valid codex slug (see hermes codex_models.py)
_HERMES_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _persist_token_to_hermes_auth(token: str, refresh: Optional[str]) -> bool:
    """Write the codex OAuth credential into the DEFAULT hermes profile's
    ``auth.json`` using hermes's own canonical structure, mirroring hermes's
    ``_save_codex_tokens`` (hermes_cli/auth.py).

    The source of truth hermes reads is ``providers["openai-codex"]["tokens"]``
    — the ``credential_pool`` entry is *derived* from it at gateway load time
    (credential_pool.py seeds ``device_code`` from this state). Writing the
    pool directly does nothing; writing ``providers.tokens`` + flipping
    ``active_provider`` is what makes hermes pick up codex.

    Default profile only (``~/.hermes``): codex's OAuth refresh token is
    single-use, so fanning the same token across profiles would let them
    invalidate each other on independent refresh. Returns True on success.
    """
    from services.cowork_agent.adapters.hermes.paths import HERMES_DIR

    auth_path = HERMES_DIR / "auth.json"
    last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tokens: dict = {"access_token": token}
    if refresh:
        tokens["refresh_token"] = refresh

    try:
        if auth_path.is_file():
            try:
                data = json.loads(auth_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                print(f"[codex-setup] hermes auth.json unreadable ({e}); starting fresh")
                data = {}
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}

        data.setdefault("version", 1)
        # Clear any stale derived pool entry so hermes rebuilds it from tokens.
        pool = data.get("credential_pool")
        if isinstance(pool, dict):
            pool.pop("openai-codex", None)
        # Clear a prior suppression so the device_code source seeds again.
        sup = data.get("suppressed_sources")
        if isinstance(sup, dict):
            sup.pop("openai-codex", None)

        providers = data.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            data["providers"] = providers
        providers["openai-codex"] = {
            "tokens": tokens,
            "last_refresh": last_refresh,
            "auth_mode": "chatgpt",
        }
        data["active_provider"] = "openai-codex"
        data["updated_at"] = datetime.now(timezone.utc).isoformat()

        tmp = str(auth_path) + ".tmp"
        # auth.json holds secrets — keep it 0600 like hermes does.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, auth_path)
        print(f"[codex-setup] hermes default auth.json: providers.openai-codex.tokens set, active_provider=openai-codex")
        return True
    except OSError as e:
        print(f"[codex-setup] failed to write hermes auth.json ({e})")
        return False


def _set_hermes_primary_model() -> None:
    """Make ``openai-codex``/``gpt-5.4`` the primary model+provider for the
    DEFAULT hermes profile via ``hermes config set`` (mirrors openclaw's
    ``_upsert_openclaw_config``). Without this the profile keeps its prior
    default and never routes through codex even with the credential present.
    Best-effort; failures are logged, never raised."""
    try:
        from services.cowork_agent.registry.agent_registry import get_agent
        from services.cowork_agent.adapters.hermes.paths import HERMES_DIR
        hermes_bin = get_agent("hermes").binary
    except Exception as e:  # noqa: BLE001
        print(f"[codex-setup] can't resolve hermes binary; skipping primary-model set ({e})")
        return

    env = dict(os.environ)
    env["HERMES_HOME"] = str(HERMES_DIR)
    for key, value in (
        ("model.provider", "openai-codex"),
        ("model.base_url", _HERMES_CODEX_BASE_URL),
        ("model.default", _HERMES_CODEX_PRIMARY_MODEL),
    ):
        try:
            r = subprocess.run(
                [hermes_bin, "config", "set", key, value],
                cwd=str(HERMES_DIR), env=env, capture_output=True, text=True,
                timeout=30, close_fds=True, start_new_session=True, check=False,
            )
            if r.returncode != 0:
                print(f"[codex-setup] `hermes config set {key}` rc={r.returncode}: {(r.stderr or r.stdout or '').strip()[:200]}")
        except Exception as e:  # noqa: BLE001
            print(f"[codex-setup] `hermes config set {key}` failed ({e})")
    print(f"[codex-setup] hermes default primary model → openai-codex/{_HERMES_CODEX_PRIMARY_MODEL}")


def _remove_codex_cli_auth() -> None:
    """Delete ``~/.codex/auth.json`` after adopting its token into hermes.

    Codex's OAuth refresh tokens are single-use; if both the codex CLI store
    and hermes hold the same account's token, whichever refreshes first
    invalidates the other (``token_invalidated`` 401). Removing the CLI copy
    leaves hermes as the sole holder. Best-effort."""
    for p in (
        Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "codex" / "auth.json",
        Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "codex" / "auth.json",
        Path.home() / ".codex" / "auth.json",
        Path.home() / ".config" / "codex" / "auth.json",
    ):
        try:
            if p.is_file():
                p.unlink()
                print(f"[codex-setup] removed codex CLI auth {p} (single-holder for hermes)")
        except OSError as e:
            print(f"[codex-setup] couldn't remove {p} ({e})")


def _ensure_node_on_path() -> None:
    """
    Prepend the directory containing `npm` to PATH if it's not already there.

    Why: when the FastAPI process is launched outside an interactive shell
    (systemd, supervisor, coder-agent), nvm's shim never runs, so PATH lacks
    `~/.nvm/versions/node/<ver>/bin` and `shutil.which("npm")` returns None.
    The /connect/codex endpoint then bails with "npm not found" before any
    install attempt can happen.

    Order of search: existing PATH → ~/.nvm/versions/node/<latest>/bin →
    /usr/local/bin → /usr/bin. Idempotent and best-effort.
    """
    if shutil.which("npm"):
        return
    candidates: list[str] = []
    nvm_root = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm_root):
        try:
            for entry in sorted(os.listdir(nvm_root), reverse=True):
                candidates.append(os.path.join(nvm_root, entry, "bin"))
        except OSError:
            pass
    candidates.extend(["/usr/local/bin", "/usr/bin"])
    for bin_dir in candidates:
        if os.path.isfile(os.path.join(bin_dir, "npm")):
            cur = os.environ.get("PATH", "")
            if bin_dir not in cur.split(os.pathsep):
                os.environ["PATH"] = bin_dir + os.pathsep + cur
                print(f"[codex-setup] PATH augmented with {bin_dir}")
            return
    print(
        "[codex-setup] could not locate npm in PATH or nvm/system bin dirs; "
        f"checked={candidates!r}"
    )


def _codex_cli_path() -> str:
    path = (os.getenv("CODEX_CLI_PATH") or "codex").strip()
    if not os.path.isabs(path):
        found = shutil.which(path)
        if found:
            return found
    return path


def _is_codex_installed() -> bool:
    return shutil.which("codex") is not None


def _pty_wanted() -> bool:
    flag = os.getenv("CODEX_LOGIN_USE_PTY", "1").strip().lower()
    return flag not in ("0", "false", "no", "off") and _HAS_PTY


def _set_pty_winsize(slave_fd: int, rows: int = 24, cols: int = 120) -> None:
    if not _HAS_PTY:
        return
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


# Canonical route is /connect/codex. The pre-rename /codex/setup path is kept as
# a hidden legacy alias (include_in_schema=False) so an un-migrated xo-swarm
# frontend keeps working — remove it once all clients call /connect/codex.
router = APIRouter(tags=["codex-setup"])


@router.post("/connect/codex")
@router.post("/codex/setup", include_in_schema=False)  # legacy alias
async def codex_setup():
    """
    Run `codex login --device-auth` and stream its output via SSE.

    Installs the codex CLI via npm on first use. Subsequent calls skip install
    because `shutil.which("codex")` finds the already-installed binary.
    """
    async def generate() -> AsyncGenerator[str, None]:
        # ------------------------------------------------------------------ #
        # Step 1 – ensure codex CLI is installed                              #
        # ------------------------------------------------------------------ #
        _ensure_node_on_path()
        if not _is_codex_installed():
            print(f"[codex-setup] codex CLI not found, installing {CODEX_NPM_PACKAGE}")
            yield f"data: {json.dumps({'type': 'installing', 'package': CODEX_NPM_PACKAGE})}\n\n"
            try:
                npm_proc = await asyncio.create_subprocess_exec(
                    "npm", "install", "-g", CODEX_NPM_PACKAGE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    async def _drain_npm():
                        while True:
                            line = await npm_proc.stdout.readline()
                            if not line:
                                break
                            text = line.decode("utf-8", errors="replace").rstrip()
                            print(f"[codex-setup] npm> {text}")
                            return text  # caller collects via async for

                    # Stream npm output line by line with overall timeout
                    deadline = time.monotonic() + _NPM_INSTALL_TIMEOUT_SECONDS
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            print(
                                f"[codex-setup] npm install timed out after "
                                f"{_NPM_INSTALL_TIMEOUT_SECONDS}s, killing pid={npm_proc.pid}"
                            )
                            npm_proc.kill()
                            yield f"data: {json.dumps({'type': 'error', 'error': 'npm install timed out'})}\n\n"
                            return
                        try:
                            line = await asyncio.wait_for(
                                npm_proc.stdout.readline(),
                                timeout=min(remaining, _SSE_HEARTBEAT_INTERVAL),
                            )
                        except asyncio.TimeoutError:
                            yield ": heartbeat\n\n"
                            continue
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace").rstrip()
                        print(f"[codex-setup] npm> {text}")
                        yield f"data: {json.dumps({'type': 'install_log', 'line': text})}\n\n"

                    await npm_proc.wait()
                except Exception:
                    if npm_proc.returncode is None:
                        npm_proc.kill()
                    raise

                if npm_proc.returncode != 0:
                    print(
                        f"[codex-setup] npm install failed (exit code "
                        f"{npm_proc.returncode}) for package {CODEX_NPM_PACKAGE}"
                    )
                    yield f"data: {json.dumps({'type': 'error', 'error': f'npm install failed (exit code {npm_proc.returncode})'})}\n\n"
                    return

                if not _is_codex_installed():
                    print(
                        "[codex-setup] codex CLI not found in PATH after npm install; "
                        f"PATH={os.environ.get('PATH', '')!r}"
                    )
                    yield f"data: {json.dumps({'type': 'error', 'error': 'codex CLI not found in PATH after npm install'})}\n\n"
                    return

                print("[codex-setup] codex CLI installed successfully")
                yield f"data: {json.dumps({'type': 'install_log', 'line': 'codex CLI installed successfully'})}\n\n"

            except FileNotFoundError as e:
                print(
                    f"[codex-setup] npm executable not found: {e}; "
                    f"PATH={os.environ.get('PATH', '')!r}"
                )
                yield f"data: {json.dumps({'type': 'error', 'error': 'npm not found — cannot install codex CLI'})}\n\n"
                return
            except Exception as e:
                import traceback
                print(f"[codex-setup] unexpected error during npm install: {e}")
                traceback.print_exc()
                yield f"data: {json.dumps({'type': 'error', 'error': f'npm install failed: {e}'})}\n\n"
                return
        else:
            print("[codex-setup] codex CLI already installed, skipping npm install")

        # ------------------------------------------------------------------ #
        # Step 2 – run codex login --device-auth via PTY                      #
        # ------------------------------------------------------------------ #
        cli_path = _codex_cli_path()
        print(f"[codex-setup] spawning: {cli_path} login --device-auth")

        queue: asyncio.Queue = asyncio.Queue()
        process: Optional[asyncio.subprocess.Process] = None
        master_fd: Optional[int] = None

        def _pty_reader_thread(fd: int, loop: asyncio.AbstractEventLoop) -> None:
            """Blocking read loop in a plain OS thread — immune to asyncio lifecycle issues."""
            buf = b""
            try:
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        text = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                        stripped = _strip_ansi(text).strip()
                        if stripped:
                            print(f"[codex-setup] PTY> {stripped[:200]}")
                        loop.call_soon_threadsafe(queue.put_nowait, ("stdout", text))
            except OSError as e:
                print(f"[codex-setup] PTY read ended: {e}")
            if buf:
                tail = buf.decode("utf-8", errors="replace").rstrip("\r")
                if tail:
                    loop.call_soon_threadsafe(queue.put_nowait, ("stdout", tail))
            print("[codex-setup] PTY reader thread exiting")

        try:
            env = os.environ.copy()
            use_pty = _pty_wanted()
            print(f"[codex-setup] spawning login (use_pty={use_pty})")

            if use_pty:
                master_fd, slave_fd = _pty.openpty()
                try:
                    _set_pty_winsize(slave_fd)
                    process = await asyncio.create_subprocess_exec(
                        cli_path, "login", "--device-auth",
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        env=env,
                    )
                except BaseException:
                    for fd in (slave_fd, master_fd):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    master_fd = None
                    raise
                os.close(slave_fd)

                threading.Thread(
                    target=_pty_reader_thread,
                    args=(master_fd, asyncio.get_event_loop()),
                    daemon=True,
                    name="codex-pty-reader",
                ).start()

                async def _wait_done_pty():
                    rc = await process.wait()
                    print(f"[codex-setup] process exited (rc={rc})")
                    await queue.put(("done", rc))

                asyncio.create_task(_wait_done_pty())

            else:
                process = await asyncio.create_subprocess_exec(
                    cli_path, "login", "--device-auth",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    env=env,
                )

                async def _read_pipe(stream, kind: str) -> None:
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace").rstrip("\n")
                        await queue.put((kind, text))

                asyncio.create_task(_read_pipe(process.stdout, "stdout"))
                asyncio.create_task(_read_pipe(process.stderr, "stderr"))

                async def _wait_done_pipe():
                    rc = await process.wait()
                    print(f"[codex-setup] process exited (rc={rc})")
                    await queue.put(("done", rc))

                asyncio.create_task(_wait_done_pipe())

            deadline = time.monotonic() + CODEX_SETUP_TIMEOUT_SECONDS

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    if time.monotonic() >= deadline:
                        if process and process.returncode is None:
                            print("[codex-setup] timed out; killing process")
                            process.kill()
                        yield f"data: {json.dumps({'type': 'error', 'error': 'Login timed out'})}\n\n"
                        break
                    yield ": heartbeat\n\n"
                    continue

                kind, value = item
                if kind == "done":
                    if value == 0:
                        creds = _read_codex_credentials()
                        if creds and creds.get("token"):
                            token = creds["token"]
                            refresh = creds.get("refresh")
                            email = creds.get("email")
                            for key in _TOKEN_ENV_KEYS:
                                os.environ[key] = token

                            # Persist to whichever backend is active (AGENT_NAME),
                            # matching the dispatch the rest of the API uses.
                            active_backend = os.getenv("AGENT_NAME", "openclaw")
                            if active_backend == "hermes":
                                # Write codex into hermes's canonical auth structure
                                # (providers.tokens + active_provider) and set it as
                                # the default profile's primary model, then drop the
                                # codex CLI copy so it can't refresh-race the token.
                                if await asyncio.to_thread(_persist_token_to_hermes_auth, token, refresh):
                                    await asyncio.to_thread(_set_hermes_primary_model)
                                    await asyncio.to_thread(_remove_codex_cli_auth)
                                    # The running gateway caches its credential pool
                                    # at startup — restart so it picks up the new
                                    # codex token without a manual step.
                                    try:
                                        from api.channels.routes import _run_hermes_sh
                                        rc, _out = await _run_hermes_sh("restart", timeout_s=90.0)
                                        print(f"[codex-setup] hermes gateway restart rc={rc}")
                                    except Exception as e:  # noqa: BLE001
                                        print(f"[codex-setup] hermes gateway restart skipped ({e})")
                            elif active_backend == "openclaw":
                                # openclaw consumes codex as a model provider, so it
                                # needs the credential mirrored into its own gateway
                                # store. These writes are openclaw-shaped and belong
                                # to openclaw alone — hence the explicit branch.
                                _persist_token_to_env_files(token)
                                print(f"[codex-setup] token persisted (len={len(token)})")
                                if email:
                                    _upsert_openclaw_config(email)
                                    expires_ms = creds.get("expires_ms")
                                    if refresh and expires_ms:
                                        _upsert_agent_auth_profile(email, token, refresh, expires_ms)
                                    else:
                                        print(
                                            "[codex-setup] missing refresh/expires; "
                                            "skipping agent auth-profiles.json upsert"
                                        )
                                else:
                                    print("[codex-setup] no email claim found, skipping openclaw.json upsert")
                            else:
                                # codex / claude_code / antigravity / anything else:
                                # the codex CLI's own ~/.codex/auth.json is the login,
                                # and the token is mirrored only into the ACTIVE agent's
                                # manifest env_file. Never write another agent's config
                                # — that previously fabricated ~/.openclaw/ on boxes
                                # where openclaw was not even installed.
                                _persist_token_to_env_files(token)
                                print(
                                    f"[codex-setup] token persisted (len={len(token)}); "
                                    f"backend={active_backend} — no foreign-agent credential writes"
                                )
                        else:
                            print("[codex-setup] login succeeded but no token found in credential files")
                    yield f"data: {json.dumps({'type': 'done', 'returncode': value})}\n\n"
                    break
                yield f"data: {json.dumps({'type': kind, 'line': value})}\n\n"

        except FileNotFoundError:
            print(
                f"[codex-setup] codex CLI not found: {cli_path}; "
                f"PATH={os.environ.get('PATH', '')!r}"
            )
            yield f"data: {json.dumps({'type': 'error', 'error': f'codex CLI not found: {cli_path}'})}\n\n"
        except Exception as e:
            import traceback
            print(f"[codex-setup] unexpected error during login: {type(e).__name__}: {e}")
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            print("[codex-setup] SSE generator finished")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
