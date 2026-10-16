"""A small shell parser for the policy engine (design ch 15 §2.1; decision §0.61).

Commands are parsed, never prefix-matched: CLI prefix rules were bypassed live
by ``git -C . push``, ``sh -c 'git push'`` and ``/usr/bin/git push`` (R-E F6,
F7). This splits a command line into its simple commands (across ``&&``,
``||``, ``;``, ``|``, ``&``, newlines and subshell or group brackets), each with
its words, leading ``NAME=value`` assignments and redirections, on the
standard library's ``shlex``.

It refuses what it can't see through, and a refusal means "ask the person",
never "allow": command or process substitution, any heredoc, ``eval``,
control flow (``if``, ``for``, ``while``, ``case``, functions), unbalanced
quotes, and anything over ``MAX_COMMAND_CHARS``. When in doubt it splits
more, never less: a quoted ``;`` still ends a command here, which can only make
the result stricter.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

MAX_COMMAND_CHARS = 10_000

SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", ";;", "\n"})
BRACKETS = frozenset({"(", ")", "{", "}"})
REDIRECTS = frozenset({">", ">>", "<", "&>", "&>>", ">&", "<&", ">|", "<>"})
CONTROL_FLOW = frozenset({"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case",
                          "esac", "function", "select", "coproc", "[[", "]]"})
#: What a command line can hide behind; the parser won't guess past them.
OPAQUE = (("$(", "command substitution"), ("`", "command substitution"), ("<(", "process substitution"),
          (">(", "process substitution"), ("<<", "a heredoc"))


class ShellRefused(ValueError):
    """The parser can't see through this command line; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Redirect:
    op: str
    target: str

    @property
    def is_file(self) -> bool:
        """False for a descriptor copy (``2>&1``, ``>&-``)."""
        return not (self.op in (">&", "<&") and (self.target.isdigit() or self.target == "-"))

    @property
    def writes(self) -> bool:
        return self.op not in ("<", "<&")


@dataclass(frozen=True)
class SimpleCommand:
    argv: tuple[str, ...]
    assignments: tuple[str, ...] = ()
    redirects: tuple[Redirect, ...] = ()

    @property
    def name(self) -> str:
        """The program's name without its directory: ``/usr/bin/git`` is
        ``git``, as the shell would run it."""
        return self.argv[0].rsplit("/", 1)[-1] if self.argv else ""


def parse(text: str) -> tuple[SimpleCommand, ...]:
    if len(text) > MAX_COMMAND_CHARS:
        raise ShellRefused(f"it is longer than {MAX_COMMAND_CHARS:,} characters")
    for marker, what in OPAQUE:
        if marker in text:
            raise ShellRefused(f"it uses {what}")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace = " \t\r"      # a newline separates commands, like ";"
    lexer.commenters = ""           # "#" is only a comment at a word's start; keep it as text
    lexer.wordchars += "#$%+,:@^~!{}[]=*?\\"
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise ShellRefused(f"its quoting doesn't close ({exc})") from None
    return tuple(_commands(tokens))


def canonical_command(text: str) -> str:
    """The command line a grant and "the same action" bind to (ch 15 §6.3):
    the text with every unquoted run of spaces and tabs made one space and
    the ends trimmed (a carriage return is not collapsed: to bash it is part
    of a word). Quoted spans and escapes are kept as written, so a
    quoted ``'&&'`` stays a word, never the operator, and two commands that
    canonicalise alike differ only in whitespace the shell ignores. Spacing
    next to an operator is kept (``2>f`` and ``2 > f`` differ), which can
    only ask more often.

    A line ``parse`` refuses, one with a ``${…}`` expansion (whose words the
    shell may re-split by a changed ``IFS``) or one with ANSI-C quoting
    (``$'…'``, where ``\'`` is an escaped quote this scan doesn't model), is
    returned as given: hashing more strictly never widens a grant.
    """
    if "${" in text or "$'" in text:
        return text
    try:
        parse(text)
    except ShellRefused:
        return text
    out: list[str] = []
    space = False
    index, end = 0, len(text)
    while index < end:
        char = text[index]
        if char in " \t":
            space = True
            index += 1
            continue
        if space and out:
            out.append(" ")
        space = False
        if char == "\\":
            out.append(text[index:index + 2])
            index += 2
        elif char == "'":
            close = text.find("'", index + 1)
            if close < 0:
                return text
            out.append(text[index:close + 1])
            index = close + 1
        elif char == '"':
            close = index + 1
            while close < end and text[close] != '"':
                close += 2 if text[close] == "\\" else 1
            if close >= end:
                return text
            out.append(text[index:close + 1])
            index = close + 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _commands(tokens: list[str]):
    argv: list[str] = []
    assignments: list[str] = []
    redirects: list[Redirect] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in SEPARATORS or token in BRACKETS:
            if argv or redirects:
                yield _finish(argv, assignments, redirects)
            argv, assignments, redirects = [], [], []
        elif token in REDIRECTS:
            if index + 1 >= len(tokens) or tokens[index + 1] in SEPARATORS | BRACKETS | REDIRECTS:
                raise ShellRefused(f"a redirection ({token}) has no target")
            if argv and argv[-1].isdigit():
                argv.pop()          # "2>file": the 2 names a descriptor, not a word
            redirects.append(Redirect(token, tokens[index + 1]))
            index += 1
        elif any(c in token for c in "<>|;&()") and not _quoted_word(token):
            raise ShellRefused(f"an operator it doesn't know ({token})")
        elif not argv and _is_assignment(token):
            assignments.append(token)
        elif not argv and token == "!":
            pass                    # negates the exit status only
        else:
            if not argv and token in CONTROL_FLOW:
                raise ShellRefused(f"it uses shell control flow ({token})")
            if not argv and token == "eval":
                raise ShellRefused("it uses eval")
            argv.append(token)
        index += 1
    if argv or redirects:
        yield _finish(argv, assignments, redirects)


def _finish(argv: list[str], assignments: list[str], redirects: list[Redirect]) -> SimpleCommand:
    return SimpleCommand(argv=tuple(argv), assignments=tuple(assignments), redirects=tuple(redirects))


def _is_assignment(token: str) -> bool:
    name, eq, _ = token.partition("=")
    return bool(eq) and name.replace("_", "a").isalnum() and not name[0].isdigit()


def _quoted_word(token: str) -> bool:
    """A word holding operator characters can only come from quotes, which
    shlex has already removed; punctuation tokens are pure operators."""
    return not set(token) <= set(";&|<>()")
