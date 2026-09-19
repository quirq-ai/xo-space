"""What the sharing module emits and raises."""

#: Event lines this module writes to ``sharing/events.jsonl``: the relay's
#: transitions, ``sharing.<kind>`` (``status.py``). ``recent`` and the
#: Inbox feeder read them as the notification vocabulary.
TYPES = (
    "sharing.shared_with_you",   # a repo shared with this workspace is not cloned here
    "sharing.fetched",           # commits arrived through the ledger and were fetched
    "sharing.error",             # a fetch or a scan failed for one repo
    "sharing.revoked",           # the repo left this workspace's membership
    "sharing.cloned",            # the relay cloned a shared repo here
    "sharing.clone_failed",      # it tried and could not (needs_auth, no_access, exists, error)
)

#: Signals raised through ``services.signals``: none yet.
SIGNALS = ()
