"""What a runtime's adapter declares for its lifecycle: ``SPEC``, exported by
``adapters/<name>/lifecycle/__init__.py`` (plan §5.2). Commands, aliases and
old URLs are data in its manifest; this is the behaviour only code can give."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping

from services.errors import ServiceError


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


@dataclass(frozen=True)
class LifecycleSpec:
    title: str                                            # the runtime's name, in messages
    binary: Callable[[], str]                             # the CLI's path, or its bare name when not found
    logged_in: Callable[[], bool]                         # the local login check: reads files, calls no vendor
    account: Callable[[], str | None] = lambda: None      # the signed-in account's label, never a credential
    via_api_key: Callable[[], bool] = lambda: False       # by an API key or cloud credentials (billed per use)
    sign_in: SignIn | None = None                         # None: signed in from a terminal only
    update_note: Callable[[str | None], str | None] = lambda version: None


class LifecycleError(ServiceError):
    """A refused or failed lifecycle action; ``extra`` goes into the answer."""

    def __init__(self, code: str, message: str, status: int = 400, **extra) -> None:
        super().__init__(code, message, status)
        self.extra = extra
