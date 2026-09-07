"""
usage_sync.py — Daily usage sync to xo-swarm-api.

Runs as an asyncio background task started from the FastAPI lifespan. The
parsing/aggregation work lives in the active agent's
``config/agents/<name>/usage/usage.py`` (resolved via
``services.cowork_agent.engine.usage_loader``). This file is orchestration only:

  - watermark I/O
  - delegate to ``module.aggregate_for_sync(since_date=watermark)``
  - decorate records with workspace identifiers
  - POST to ``${CHAT_API_BASE_URL}/usage/report``
  - advance watermark

On first run (no watermark): full historical backfill. Subsequently: only
processes dates >= last-synced watermark.
"""

import asyncio
import json
import os
import datetime
from collections import defaultdict

import httpx

from services.cowork_agent.registry.agent_registry import get_active_agent
from services.cowork_agent.engine.usage_loader import load_usage_module

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CHAT_API_BASE_URL = os.getenv("CHAT_API_BASE_URL", "https://api-swarm-beta.xo.builders")
USAGE_REPORT_PATH = "/usage/report"

# Watermark file is namespaced under the active agent so each adapter keeps
# its own independent sync state. Switching AGENT_NAME (e.g. openclaw →
# claude_code) lands on a fresh path with no watermark, which correctly
# triggers a full backfill for the new agent rather than reusing the
# previous agent's truncation point.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_WATERMARK_PATH = os.path.join(
    _REPO_DIR, "data", get_active_agent().name, "usage_sync_state.json"
)
SYNC_STATE_FILE = os.getenv("USAGE_SYNC_STATE_FILE", _DEFAULT_WATERMARK_PATH)

SYNC_HOUR_UTC = int(os.getenv("USAGE_SYNC_HOUR_UTC", "2"))
DEBUG_ENABLED = (os.getenv("USAGE_SYNC_DEBUG", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
DEBUG_INTERVAL_MINUTES = int(os.getenv("USAGE_SYNC_DEBUG_INTERVAL_MINUTES", "0") or "0")

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _timestamp_prefix() -> str:
    tz_pref = (os.getenv("USAGE_SYNC_LOG_TZ", "UTC") or "UTC").strip().upper()
    if tz_pref == "IST":
        tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")
        tz_name = "IST"
    else:
        tz = datetime.timezone.utc
        tz_name = "UTC"
    ts = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{ts} {tz_name}]"


def _debug_log(message: str) -> None:
    if DEBUG_ENABLED:
        print(f"{_timestamp_prefix()} usage_sync_debug: {message}")


# ---------------------------------------------------------------------------
# Watermark persistence
# ---------------------------------------------------------------------------


def _load_sync_state() -> dict:
    if os.path.exists(SYNC_STATE_FILE):
        try:
            with open(SYNC_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_sync_state(state: dict) -> None:
    os.makedirs(os.path.dirname(SYNC_STATE_FILE), exist_ok=True)
    tmp = SYNC_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, SYNC_STATE_FILE)


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------


def _empty_record(workspace_id: str, workspace_name, project_id, note: str,
                  report_date: str | None = None) -> dict:
    if not report_date:
        report_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return {
        "report_date": report_date,
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "project_id": project_id,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "total_tokens": 0,
        "total_cost": 0.0,
        "input_cost": 0.0,
        "output_cost": 0.0,
        "cache_read_cost": 0.0,
        "cache_write_cost": 0.0,
        "total_messages": 0,
        "total_sessions": 0,
        "total_tool_calls": 0,
        "model_usage": [],
        "tool_usage": [],
        "note": note,
    }


def _record_key_probe(state: dict, outcome: str, status: int | None) -> None:
    """Persist the last key-probe outcome so the Setup tab can show the
    reporting status (off / on / blocked) without grepping the log.
    ``outcome``: "accepted", "rejected", or "unverified" (probe inconclusive).
    Best-effort: a failure to persist must never block the sync itself."""
    state["key_probe"] = {
        "outcome": outcome,
        "status": status,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        _save_sync_state(state)
    except OSError as e:
        print(f"{_timestamp_prefix()} usage_sync: could not persist key-probe state: {e}")


async def _key_accepted(
    client: httpx.AsyncClient, url: str, headers: dict, state: dict
) -> bool:
    """Verify the token before any usage data leaves the machine.

    Same endpoint, empty record list: the request carries only the token,
    passes through exactly the auth dependency the real report passes
    through, and stores nothing on success. Anything but 200 fails closed —
    a token the swarm has not accepted sends no usage data, so "reported
    only when the key is valid" is literally true, not just "stored only
    when the key is valid". The outcome is persisted for the Setup tab.
    """
    try:
        probe = await client.post(url, json={"records": []}, headers=headers)
    except Exception as e:
        print(f"{_timestamp_prefix()} usage_sync: could not verify XO_API_KEY ({e}) — nothing sent, will retry next cycle")
        _record_key_probe(state, "unverified", None)
        return False
    if probe.status_code == 200:
        _record_key_probe(state, "accepted", probe.status_code)
        return True
    if probe.status_code in (401, 403):
        print(f"{_timestamp_prefix()} usage_sync: XO_API_KEY rejected by xo-swarm-api (HTTP {probe.status_code}) — nothing sent. Fix or remove the key in .env.")
        _record_key_probe(state, "rejected", probe.status_code)
    else:
        print(f"{_timestamp_prefix()} usage_sync: key check returned HTTP {probe.status_code} — nothing sent, will retry next cycle")
        _record_key_probe(state, "unverified", probe.status_code)
    return False


async def _post_records(records: list, daily: dict | None, state: dict) -> None:
    from routers.auth.auth import get_auth_token

    token = get_auth_token()
    if not token:
        print(f"{_timestamp_prefix()} usage_sync: not authenticated — skipping report (nothing sent)")
        return
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{CHAT_API_BASE_URL.rstrip('/')}{USAGE_REPORT_PATH}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            if not await _key_accepted(client, url, headers, state):
                return
            response = await client.post(url, json={"records": records}, headers=headers)

        if response.status_code == 200:
            result = response.json()
            upserted = result.get("upserted", 0)
            if daily is None:
                print(f"{_timestamp_prefix()} usage_sync: posted placeholder record (note carried; watermark not advanced)")
            else:
                print(f"{_timestamp_prefix()} usage_sync: successfully synced {upserted} day(s) to swarm")
                yesterday = (
                    datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
                ).strftime("%Y-%m-%d")
                latest_date = max(daily.keys())
                watermark = min(yesterday, latest_date)
                state["last_synced_date"] = watermark
                state["last_sync_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                _save_sync_state(state)
        else:
            print(f"{_timestamp_prefix()} usage_sync: POST failed with {response.status_code}: {response.text[:200]}")
    except Exception as e:
        print(f"{_timestamp_prefix()} usage_sync: error posting to swarm (will retry next cycle): {e}")


def usage_reporting_status() -> dict:
    """One fact for the Setup tab: is anything being reported?

    ``status`` is one of:
      - "off"      no key set — nothing is sent, not even a placeholder
      - "on"       key set and accepted by xo-swarm-api on the last probe
      - "blocked"  key set but rejected (HTTP 401/403) — nothing is sent
      - "pending"  key set, no conclusive probe yet (first sync still to
                   run, or the last probe could not reach the swarm)

    The probe outcome comes from the state file `_key_accepted` writes; the
    same file carries the watermark, so ``last_synced_date`` rides along.
    """
    from routers.auth.auth import get_auth_token  # sanctioned lazy import (see CONTRIBUTING)

    state = _load_sync_state()
    probe = state.get("key_probe") or {}
    outcome = probe.get("outcome")
    if not get_auth_token():
        status = "off"
    elif outcome == "accepted":
        status = "on"
    elif outcome == "rejected":
        status = "blocked"
    else:
        status = "pending"
    return {
        "status": status,
        "probe_outcome": outcome,
        "probe_at": probe.get("at"),
        "last_synced_date": state.get("last_synced_date"),
        "last_sync_at": state.get("last_sync_at"),
    }


# ---------------------------------------------------------------------------
# Core sync — delegates parsing/aggregation to the active agent's module
# ---------------------------------------------------------------------------


async def _run_sync(is_backfill: bool = False) -> None:
    """Aggregate the active agent's usage and POST to xo-swarm-api.

    Always sends at least one record: when discovery/aggregation yields
    nothing, posts a zero-valued placeholder whose ``note`` column explains
    why so the analytics surface still shows the sync ran.
    """
    workspace_id = os.getenv("CODER_WORKSPACE_ID") or "unknown"
    workspace_name = os.getenv("CODER_WORKSPACE_NAME") or None
    project_id = os.getenv("XO_PROJECT_ID") or None

    state = _load_sync_state()
    last_synced_date = None if is_backfill else state.get("last_synced_date")

    try:
        mod = load_usage_module()
    except Exception as e:
        note = f"failed to load active agent's usage module: {e}"
        print(f"{_timestamp_prefix()} usage_sync: {note} — posting placeholder")
        await _post_records(
            [_empty_record(workspace_id, workspace_name, project_id, note)],
            daily=None, state=state,
        )
        return

    try:
        aggregated = mod.sync_payload(since_date=last_synced_date)
    except Exception as e:
        note = f"aggregation failed in agent usage module: {e}"
        print(f"{_timestamp_prefix()} usage_sync: {note} — posting placeholder")
        await _post_records(
            [_empty_record(workspace_id, workspace_name, project_id, note)],
            daily=None, state=state,
        )
        return

    session_dates_count = aggregated.pop("__session_dates__", {})
    parse_errors = aggregated.pop("__parse_errors__", None)
    daily = aggregated

    if not daily:
        if parse_errors:
            joined = "; ".join(parse_errors[:3])
            extra = f" (+{len(parse_errors) - 3} more)" if len(parse_errors) > 3 else ""
            note = f"failed to parse all input(s): {joined}{extra}"
        elif last_synced_date:
            note = f"no new entries since watermark {last_synced_date}"
        else:
            note = "session files present but contained no usage entries"
        print(f"{_timestamp_prefix()} usage_sync: {note} — posting placeholder")
        await _post_records(
            [_empty_record(workspace_id, workspace_name, project_id, note)],
            daily=None, state=state,
        )
        return

    partial_note: str | None = None
    if parse_errors:
        joined = "; ".join(parse_errors[:3])
        extra = f" (+{len(parse_errors) - 3} more)" if len(parse_errors) > 3 else ""
        partial_note = f"partial: skipped {len(parse_errors)} unparseable input(s): {joined}{extra}"

    for date_str, day in daily.items():
        day["total_sessions"] = session_dates_count.get(date_str, 0)
        day["workspace_id"] = workspace_id
        day["workspace_name"] = workspace_name
        day["project_id"] = project_id
        day["note"] = partial_note

    await _post_records(list(daily.values()), daily=daily, state=state)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


async def _seconds_until_next_run() -> float:
    if DEBUG_INTERVAL_MINUTES > 0:
        return float(DEBUG_INTERVAL_MINUTES * 60)
    now = datetime.datetime.now(datetime.timezone.utc)
    target = now.replace(hour=SYNC_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def _next_target_utc(now: datetime.datetime | None = None) -> datetime.datetime:
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if DEBUG_INTERVAL_MINUTES > 0:
        return current + datetime.timedelta(minutes=DEBUG_INTERVAL_MINUTES)
    target = current.replace(hour=SYNC_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= current:
        target += datetime.timedelta(days=1)
    return target


async def start_usage_sync_scheduler() -> None:
    """Entry point for the background task.

    1. If no watermark exists, run full backfill.
    2. Then run daily at SYNC_HOUR_UTC:00 UTC.

    Errors are caught and logged — a failure never crashes the server.
    """
    await asyncio.sleep(5)

    state = _load_sync_state()
    _debug_log(
        f"scheduler_started pid={os.getpid()} state_file={SYNC_STATE_FILE} "
        f"last_synced_date={state.get('last_synced_date')} last_sync_at={state.get('last_sync_at')} "
        f"mode={'debug_interval' if DEBUG_INTERVAL_MINUTES > 0 else 'daily'} "
        f"interval_minutes={DEBUG_INTERVAL_MINUTES if DEBUG_INTERVAL_MINUTES > 0 else 'n/a'}"
    )
    if not state.get("last_synced_date"):
        print(f"{_timestamp_prefix()} usage_sync: no watermark found, starting historical backfill...")
        _debug_log("initial_path=backfill reason=no_watermark")
        try:
            await _run_sync(is_backfill=True)
            print(f"{_timestamp_prefix()} usage_sync: backfill complete")
            _debug_log("backfill_completed")
        except Exception as e:
            print(f"{_timestamp_prefix()} usage_sync: backfill error (non-fatal): {e}")
            _debug_log(f"backfill_failed error={e}")
    else:
        last_sync_at = state.get("last_sync_at")
        needs_catchup = False
        catchup_reason = "no_last_sync_at"
        if last_sync_at:
            try:
                last_dt = datetime.datetime.fromisoformat(last_sync_at)
                hours_since = (datetime.datetime.now(datetime.timezone.utc) - last_dt).total_seconds() / 3600
                if hours_since > 24:
                    needs_catchup = True
                    catchup_reason = f"hours_since_gt_24 ({hours_since:.2f})"
                    print(f"{_timestamp_prefix()} usage_sync: last sync was {hours_since:.1f}h ago, running catch-up sync...")
                else:
                    catchup_reason = f"hours_since_le_24 ({hours_since:.2f})"
            except Exception:
                needs_catchup = True
                catchup_reason = "last_sync_at_parse_error"

        _debug_log(f"catchup_decision needs_catchup={needs_catchup} reason={catchup_reason}")

        if needs_catchup:
            try:
                _debug_log("trigger_enter source=startup_catchup")
                await _run_sync(is_backfill=False)
                print(f"{_timestamp_prefix()} usage_sync: catch-up sync complete")
                _debug_log("trigger_exit source=startup_catchup status=ok")
            except Exception as e:
                print(f"{_timestamp_prefix()} usage_sync: catch-up sync error (non-fatal): {e}")
                _debug_log(f"trigger_exit source=startup_catchup status=error error={e}")
        else:
            print(f"{_timestamp_prefix()} usage_sync: watermark found (last synced: {state['last_synced_date']}), next run at {SYNC_HOUR_UTC:02d}:00 UTC")

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        wait = await _seconds_until_next_run()
        target = _next_target_utc(now)
        _debug_log(
            f"sleep_begin pid={os.getpid()} now_utc={now.isoformat()} "
            f"target_utc={target.isoformat()} wait_seconds={wait:.1f}"
        )
        if DEBUG_INTERVAL_MINUTES > 0:
            print(
                f"{_timestamp_prefix()} usage_sync: next sync in {wait / 60:.1f}m "
                f"(debug interval mode: every {DEBUG_INTERVAL_MINUTES}m)"
            )
        else:
            print(f"{_timestamp_prefix()} usage_sync: next sync in {wait / 3600:.1f}h (at {SYNC_HOUR_UTC:02d}:00 UTC)")
        await asyncio.sleep(wait)
        wake = datetime.datetime.now(datetime.timezone.utc)
        _debug_log(f"sleep_end pid={os.getpid()} woke_at_utc={wake.isoformat()} skew_seconds={(wake - target).total_seconds():.1f}")
        try:
            _debug_log("trigger_enter source=daily_loop")
            await _run_sync(is_backfill=False)
            _debug_log("trigger_exit source=daily_loop status=ok")
        except Exception as e:
            print(f"{_timestamp_prefix()} usage_sync: daily sync error (will retry tomorrow): {e}")
            _debug_log(f"trigger_exit source=daily_loop status=error error={e}")
