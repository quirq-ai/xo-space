"""
XO Space API Server
FastAPI server that interfaces with local Claude Code CLI.
"""

import asyncio
import os
import json
import datetime
import subprocess
import sys
import uuid
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import dotenv_values, load_dotenv
import httpx
import uvicorn
from config.models.claude_code import ClaudeCodeClient
from config.models.codex import CodexCodeClient
from utils.local_port import LocalPortsUnavailableError, resolve_server_port

# Load environment variables. Keys already exported by the shell (or by
# docker -e / compose) are recorded first: they outrank every file below,
# exactly as install.sh orders them.
_shell_env_keys = frozenset(os.environ)
load_dotenv()


def _load_storage_roots() -> None:
    """Apply ``<state root>/roots.env`` — the storage roots the Setup tab writes.

    Precedence, mirroring install.sh: shell/container env > roots.env >
    the checkout's .env. Loading it here is what makes the Setup tab's XO
    root real for every reader (all of which resolve through
    ``project_layout.xo_projects_root()``) after a restart, instead of only
    when the Docker installer is re-run.

    The file is anchored to the state root we can see right now; if it
    relocates the state root, runtime.env and secrets are then read from the
    new location.
    """
    anchor = Path(
        (os.getenv("QUIRQ_STATE_ROOT", "") or "").strip() or Path.home() / ".quirq"
    ).expanduser()
    try:
        values = dotenv_values(anchor / "roots.env")
    except OSError:
        return
    for key in ("XO_PROJECTS_ROOT", "QUIRQ_STATE_ROOT"):
        value = (values.get(key) or "").strip()
        if value and key not in _shell_env_keys:
            os.environ[key] = value


_load_storage_roots()
_quirq_state_root = Path(
    (os.getenv("QUIRQ_STATE_ROOT", "") or "").strip() or Path.home() / ".quirq"
).expanduser()
_quirq_runtime_file = (
    (os.getenv("QUIRQ_RUNTIME_FILE", "") or "").strip()
    or str(_quirq_state_root / "runtime.env")
)
load_dotenv(_quirq_runtime_file, override=True)
_quirq_secrets_file = (os.getenv("QUIRQ_SECRETS_FILE", "") or "").strip()
if _quirq_secrets_file:
    load_dotenv(_quirq_secrets_file, override=True)

from routers.auth.auth import (
    XO_API_KEY,
    consume_auth_flow,
    get_auth_token,
    get_auth_state,
    router as auth_router,
)
from routers.auth.claude_setup_token import router as claude_setup_token_router
from routers.auth.codex_setup import router as codex_setup_router
from routers.cowork_agent.legacy.openclaw_usage import router as openclaw_usage_router
from routers.status.models import router as models_router
from routers.status.channels import router as channels_router
from routers.status.providers import router as providers_router
try:
    from services.usage_sync import start_usage_sync_scheduler
except Exception as _usage_import_err:
    start_usage_sync_scheduler = None
    print(f"⚠️ Usage sync module failed to load (non-fatal): {_usage_import_err}")


# =============================================================================
# Configuration
# =============================================================================

# External Chat API base URL (xo-swarm-api or similar)
CHAT_API_BASE_URL = os.getenv("CHAT_API_BASE_URL", "https://api-swarm-beta.xo.builders")
STAGE = (os.getenv("STAGE", "beta") or "beta").strip().lower()
IS_LOCAL_STAGE = STAGE == "local"

# Resolve runtime paths using stage-aware defaults.
def _resolve_cli_path(env_var: str, default_cmd: str, beta_default: str) -> str:
    configured = (os.getenv(env_var, "") or "").strip()
    if configured:
        if IS_LOCAL_STAGE and os.path.isabs(configured) and not os.path.exists(configured):
            print(
                f"⚠️ {env_var} points to missing path: {configured}. "
                f"Falling back to '{default_cmd}' from PATH."
            )
        elif IS_LOCAL_STAGE:
            return configured
        else:
            return configured

    if IS_LOCAL_STAGE:
        discovered = shutil.which(default_cmd)
        if discovered:
            return discovered
        return default_cmd

    return beta_default


def _resolve_workspace_root() -> str:
    configured = (os.getenv("AI_WORKSPACE_ROOT", "") or "").strip()
    if configured:
        if IS_LOCAL_STAGE and not os.path.isdir(configured):
            print(
                f"⚠️ AI_WORKSPACE_ROOT does not exist: {configured}. "
                "Falling back to project directory."
            )
        else:
            return configured

    if IS_LOCAL_STAGE:
        return str(Path(__file__).resolve().parent)

    return "/home/coder"


# Claude Code CLI path
CLAUDE_CLI_PATH = _resolve_cli_path(
    env_var="CLAUDE_CLI_PATH",
    default_cmd="claude",
    beta_default="/home/coder/.local/bin/claude",
)
# Codex CLI path
CODEX_CLI_PATH = _resolve_cli_path(
    env_var="CODEX_CLI_PATH",
    default_cmd="codex",
    beta_default="codex",
)


AI_WORKSPACE_ROOT = _resolve_workspace_root()

if IS_LOCAL_STAGE and not os.path.isdir(AI_WORKSPACE_ROOT):
    print(
        f"⚠️ AI_WORKSPACE_ROOT does not exist: {AI_WORKSPACE_ROOT}. "
        "Falling back to project directory (local stage)."
    )
    AI_WORKSPACE_ROOT = str(Path(__file__).resolve().parent)

# HTTP client timeout settings
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
STARTUP_WARMUP_ENABLED = os.getenv("STARTUP_WARMUP_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_STARTUP_WARMUP_URL_CONFIGURED = bool(
    (os.getenv("STARTUP_WARMUP_URL", "") or "").strip()
)
STARTUP_WARMUP_URL = (
    os.getenv(
        "STARTUP_WARMUP_URL",
        f"http://localhost:{os.getenv('PORT', '5002')}",
    )
    .strip()
    .rstrip("/")
    + "/"
)
STARTUP_WARMUP_DELAY_SECONDS = float(os.getenv("STARTUP_WARMUP_DELAY_SECONDS", "10"))

# Claude Code timeout (in seconds) - allow longer for complex queries
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))  # 5 minutes default
# Codex timeout (in seconds)
CODEX_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", str(CLAUDE_TIMEOUT)))

# Runtime provider switch: claude | codex
AI_PROVIDER = os.getenv("AI_PROVIDER", "claude").strip().lower()
CLAUDE_PERMISSION_MODE = os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()

def _claude_auth_debug_snapshot() -> Dict[str, Any]:
    """
    Return a safe, non-secret snapshot of Claude auth-related environment state.
    """
    claude_oauth_token = (os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "") or "").strip()
    anthropic_api_key = (os.getenv("ANTHROPIC_API_KEY", "") or "").strip()
    anthropic_oauth_api_key = (os.getenv("ANTHROPIC_OAUTH_API_KEY", "") or "").strip()

    has_claude_oauth = bool(claude_oauth_token)
    has_anthropic_api = bool(anthropic_api_key)
    has_anthropic_oauth_api = bool(anthropic_oauth_api_key)

    present_sources = []
    if has_claude_oauth:
        present_sources.append("CLAUDE_CODE_OAUTH_TOKEN")
    if has_anthropic_oauth_api:
        present_sources.append("ANTHROPIC_OAUTH_API_KEY")
    if has_anthropic_api:
        present_sources.append("ANTHROPIC_API_KEY")

    if has_claude_oauth:
        likely_source = "CLAUDE_CODE_OAUTH_TOKEN"
    elif has_anthropic_oauth_api:
        likely_source = "ANTHROPIC_OAUTH_API_KEY"
    elif has_anthropic_api:
        likely_source = "ANTHROPIC_API_KEY"
    else:
        likely_source = "none_detected"

    return {
        "env_presence": {
            "CLAUDE_CODE_OAUTH_TOKEN": has_claude_oauth,
            "ANTHROPIC_OAUTH_API_KEY": has_anthropic_oauth_api,
            "ANTHROPIC_API_KEY": has_anthropic_api,
        },
        "present_sources": present_sources,
        "multiple_sources_set": len(present_sources) > 1,
        "likely_auth_source": likely_source,
    }


# =============================================================================
# Session Management
# =============================================================================


session_store: Dict[str, str] = {}


def get_session_id(project_id: str) -> Optional[str]:
    """Get existing session ID for a project."""
    return session_store.get(project_id)


def create_session_id(project_id: str) -> str:
    """Create and store a new session ID for a project."""
    session_id = str(uuid.uuid4())
    session_store[project_id] = session_id
    print(f"📝 Created session {session_id} for project {project_id}")
    return session_id


def clear_session(project_id: str) -> None:
    """Clear session for a project."""
    if project_id in session_store:
        del session_store[project_id]
        print(f"🗑️ Cleared session for project {project_id}")


class AskQuestionRequest(BaseModel):
    """Request model for ask_question endpoints"""
    project_name: str
    question: str
    user_id: Optional[str] = "default_user"
    message_type: Optional[str] = "@xo"
    agent_type: Optional[str] = None


# =============================================================================
# External Chat API Client
# =============================================================================

class ChatAPIClient:
    """Client for external Chat API endpoints."""

    def __init__(self, base_url: str = CHAT_API_BASE_URL):
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        token = get_auth_token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def push_message(
        self,
        project_id: str,
        user_id: str,
        message: str,
        message_type: str = "@xo"
    ) -> Optional[Dict[str, Any]]:
        """Push a message to the chat storage via external API."""
        url = f"{self.base_url}/chat/add_message"
        payload = {
            "project_id": project_id,
            "user_id": user_id,
            "message": message,
            "type": message_type
        }

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.post(url, json=payload, headers=self._headers())
                if response.status_code == 200:
                    print(f"✅ Pushed message: project={project_id}, type={message_type}")
                    return response.json()
                else:
                    print(f"⚠️ Failed to push message: {response.status_code} - {response.text}")
                    return None
        except Exception as e:
            print(f"⚠️ Chat API error: {str(e)}")
            return None

    async def fetch_messages(
        self,
        project_id: str,
        limit: int = 50
    ) -> Optional[list]:
        """Fetch messages from the chat storage."""
        url = f"{self.base_url}/chat/get_messages"
        params = {"project_id": project_id, "limit": limit}

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.get(url, params=params, headers=self._headers())
                if response.status_code == 200:
                    data = response.json()
                    messages = data.get("messages", [])
                    print(f"✅ Fetched {len(messages)} messages: project={project_id}")
                    return messages
                else:
                    print(f"⚠️ Failed to fetch messages: {response.status_code} - {response.text}")
                    return None
        except Exception as e:
            print(f"⚠️ Chat API error: {str(e)}")
            return None

    async def get_message_count(self, project_id: str) -> int:
        """Get message count for a project."""
        messages = await self.fetch_messages(project_id, limit=100)
        return len(messages) if messages else 0


# Global chat client
chat_client = ChatAPIClient()


if AI_PROVIDER == "codex":
    ai_client = CodexCodeClient(
        cli_path=CODEX_CLI_PATH,
        timeout_seconds=CODEX_TIMEOUT,
    )
else:
    ai_client = ClaudeCodeClient(
        cli_path=CLAUDE_CLI_PATH,
        timeout_seconds=CLAUDE_TIMEOUT,
        permission_mode=CLAUDE_PERMISSION_MODE,
        working_directory=AI_WORKSPACE_ROOT,
        allowed_directories=[AI_WORKSPACE_ROOT],
    )


# =============================================================================
# Helper Functions
# =============================================================================

async def save_chat_messages(
    project_id: str,
    user_id: str,
    user_message: str,
    agent_response: str,
    message_type: str = "@xo"
) -> None:
    """Save both user message and agent response to chat storage."""
    await chat_client.push_message(
        project_id=project_id,
        user_id=user_id,
        message=user_message,
        message_type=message_type
    )
    await chat_client.push_message(
        project_id=project_id,
        user_id=user_id,
        message=agent_response,
        message_type="agent"
    )


async def startup_warmup_request() -> None:
    """Trigger a local warmup request after app startup (non-fatal)."""
    if not STARTUP_WARMUP_ENABLED:
        print("   Startup warmup: disabled")
        return

    await asyncio.sleep(max(0.0, STARTUP_WARMUP_DELAY_SECONDS))
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(STARTUP_WARMUP_URL)
        print(f"✅ Startup warmup request succeeded: {STARTUP_WARMUP_URL} ({response.status_code})")
    except Exception as e:
        print(f"⚠️ Startup warmup request failed (non-fatal): {STARTUP_WARMUP_URL} - {e}")


# =============================================================================
# FastAPI Application
# =============================================================================

def _session_telemetry_daemons(action: str) -> None:
    """Start or stop every session-telemetry provider's ingestion daemon.

    Core never names a telemetry backend. Providers are discovered through the
    capability loader, and each may optionally expose ``start_daemon`` /
    ``stop_daemon``; one that does not is skipped, exactly as with
    ``runtime_mounts`` in scripts/list_runtime_mounts.py.

    Deliberately not gated by QUIRQ_SKIP_BOOT_INSTALL: starting a process that
    pip already installed is not installing software.

    Non-fatal in both directions — a provider that fails to start must not
    block the boot, and one that fails to stop must not hang the shutdown.
    """

    try:
        from services.cowork_agent.adapters.loader import (
            list_capability_providers,
            try_load_capability,
        )
    except Exception as exc:
        print(f"⚠️ session telemetry daemon {action} skipped (non-fatal): {exc}")
        return

    for provider in list_capability_providers("session_telemetry"):
        module = try_load_capability("session_telemetry", agent=provider)
        hook = getattr(module, f"{action}_daemon", None) if module else None
        if not callable(hook):
            continue
        try:
            hook()
        except Exception as exc:
            print(
                f"⚠️ {provider} telemetry daemon {action} failed (non-fatal): {exc}"
            )


def _boot_installs_disabled() -> bool:
    """Whether the boot-time system-dependency installers are switched off.

    Set QUIRQ_SKIP_BOOT_INSTALL=1 for deployments that must not download or
    install anything beyond requirements.txt — the Docker-free native runner
    does, because it runs on a user's own machine rather than a disposable
    container. Defaults to off, so container and Coder boots are unchanged.

    Callers still work when this is on: both installers are already non-fatal,
    and every dependency they provide degrades a feature rather than breaking
    startup.
    """
    return (os.getenv("QUIRQ_SKIP_BOOT_INSTALL", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _run_agent_setup() -> None:
    """Run config/agents/<AGENT_NAME>/setup.sh once at boot.

    Dispatches by the AGENT_NAME env var (e.g. AGENT_NAME=openclaw → runs
    config/agents/openclaw/setup.sh). Idempotent — re-runs on every boot
    but each step (apt install, node install, openclaw CLI install,
    gateway start) is gated on its own "already installed?" check.
    Non-fatal: a failure here logs and returns so the API still comes
    up for debugging.
    """
    if _boot_installs_disabled():
        print("🔧 QUIRQ_SKIP_BOOT_INSTALL set — skipping agent bootstrap")
        return

    agent = (os.getenv("AGENT_NAME", "") or "").strip()
    if not agent:
        print("🔧 AGENT_NAME unset — skipping agent bootstrap")
        return

    repo_root = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(repo_root, "config", "agents", agent, "setup.sh")
    if not os.path.isfile(script):
        print(f"⚠️ No setup.sh for AGENT_NAME={agent} (expected at {script}) — skipping")
        return

    print(f"🔧 Running agent setup for AGENT_NAME={agent}...")
    try:
        os.chmod(script, 0o755)
    except OSError:
        pass

    try:
        # 15-minute ceiling covers a cold first-time install (apt + Node + npm
        # + OpenClaw CLI). Steady-state re-runs finish in seconds.
        result = subprocess.run(
            ["bash", script],
            cwd=repo_root,
            check=False,
            timeout=900,
        )
        if result.returncode == 0:
            print(f"✅ Agent setup ({agent}) completed")
        else:
            print(f"⚠️ Agent setup ({agent}) exited with code {result.returncode} (non-fatal — server will still start)")
    except subprocess.TimeoutExpired:
        print(f"⚠️ Agent setup ({agent}) timed out after 15min (non-fatal)")
    except Exception as e:
        print(f"⚠️ Agent setup ({agent}) failed (non-fatal): {e}")


def _install_shared_deps() -> None:
    """Install shared, agent-agnostic system deps once at boot.

    Delegates to scripts/install_shared_deps.sh, which ensures rclone
    (gdrive/onedrive connectors), gh (xo-projects-sync backup repos), and
    gnupg (xo-projects-sync encryption) are present. Moving these here means
    workspace templates no longer each have to install them. The script is
    idempotent (skips deps already on PATH) and no-ops on non-apt hosts.
    Non-fatal: a failure logs and returns so the API still comes up.
    """
    if _boot_installs_disabled():
        print("🔧 QUIRQ_SKIP_BOOT_INSTALL set — skipping shared dep install")
        return

    repo_root = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(repo_root, "scripts", "install_shared_deps.sh")
    if not os.path.isfile(script):
        print(f"⚠️ No shared-deps script (expected at {script}) — skipping")
        return

    # Invoke via `bash <script>` (below), which does not require the script's
    # executable bit — so we deliberately do NOT chmod it. chmod-ing on every
    # boot would flip the tracked mode (644 → 755) and surface the file as a
    # spurious git change in the workspace.
    print("🔧 Ensuring shared system deps (rclone, gh, gnupg)...")
    try:
        # 10-minute ceiling covers a cold first-time install (rclone download +
        # gh .deb + apt). Steady-state re-runs finish in seconds.
        #
        # The server is typically launched as venv/bin/python WITHOUT venv
        # activation, so venv/bin is not on PATH — but console scripts pip
        # installs for us (e.g. `argus`) live exactly there, next to the
        # interpreter. Prepend it so the script sees them.
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join(
            [os.path.dirname(sys.executable), env.get("PATH", "")])
        result = subprocess.run(
            ["bash", script],
            cwd=repo_root,
            check=False,
            timeout=600,
            env=env,
        )
        if result.returncode == 0:
            print("✅ Shared dep check completed")
        else:
            print(f"⚠️ Shared dep install exited with code {result.returncode} (non-fatal — server will still start)")
    except subprocess.TimeoutExpired:
        print("⚠️ Shared dep install timed out after 10min (non-fatal)")
    except Exception as e:
        print(f"⚠️ Shared dep install failed (non-fatal): {e}")


def _write_install_pointer() -> None:
    """Record where this install lives so local clients (the Claude Code
    plugin, support tooling) can find it without scanning the filesystem.

    One fixed path, dynamic contents: ~/.config/quirq/install.json (XDG
    honoured), rewritten on every boot because it is a last-known-location
    hint, not configuration — port fallbacks and relocated roots must show
    up here. Consumers verify the recorded paths still exist before
    trusting them, so a stale file after an uninstall is harmless.
    """
    try:
        from services.cowork_agent.local_state import quirq_state_dir
        from services.cowork_agent.project_layout import xo_projects_root

        config_home = Path(
            os.getenv("XDG_CONFIG_HOME", "").strip() or Path.home() / ".config"
        )
        pointer_dir = config_home / "quirq"
        pointer_dir.mkdir(parents=True, exist_ok=True)
        pointer = {
            "repo_dir": str(Path(__file__).resolve().parent),
            "projects_root": str(xo_projects_root()),
            "state_root": str(quirq_state_dir()),
            "host": os.getenv("HOST", "127.0.0.1"),
            "port": int(os.getenv("PORT", "5002")),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        (pointer_dir / "install.json").write_text(json.dumps(pointer, indent=2) + "\n")
    except Exception as e:
        print(f"⚠️ Could not write install pointer (non-fatal): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Written first: the pointer must exist even if a later boot step fails,
    # so a half-started install is still discoverable.
    _write_install_pointer()

    # Bootstrap the agent runtime (OpenClaw, etc.) before serving traffic.
    # Done synchronously so the API doesn't accept requests until the
    # agent it's meant to drive is installed and configured.
    _run_agent_setup()

    # Install shared, agent-agnostic system deps (rclone, gh, gnupg) before the
    # connector + xo-projects-sync checks below rely on them being on PATH.
    _install_shared_deps()

    # Session telemetry ingestion. Tied to this process's lifetime so the
    # Sessions tab has fresh data whenever Quirq is up, and nothing is left
    # running once it is down.
    _session_telemetry_daemons("start")

    print("🚀 Starting XO Space API Server...")
    print(f"   Chat API: {CHAT_API_BASE_URL}")
    _tok = get_auth_token()
    _src = get_auth_state().get("token_source", "none")
    print(f"   Chat API auth: {'enabled (' + _src + ')' if _tok else 'not set'}")
    print(f"   Stage: {STAGE}")
    print(f"   AI Provider: {AI_PROVIDER}")
    print(f"   Claude CLI: {CLAUDE_CLI_PATH} (timeout={CLAUDE_TIMEOUT}s)")
    print(f"   Claude Permission Mode: {CLAUDE_PERMISSION_MODE}")
    print(f"   AI Workspace Root: {AI_WORKSPACE_ROOT}")
    print(f"   Codex CLI: {CODEX_CLI_PATH} (timeout={CODEX_TIMEOUT}s)")
    print(f"   Startup warmup: {'enabled' if STARTUP_WARMUP_ENABLED else 'disabled'} ({STARTUP_WARMUP_URL})")
    print("   Skills: .claude/skills (Claude-native)")
    print("   Skills: .agents/skills + AGENTS.md (Codex-native)")
    startup_auth_session_id = os.getenv("XO_AUTH_SESSION_ID", "").strip()
    startup_poll_token = os.getenv("XO_POLL_TOKEN", "").strip()
    if XO_API_KEY:
        print("   XO auth: using XO_API_KEY (no consume)")
    elif startup_auth_session_id and startup_poll_token:
        print("   XO startup consume: attempting token consume")
        try:
            await consume_auth_flow(
                auth_session_id=startup_auth_session_id,
                poll_token=startup_poll_token,
            )
            print("✅ XO startup consume succeeded")
        except HTTPException as e:
            print(f"⚠️ XO startup consume failed: status={e.status_code}, detail={e.detail}")
        except Exception as e:
            print(f"⚠️ XO startup consume failed unexpectedly: {str(e)}")
    elif startup_auth_session_id or startup_poll_token:
        print(
            "⚠️ XO startup consume skipped: set both XO_AUTH_SESSION_ID and XO_POLL_TOKEN."
        )

    # Start rclone daemon for the gdrive/onedrive connectors (non-fatal if rclone isn't installed)
    try:
        from services.cowork_agent.connectors.gdrive import ensure_rclone_running
        await ensure_rclone_running()
        print("   rclone daemon: started for gdrive/onedrive connectors")
    except Exception as exc:
        print(f"⚠️ rclone startup skipped (non-fatal): {exc}")

    # Check gnupg availability (required by xo-projects-sync; non-fatal — does NOT install).
    # Mirrors the rclone pattern: surface missing system deps at boot so they don't
    # only appear as confused 500s on the first /setup attempt.
    try:
        from services.cowork_agent.xo_projects_sync.crypto import check_gpg_available
        check_gpg_available()
        print("   gnupg: available (xo-projects-sync ready)")
    except Exception as exc:
        print(f"⚠️ gnupg not available — xo-projects-sync /setup will return 500 until installed: {exc}")

    # Install bundled skills into Claude Code and OpenClaw skill dirs (non-fatal)
    try:
        from services.cowork_agent.skill_installer import install_xo_skills
        install_xo_skills()
    except Exception as exc:
        print(f"⚠️ Skill install failed (non-fatal): {exc}")

    # Install the active agent's declared boot-time skills — catalog names listed
    # under "startup_skills" in config/agents/<AGENT_NAME>/settings.json. Agents
    # that declare none install nothing. Backgrounded because these shell out to
    # the network (npx/git); boot must not wait on them.
    _startup_skills_task = None
    try:
        from services.cowork_agent.skill_catalog import install_startup_skills
        _startup_skills_task = asyncio.create_task(install_startup_skills())
        print("   Startup skills: background install scheduled")
    except Exception as exc:
        print(f"⚠️ Startup skill install failed to schedule (non-fatal): {exc}")

    # Write ~/xo-projects/.xo/xo.json (static defaults) and seed live status
    # in the background. The dispatcher inside seed_agent_status() picks the
    # right adapter for the current AGENT_NAME (no-op for agents without a
    # status source). Non-fatal on every failure path.
    _xo_status_task = None
    try:
        from services.xo_manifest import write_static_manifest, seed_agent_status
        await write_static_manifest()
        _xo_status_task = asyncio.create_task(seed_agent_status())
        print("   xo.json: status seed scheduled (background)")
    except Exception as exc:
        print(f"⚠️ xo.json: setup failed (non-fatal): {exc}")

    # Start daily usage sync background task
    _sync_task = None
    _warmup_task = None
    _watcher_task = None
    if start_usage_sync_scheduler:
        try:
            _sync_task = asyncio.create_task(start_usage_sync_scheduler())
            print("   Usage sync: background task started")
        except Exception as e:
            print(f"⚠️ Usage sync failed to start (non-fatal): {e}")

    # Visualizer watcher — materialises portable project metadata from the
    # active runtime's native session store. Non-fatal: BFF endpoints keep
    # serving whatever is already on disk.
    _watcher_enabled = (
        os.getenv("QUIRQ_WATCHER_ENABLED", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if _watcher_enabled:
        try:
            from services.cowork_agent.visualizer.watcher import start_watcher
            _watcher_task = asyncio.create_task(start_watcher())
            print("   Watcher: background task started")
        except Exception as e:
            print(f"⚠️ Watcher failed to start (non-fatal): {e}")
    else:
        print("   Watcher: disabled by runtime configuration")

    _warmup_task = asyncio.create_task(startup_warmup_request())

    yield

    # Stop ingestion first: the daemon is an external process, so leaving it
    # behind outlives this server, unlike the asyncio tasks cancelled below.
    _session_telemetry_daemons("stop")

    # Cleanup background task
    if _warmup_task and not _warmup_task.done():
        _warmup_task.cancel()
        try:
            await _warmup_task
        except asyncio.CancelledError:
            pass

    if _sync_task:
        _sync_task.cancel()
        try:
            await _sync_task
        except asyncio.CancelledError:
            pass

    if _watcher_task:
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass

    if _startup_skills_task and not _startup_skills_task.done():
        _startup_skills_task.cancel()
        try:
            await _startup_skills_task
        except asyncio.CancelledError:
            pass

    if _xo_status_task and not _xo_status_task.done():
        _xo_status_task.cancel()
        try:
            await _xo_status_task
        except asyncio.CancelledError:
            pass
    print("👋 Shutting down XO Space API Server...")


app = FastAPI(
    title="XO Space API",
    description="XO Space API - local control plane brokering chat to coding-agent runtimes",
    version="1.0.0",
    lifespan=lifespan
)

_CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(claude_setup_token_router)
app.include_router(codex_setup_router)
app.include_router(openclaw_usage_router)
app.include_router(models_router)
app.include_router(channels_router)
app.include_router(providers_router)

# Cowork Agent API (migrated from bridge/) — serves the xo-cowork frontend.
from routers.cowork_agent import all_routers as cowork_agent_routers
for _r in cowork_agent_routers:
    app.include_router(_r)

# Space: local workspace knowledge graph (static UI + server control widget).
from routers.space import router as space_router, mount_space
from routers.xo_data import router as xo_data_router
app.include_router(space_router)
app.include_router(xo_data_router)
mount_space(app)

# =============================================================================
# Endpoints
# =============================================================================

@app.get("/")
async def root(request: Request):
    """Root endpoint.

    Browsers (``Accept: text/html``) are redirected to the Space UI at
    ``/space/``; API clients and health checks still receive the JSON status.
    This lets the workspace root URL land on the UI when the API is proxied at
    the port root (e.g. a Coder subdomain app whose base is the port itself,
    not ``/space``) — the UI then talks to the API same-origin.
    """
    if "text/html" in request.headers.get("accept", ""):
        # Carry the query string across the redirect (still percent-encoded),
        # so links like /?project=x land on /space/?project=x.
        query = request.url.query
        url = f"/space/?{query}" if query else "/space/"
        return RedirectResponse(url=url, status_code=307)
    return {"status": "XO Space API running"}


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.datetime.now().isoformat(),
        "chat_api_url": CHAT_API_BASE_URL,
        "stage": STAGE,
        "auth": get_auth_state(),
        "ai_provider": AI_PROVIDER,
        "claude_cli": CLAUDE_CLI_PATH,
        "codex_cli": CODEX_CLI_PATH,
        "active_sessions": len(session_store)
    }


@app.get("/debug/ai-auth")
async def debug_ai_auth():
    """Debug endpoint to inspect effective AI auth configuration (safe output)."""
    snapshot = _claude_auth_debug_snapshot()
    return {
        "stage": STAGE,
        "ai_provider": AI_PROVIDER,
        "claude_cli": CLAUDE_CLI_PATH,
        "claude_permission_mode": CLAUDE_PERMISSION_MODE,
        "ai_workspace_root": AI_WORKSPACE_ROOT,
        "auth_debug": snapshot,
        "note": (
            "Secrets are never returned. This is a best-effort hint based on server env; "
            "CLI internals may apply their own precedence."
        ),
    }


@app.get("/sessions")
async def list_sessions():
    """List all active sessions."""
    return {
        "sessions": session_store,
        "count": len(session_store)
    }


@app.delete("/sessions/{project_id}")
async def delete_session(project_id: str):
    """Delete a session for a project."""
    if project_id in session_store:
        clear_session(project_id)
        return {"success": True, "message": f"Session cleared for {project_id}"}
    return {"success": False, "message": f"No session found for {project_id}"}


@app.post("/gateway/restart")
async def gateway_restart():
    """Restart the active agent's gateway via its ``config/agents/<agent>/agent.sh``.

    Resolves the script from the active ``AGENT_NAME`` rather than hardcoding a
    backend; agents without an ``agent.sh`` (e.g. claude_code) return 404.
    """
    import subprocess
    from services.xo_manifest import resolve_agent_name
    agent = resolve_agent_name()
    script = (Path(__file__).resolve().parent / "config" / "agents" / agent / "agent.sh").resolve()
    if not script.exists() or not script.is_file():
        raise HTTPException(status_code=404, detail="Gateway script not found")
    try:
        result = subprocess.run(
            [str(script), "restart"],
            capture_output=True, text=True, timeout=30
        )
        return {
            "status": "restarted" if result.returncode == 0 else "error",
            "output": result.stdout,
            "error": result.stderr if result.returncode != 0 else None
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Restart timed out after 30s"}
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/app/restart")
async def app_restart():
    """Restart the XO Space API app process via cowork-api.sh."""
    import subprocess
    # Timestamped marker: a restart kills every in-flight subprocess (e.g. a
    # pending auth login) — correlate this line with mid-flow failures.
    print(f"[app] restart requested at {datetime.datetime.now().isoformat()} — killing process tree")
    script = (Path(__file__).resolve().parent / "cowork-api.sh").resolve()
    if not script.exists() or not script.is_file():
        raise HTTPException(status_code=404, detail="App restart script not found")
    try:
        subprocess.Popen(
            [str(script), "restart"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(script.parent),
            start_new_session=True,
        )
        return {
            "status": "accepted",
            "message": "Restart triggered in background"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/app/update")
async def app_update():
    """Pull latest code safely via cowork-update.sh in background."""
    import subprocess
    script = (Path(__file__).resolve().parent / "cowork-update.sh").resolve()
    if not script.exists() or not script.is_file():
        raise HTTPException(status_code=404, detail="App update script not found")
    try:
        subprocess.Popen(
            [str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(script.parent),
            start_new_session=True,
        )
        return {
            "status": "accepted",
            "message": "Update triggered in background"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/ask_question")
async def ask_question(data: AskQuestionRequest):
    """
    Send a question to configured AI CLI provider (non-streaming).

    - First message creates a new session
    - Subsequent messages resume the existing session
    """
    try:
        # Check if this is a new session
        session_id = get_session_id(data.project_name)
        is_new = session_id is None

        if is_new:
            # Generate session ID but don't store yet (only store after success)
            session_id = str(uuid.uuid4())
            print(f"🆕 New session for project: {data.project_name} -> {session_id}")
        else:
            print(f"🔄 Resuming session {session_id} for project: {data.project_name}")

        # Send to selected AI provider CLI
        response = await ai_client.ask(
            question=data.question,
            session_id=session_id,
            is_new_session=is_new,
            agent_type=data.agent_type,
        )

        # Store session ID only after successful first call
        if is_new:
            session_store[data.project_name] = session_id
            print(f"📝 Stored session {session_id} for project {data.project_name}")

        # Save to chat storage
        await save_chat_messages(
            project_id=data.project_name,
            user_id=data.user_id,
            user_message=data.question,
            agent_response=response,
            message_type=data.message_type
        )

        return {
            "id": None,
            "message": response,
            "project_id": data.project_name,
            "user_id": data.user_id,
            "session_id": session_id,
            "is_new_session": is_new,
            "timestamp": datetime.datetime.now().isoformat()
        }

    except Exception as e:
        import traceback
        print(f"❌ Error: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail={"error": f"Failed to process question: {str(e)}"}
        )


@app.post("/ask_question_streaming")
async def ask_question_streaming(data: AskQuestionRequest):
    """
    Send a question to configured AI CLI provider (streaming).

    Returns SSE stream of response tokens.

    Streaming returns normalized token events.
    """
    try:
        # Check if this is a new session
        session_id = get_session_id(data.project_name)
        is_new = session_id is None

        if is_new:
            # Generate session ID but don't store yet (only store after success)
            session_id = str(uuid.uuid4())
            print(f"🆕 New streaming session: {data.project_name} -> {session_id}")
        else:
            print(f"🔄 Resuming streaming session: {session_id}")

        # Buffer for full response
        full_response_parts = []
        stream_success = False

        async def generate_stream():
            nonlocal stream_success
            try:
                async for event in ai_client.ask_streaming(
                    question=data.question,
                    session_id=session_id,
                    is_new_session=is_new,
                    agent_type=data.agent_type,
                ):
                    # Handle normalized provider event types
                    event_type = event.get("type", "")

                    if event_type == "token":
                        text = event.get("token", "")
                        if text:
                            full_response_parts.append(text)
                            yield f"data: {json.dumps({'type': 'token', 'token': text})}\n\n"

                    elif event_type == "error":
                        stream_success = False  # Mark as failed
                        yield f"data: {json.dumps({'type': 'error', 'error': event.get('error', 'Unknown error')})}\n\n"

                    elif event_type == "done":
                        # Provider signals completion.
                        stream_success = True
                        yield f"data: {json.dumps({'done': True})}\n\n"

            except Exception as e:
                print(f"❌ Stream generation error: {str(e)}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                stream_success = False  # Ensure we don't store session on error

            finally:
                # Store session ID only after successful first call
                if is_new and stream_success and full_response_parts:
                    session_store[data.project_name] = session_id
                    print(f"📝 Stored session {session_id} for project {data.project_name}")

                # Save full chat only after successful stream completion.
                # This avoids persisting partial responses when streaming errors out.
                final_response = "".join(full_response_parts)
                if stream_success and final_response:
                    await save_chat_messages(
                        project_id=data.project_name,
                        user_id=data.user_id,
                        user_message=data.question,
                        agent_response=final_response,
                        message_type=data.message_type
                    )
                    print(f"✅ Saved response ({len(final_response)} chars)")
                elif final_response and not stream_success:
                    print("⚠️ Skipped chat save due to incomplete streaming response")

        return StreamingResponse(
            generate_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )

    except Exception as e:
        import traceback
        print(f"❌ Error: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail={"error": f"Failed to process question: {str(e)}"}
        )


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    requested_port = int(os.getenv("PORT", "5002"))
    try:
        port = resolve_server_port(
            host=host,
            requested_port=requested_port,
            stage=STAGE,
        )
    except LocalPortsUnavailableError as exc:
        print(f"❌ {exc}")
        raise SystemExit(1) from exc

    if port != requested_port:
        print(
            f"⚠️ Local port {requested_port} is already in use; "
            f"using http://{host}:{port} instead."
        )
        os.environ["PORT"] = str(port)
        if not _STARTUP_WARMUP_URL_CONFIGURED:
            STARTUP_WARMUP_URL = f"http://localhost:{port}/"

    reload = os.getenv("UVICORN_RELOAD", "").strip().lower() in ("1", "true", "yes")

    if reload:
        uvicorn.run(
            "server:app",
            host=host,
            port=port,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parent)],
        )
    else:
        uvicorn.run("server:app", host=host, port=port, reload=False)
