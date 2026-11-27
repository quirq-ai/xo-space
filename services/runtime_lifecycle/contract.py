"""What a runtime's adapter declares for its lifecycle: ``SPEC``, exported by
``adapters/<name>/lifecycle/__init__.py``. Commands, aliases and
old URLs are data in its manifest; this is the behaviour only code can give."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Literal, Mapping

from services.errors import ServiceError

#: The sign-in events (``signin.stream``) rendered as the frames one view sends.
View = Callable[[AsyncIterator[dict]], AsyncIterator[dict]]


@dataclass(frozen=True)
class SignIn:
    """The CLI's own sign-in, run on a pseudo-terminal. ``paste_code``: it
    prints a link and reads the code the person pastes back. ``device_code``:
    it prints a link and a code, and waits until the person has entered it."""

    style: Literal["paste_code", "device_code"]
    argv: tuple[str, ...]                                 # after the binary
    auth_url: Callable[[str], str | None]                 # the link in a line of output, ANSI stripped
    code: Callable[[str], str] = str.strip                # a pasted value as the CLI reads it
    env: Mapping[str, str] = field(default_factory=dict)
    unset: tuple[str, ...] = ()                           # variables the CLI must not see (they'd bypass the login)
    finish: Callable[[], None] = lambda: None             # what the CLI's login leaves undone, once it has landed
    landed: Callable[[], bool] | None = None              # the CLI's own login, when ``logged_in`` also counts a key
    # Runs over a login already there (to refresh or switch it): success is the CLI exiting 0 with its login landed.
    relogin: bool = False


@dataclass(frozen=True)
class Credential:
    """A sign-in as the runtime's CLI stored it, handed to its account kind's listeners."""

    access: str = field(repr=False)
    refresh: str | None = field(default=None, repr=False)
    email: str | None = None
    expires_ms: int | None = None
    release: Callable[[], None] = lambda: None            # deletes the CLI's copy, for a sole-holder listener


@dataclass(frozen=True)
class Legacy:
    """How the runtime's old Connect URLs answered, where the engine's own frames differ."""

    view: View | None = None
    code_sent: str = "Code sent"


@dataclass(frozen=True)
class LifecycleSpec:
    title: str                                            # the runtime's name, in messages
    binary: Callable[[], str]                             # the CLI's path, or its bare name when not found
    logged_in: Callable[[], bool]                         # the local login check: reads files, calls no vendor
    forget: Callable[[], None] = lambda: None             # drop what ``logged_in`` keeps: an action through XO ran
    login_polled: bool = True                             # False: ``logged_in`` runs the CLI; the advisory asks the runtime
    account: Callable[[], str | None] = lambda: None      # the signed-in account's label, never a credential
    via_api_key: Callable[[], bool] = lambda: False       # by an API key or cloud credentials (billed per use)
    sign_in: SignIn | None = None                         # None: signed in from a terminal only
    update_note: Callable[[str | None], str | None] = lambda version: None
    credential: Callable[[], Credential | None] = lambda: None
    legacy: Legacy = Legacy()


class LifecycleError(ServiceError):
    """A refused or failed lifecycle action; ``extra`` goes into the answer."""

    def __init__(self, code: str, message: str, status: int = 400, **extra) -> None:
        super().__init__(code, message, status)
        self.extra = extra
