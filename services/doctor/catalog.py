"""What each state file is, what its damage costs, and the safe next step.

One :class:`About` per parsed inventory pattern (#188 design §5), written
from the code that writes and reads each file; the evidence is in
docs/sept24sesh/xo-doctor-reporting-investigation.md, Appendix A. The v1
report chose its explanation from (outcome × class) alone, which gave eight
different files one identical sentence (#188 issue 5).

Texts never name an agent. ``{project}`` and ``{toolkit}`` are filled by
:func:`fill` with what :func:`labels` finds in the finding's subject.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable, Optional

from services.doctor import inventory
from services.doctor.model import FAIL, WARN, ev, moment, size
from services.doctor.reading import ReadResult

#: The outcomes that mean "readable bytes, unusable content".
UNUSABLE = ("empty", "invalid_json", "wrong_type")

#: The end of a finding's title, per outcome or schema direction.
PHRASE = {
    "empty": "is empty",
    "invalid_json": "is damaged (not valid JSON)",
    "wrong_type": "holds the wrong kind of data",
    "unreadable": "can't be read",
    "special": "isn't a regular file",
    "file_too_large": "is too large to check",
    "newer": "was written by a newer xo-space",
    "older": "uses a format this xo-space doesn't read",
    "missing": "has no format version",
}


@dataclass(frozen=True)
class About:
    name: str
    owner: str
    consequence: str
    self_repair: str
    next_step: str
    #: Per situation ("empty", "invalid_json", "wrong_type", "watcher_stopped"):
    #: replacement texts for consequence / self_repair / next_step.
    overrides: dict = field(default_factory=dict)
    #: Per outcome: a level that differs from the behaviour's.
    levels: dict = field(default_factory=dict)

    def text(self, keys: tuple, part: str) -> str:
        """The first override of ``part`` found under ``keys``, else the default."""
        for key in keys:
            value = self.overrides.get(key, {}).get(part)
            if value:
                return value
        return getattr(self, part)


_NO_REBUILD = "Nothing: its store never rewrites a file it can't read."
_RESTORE_PROJECT = "Restore it from git or the last project sync (both keep the project's .xo/ folder)"

ABOUT: dict[str, About] = {
    "inbox/inbox.json": About(
        name="The Inbox", owner="the Inbox",
        consequence="The Inbox can't use its saved items, seen and done marks, notes and read positions.",
        self_repair="Nothing: the Inbox leaves a file it can't parse alone.",
        next_step=("Repair the JSON by hand if you can. Otherwise move the file aside: the next Inbox refresh "
                   "rebuilds items from the last day of activity, the last week of GitHub issues and blocked "
                   "todos, but seen and done marks and notes are lost."),
        overrides={
            "empty": {
                "consequence": ("The Inbox treats it as a new, empty Inbox: its items, seen and done marks and "
                                "notes are already gone."),
                "self_repair": ("The next Inbox refresh adds back items from the last day of activity, the last "
                                "week of GitHub issues and blocked todos, as new."),
                "next_step": ("If you have an earlier copy of inbox.json, put it back before the Inbox is next "
                              "opened; otherwise nothing is needed."),
            },
            "invalid_json": {
                "consequence": ("The Inbox shows as empty (the server answers with no items rather than an "
                                "error), and marking items done, deleting them or adding notes fails."),
            },
            "wrong_type": {
                "consequence": ("The Inbox treats it as empty, and its next refresh or change overwrites it, so "
                                "its items, seen and done marks and notes are lost for good."),
                "self_repair": "The next Inbox refresh replaces it with a fresh Inbox.",
                "next_step": "If you have an earlier copy of inbox.json, put it back now, before the Inbox is opened again.",
            },
        },
        levels={"empty": WARN},
    ),
    "scheduler/jobs.json": About(
        name="The saved commands list", owner="the command scheduler",
        consequence=("No scheduled command runs, and the Schedules page fails with an error, because the saved "
                     "commands (what to run, when, with which settings) can't be read."),
        self_repair=_NO_REBUILD,
        next_step=("Repair the JSON by hand. Deleting it removes every saved command, and each would have to be "
                   "added again."),
    ),
    "scheduler/state.json": About(
        name="The command schedule", owner="the command scheduler",
        consequence=("No scheduled command runs and the Schedules page can't load, because each command's next and "
                     "last run times can't be read."),
        self_repair=("Nothing while it's unreadable. If it's deleted, the scheduler rebuilds it from the saved "
                     "commands on its next tick."),
        next_step=("Delete it to let the scheduler rebuild it within a second. One-time commands that already ran "
                   "will run once more, and each command's last result is blank until its next run."),
    ),
    "connections/accounts.json": About(
        name="The connected-account list", owner="Connections",
        consequence="Connection cards show no account name, and each poll repeats the account lookup.",
        self_repair=("Nothing while it can't be parsed. If it's deleted, each connection's next poll looks the "
                     "account up again."),
        next_step="Delete it; the labels come back on each connection's next poll.",
    ),
    "connections/*/config.json": About(
        name="{toolkit}'s connection settings file", owner="Connections",
        consequence=("{toolkit} counts as not configured: it isn't polled, so its new items don't reach the Inbox, "
                     "and saving its settings fails."),
        self_repair="Nothing.",
        next_step=("Fix the file by hand, or delete it and turn {toolkit} back on in Connections, choosing the "
                   "interval and feeds again. Items already collected are kept."),
        overrides={"wrong_type": {
            "consequence": ("{toolkit} counts as not configured and isn't polled; the next time its settings are "
                            "saved, this file is replaced with the defaults."),
        }},
    ),
    "connections/*/state.json": About(
        name="{toolkit}'s polling record", owner="the connections poller",
        consequence="{toolkit}'s card shows it as never polled, and it forgets which items it already collected.",
        self_repair=("The next poll, due at once, writes a fresh record. Items it collects again are added to its "
                     "events file a second time, and its item count starts from zero."),
        next_step="Nothing is needed; to repair it sooner, use Poll now on the {toolkit} card.",
    ),
    "sharing/*.json": About(
        name="A shared project's relay bookmark", owner="project sharing",
        consequence="The relay forgets how far it got for this repository.",
        self_repair=("The relay writes it again on its next pass, within about a minute. Commits pushed just "
                     "before may not be announced to collaborators until the next push, and the Inbox may show "
                     "one repeated \"new commits fetched\" item."),
        next_step="Nothing is needed; delete it if it keeps coming back damaged.",
    ),
    "settings/onboarding.json": About(
        name="The onboarding record", owner="Setup",
        consequence="The first-run onboarding is shown again.",
        self_repair="Finishing onboarding again writes a fresh record.",
        next_step="Delete it, or leave it; only the date onboarding was first completed is lost.",
    ),
    "settings/theme.json": About(
        name="The Space theme setting", owner="the theme settings",
        consequence="The theme can't be loaded or saved: both fail with an error, so it can't be fixed from the UI.",
        self_repair="Nothing.",
        next_step="Delete it; the default theme comes back and you can choose again.",
    ),
    "settings/branding.json": About(
        name="The Space name and logo setting", owner="the branding settings",
        consequence=("The Space's name and logo can't be loaded or saved: both fail with an error, so it can't be "
                     "fixed from the UI."),
        self_repair="Nothing.",
        next_step="Delete it; the default name comes back, and the logo has to be uploaded again.",
    ),
    "usage/*.json": About(
        name="The usage-report bookmark", owner="usage reporting",
        consequence="Usage reporting can't tell what it already sent.",
        self_repair="At the next server start it re-sends all usage history once and writes the file again.",
        next_step="Nothing is needed, or delete it; the next start rebuilds it.",
        overrides={"wrong_type": {
            "consequence": ("Usage reporting has stopped: it fails on this file every time the server starts, "
                            "and never rewrites it."),
            "self_repair": "Nothing: the file is never rewritten, so a restart doesn't help.",
            "next_step": "Delete it and restart the server; usage history is then re-sent once.",
        }},
        levels={"wrong_type": FAIL},
    ),
    "projects/offsets.json": About(
        name="The watcher's reading-position file", owner="the watcher",
        consequence=("Nothing breaks while the server keeps running: the watcher keeps the positions in memory. If "
                     "the server restarts before this file is valid again, every agent session is read again from "
                     "the start, and usage totals, session counters and timelines count everything twice."),
        self_repair="The watcher writes it again from memory the next time any agent session grows.",
        next_step=("Don't restart the server yet. Use any agent session (or wait for one to continue), then run "
                   "checks again to confirm the file is valid."),
        overrides={"watcher_stopped": {
            "consequence": ("The watcher isn't running, so it can't write this file again. When the server next "
                            "starts, every agent session is read again from the start, and usage totals, session "
                            "counters and timelines count everything twice."),
            "self_repair": "Nothing while the watcher is stopped.",
            "next_step": ("If you have an earlier copy of this file, put it back before the server restarts. "
                          "Otherwise expect usage totals and timelines to double after the next restart."),
        }},
    ),
    "projects/*-offsets.json": About(
        name="An agent's reading-position file", owner="the watcher",
        consequence=("Nothing breaks while the server keeps running. If the server restarts before this file is "
                     "valid again, that agent's sessions are read again from the start, and their usage totals "
                     "and timelines count twice."),
        self_repair="The watcher writes it again from memory the next time that agent's sessions grow.",
        next_step="Don't restart the server yet. Use a session of that agent, then run checks again.",
        overrides={"watcher_stopped": {
            "consequence": ("The watcher isn't running, so it can't write this file again. When the server next "
                            "starts, that agent's sessions are read again from the start and count twice."),
            "self_repair": "Nothing while the watcher is stopped.",
            "next_step": "If you have an earlier copy of this file, put it back before the server restarts.",
        }},
    ),
    "projects/*/stats.json": About(
        name="Project {project}'s usage record", owner="the watcher",
        consequence=("The project's token, tool and daily usage totals can't be read, and the next agent activity "
                     "in this project replaces the file with totals counted from that moment."),
        self_repair="Nothing recovers the old totals: the watcher's next write starts them again from zero.",
        next_step=("If you have an earlier copy, put it back before the next agent activity in this project; after "
                   "that, the old totals are gone."),
        overrides={"wrong_type": {
            "consequence": ("The project's usage totals can't be read, and the watcher fails on this file on every "
                            "tick: the project's usage and timeline stop updating and new events are lost."),
            "next_step": "Put back an earlier copy, or move the file aside so the watcher can start the totals again.",
        }},
    ),
    "projects/*/workitems/claims.json": About(
        name="Project {project}'s claim list", owner="work items",
        consequence="Claiming or releasing a work item in this project fails, and no work item shows as in progress.",
        self_repair="Nothing: the claims store refuses a damaged file.",
        next_step="Move it aside: nothing lasting is lost, and the next claim recreates it.",
    ),
    "projects/*/sessions/sessions-augment.json": About(
        name="Project {project}'s session-counter record", owner="the watcher",
        consequence=("Message and tool counts for this project's sessions can't be read, and the next agent "
                     "activity in this project replaces the file with counts from that moment."),
        self_repair="Nothing recovers the old counts.",
        next_step="If you have an earlier copy, put it back before the next agent activity in this project.",
        overrides={"wrong_type": {
            "consequence": ("The watcher fails on this file on every tick: this project's usage totals and "
                            "timeline stop updating and new events are lost."),
            "next_step": "Put back an earlier copy, or move the file aside.",
        }},
    ),
    "projects/*/sessions/sessionslist.d/*.json": About(
        name="A session index entry of project {project}", owner="the session index",
        consequence="That chat drops out of the session list and can't be resumed. Its transcript is still on disk.",
        self_repair="Nothing: only a new session writes a new entry.",
        next_step=("Repair the JSON by hand, or move the file aside to keep it; don't delete it. A repaired entry "
                   "shows again at once."),
    ),
    "sessions/sessionslist.d/*.json": About(
        name="A session index entry for chats outside any project", owner="the session index",
        consequence=("That chat, started outside any project, drops out of the session list and can't be resumed. "
                     "Its transcript is still on disk."),
        self_repair="Nothing: only a new session writes a new entry.",
        next_step="Repair the JSON by hand, or move the file aside to keep it; don't delete it.",
    ),
    "projects/*/github/issues.json": About(
        name="Project {project}'s GitHub issue copy", owner="the GitHub poller",
        consequence="The project's GitHub issues aren't shown until it's refreshed.",
        self_repair="The GitHub poller overwrites it on its next successful poll, usually within a minute.",
        next_step=("Leave it: deleting it would close this project's GitHub items in the Inbox, and they wouldn't "
                   "reopen."),
    ),
    "cache/heartbeat.json": About(
        name="The watcher's heartbeat", owner="the watcher",
        consequence="Only the liveness display is affected.",
        self_repair="The watcher writes it again on its next tick, within seconds.",
        next_step="Nothing is needed.",
    ),
    "cache/stats.json": About(
        name="The Space-wide usage record", owner="the watcher",
        consequence="Space-wide usage shows zeros.",
        self_repair=("Rebuilt from the projects' totals, but only when a project's totals next change, or if the "
                     "file is deleted."),
        next_step="Delete it; the watcher writes it again within seconds.",
    ),
    "cache/graph.json": About(
        name="The workspace map", owner="the watcher",
        consequence="The workspace map is rebuilt on request instead of served from this copy.",
        self_repair="Rebuilt within 30 seconds.", next_step="Nothing is needed.",
    ),
    "cache/dashboard.json": About(
        name="The dashboard view", owner="the watcher",
        consequence="The dashboard is rebuilt on request instead of served from this copy.",
        self_repair="Rebuilt within 30 seconds.", next_step="Nothing is needed.",
    ),
    "cache/sessions.json": About(
        name="The merged session telemetry", owner="the watcher",
        consequence="Session telemetry is rebuilt on request instead of served from this copy.",
        self_repair="Rebuilt within 30 seconds.", next_step="Nothing is needed.",
    ),
    "cache/sessions/sessionslist.json": About(
        name="The Space-wide session list", owner="the watcher",
        consequence="Space-wide session lists show as empty.",
        self_repair="Rebuilt from the projects' session entries when a session next changes, or if the file is deleted.",
        next_step="Delete it; the watcher writes it again within seconds.",
    ),
    "cache/sessions/sessions-augment.json": About(
        name="The Space-wide session-counter record", owner="the watcher",
        consequence="Space-wide session rows show no message or tool counts.",
        self_repair="Rebuilt when a project's session counters next change, or if the file is deleted.",
        next_step="Delete it; the watcher writes it again within seconds.",
    ),
    "cache/activity/workspace.json": About(
        name="The Space's live activity", owner="the watcher",
        consequence="The Space doesn't show who is working right now.",
        self_repair="Rebuilt when someone's activity next changes, or if the file is deleted.",
        next_step="Delete it; the watcher writes it again within seconds.",
    ),
    "cache/activity/projects/*.json": About(
        name="Project {project}'s live activity", owner="the watcher",
        consequence=("The project doesn't show who is working in it right now, and its work-item claims may look "
                     "stale."),
        self_repair="Rebuilt when activity in the project next changes, or if the file is deleted.",
        next_step="Delete it; the watcher writes it again within seconds.",
    ),
    "space.json": About(
        name="The Space record", owner="the watcher",
        consequence="Nothing in the server reads it back; tools that read the Space's id and folders from it see nothing.",
        self_repair="The watcher replaces it within a minute; its label, creation date and first-seen dates are reset.",
        next_step="Nothing is needed; re-enter a custom Space label after it's rewritten.",
    ),
    "projects.json": About(
        name="The project registry", owner="the watcher",
        consequence="Nothing in the server reads it; tools that list projects from it see a stale list.",
        self_repair="The watcher replaces it within seconds.", next_step="Nothing is needed.",
    ),
    "xo.json": About(
        name="The feature manifest", owner="the server",
        consequence="The app may show or hide sections wrongly, and live model and channel status isn't mirrored.",
        self_repair="Rewritten when the server restarts.", next_step="Restart the server.",
    ),
    "project.json": About(
        name="Project {project}'s identity", owner="the project layout",
        consequence=("The project's history (sessions, usage, timeline, claims) is hidden: new activity is "
                     "recorded under its folder name instead, and leftover checks are paused."),
        self_repair="Nothing: the server refuses to replace a damaged identity.",
        next_step=(_RESTORE_PROJECT + "; it holds the project's pid. Don't delete it: a new pid would be created "
                   "and the project's history orphaned. Re-creating the project's agent before it's repaired does "
                   "the same."),
    ),
    "todos.json": About(
        name="Project {project}'s todo list", owner="the todo store",
        consequence=("The todo list shows as empty (no error), adding or changing todos fails, and the project's "
                     "todo items in the Inbox are closed."),
        self_repair="Nothing.",
        next_step=_RESTORE_PROJECT + ", or repair the JSON by hand. Don't delete it: an empty list would be created in its place.",
    ),
    "workitems.json": About(
        name="Project {project}'s work-item list", owner="the work-item store",
        consequence="Every work-item request for this project fails, and its GitHub issues show as untracked.",
        self_repair="Nothing.",
        next_step=(_RESTORE_PROJECT + ", or repair the record the server log names. Don't delete it: an empty "
                   "list would be created in its place."),
    ),
    "peers.json": About(
        name="Project {project}'s collaborator list", owner="the collaborator store",
        consequence="Collaborator requests fail, and the project can't be removed until it's repaired.",
        self_repair="Nothing.",
        next_step=(_RESTORE_PROJECT + ". Don't delete it: the project would look unshared, and removal and "
                   "sharing checks would see no collaborators."),
    ),
    "agent.json": About(
        name="Project {project}'s agent record", owner="the agent backend",
        consequence="The agent looks missing, or the whole agent list fails to load if the file holds the wrong kind of data.",
        self_repair="Nothing.",
        next_step=(_RESTORE_PROJECT + ". Don't re-create the agent while project.json is also damaged: that "
                   "replaces the project's identity."),
        levels={outcome: WARN for outcome in UNUSABLE},
    ),
}

_FALLBACK = About(name="This file", owner="its store", consequence="The store that owns it can't use it.",
                  self_repair="Unknown.", next_step="The server log names what fails.")

_DEFAULT_LEVEL = {
    inventory.IRREPLACEABLE: FAIL, inventory.OVERWRITTEN_NEXT: FAIL, inventory.LIVE_STATE: FAIL,
    inventory.READ_POSITION: FAIL, inventory.SELF_OVERWRITING: WARN, inventory.CHANGE_CACHE: WARN,
    inventory.REBUILT_VIEW: WARN,
}


def about(spec: inventory.Spec) -> About:
    return ABOUT.get(spec.pattern, _FALLBACK)


def level_for(spec: inventory.Spec, outcome: str, *, watcher_alive: bool) -> str:
    """The level of an unusable or unreadable file: a per-outcome override,
    else the behaviour's; a read position is only urgent when nothing will rewrite it."""
    override = about(spec).levels.get(outcome)
    if override:
        return override
    if spec.behaviour == inventory.READ_POSITION:
        return WARN if watcher_alive else FAIL
    return _DEFAULT_LEVEL.get(spec.behaviour, FAIL)


def labels(spec: inventory.Spec, subject: str, project_label: Callable[[str], str]) -> dict[str, str]:
    """``project``/``toolkit`` for this finding, read from its subject."""
    parts = subject.split("/")
    if spec.base == inventory.PROJECT:
        return {"project": parts[0]}
    if spec.base != inventory.STATE:
        return {}
    if parts[0] == "projects" and len(parts) >= 3:
        return {"project": project_label(parts[1])}
    if parts[:3] == ["cache", "activity", "projects"] and len(parts) == 4:
        return {"project": PurePosixPath(parts[3]).stem}
    if parts[0] == "connections" and len(parts) >= 3:
        return {"toolkit": parts[1].capitalize()}
    return {}


class _Missing(dict):
    def __missing__(self, key: str) -> str:
        return "?"


def fill(text: str, values: dict[str, str]) -> str:
    return text.format_map(_Missing(values))


def read_evidence(about_: About, result: ReadResult, accepted: Optional[frozenset[int]], now: float,
                  values: dict[str, str]) -> list[dict[str, str]]:
    """Owner, size, time, and the exact problem, for a read finding."""
    rows = [ev("Owned by", fill(about_.owner, values))]
    if result.size is not None:
        rows.append(ev("Size", size(result.size)))
    if result.mtime is not None:
        rows.append(ev("Last modified", moment(result.mtime, now)))
    if result.outcome == "invalid_json" and result.detail:
        rows.append(ev("Problem", result.detail))
    if result.outcome == "wrong_type" and result.detail:
        rows.append(ev("Problem", f"a JSON {result.detail} where an object is expected"))
    if result.outcome == "schema_unsupported":
        found = "none" if result.schema is None else str(result.schema)
        reads = ", ".join(str(v) for v in sorted(accepted)) if accepted else "any"
        rows.append(ev("Format version", f"{found} (this xo-space reads {reads})"))
    return rows
