"""Reading a command's options the way the program itself does (GNU getopt).

The policy has to see exactly what a program will do, so every spelling a
program accepts must read the same here: a value glued to its flag
(``-o.mcp.json``, ``--output=x``) or in the next word, short flags bundled
together (``-sO``, ``-Ei``), options after operands (``rm -f src -r``), and
``--`` ending the options. A ``Spec`` says which of a program's flags take a
value; nothing else about the program is needed to split its words.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    short_valued: str = ""                       # letters whose flag takes a value (-o FILE, -oFILE)
    short_optional: str = ""                     # letters with an optional glued value only (sed -i.bak)
    long_valued: frozenset[str] = frozenset()    # --names taking a value (--output FILE, --output=FILE)
    permute: bool = True                         # options may follow operands


@dataclass(frozen=True)
class Options:
    flags: tuple[tuple[str, str | None], ...]    # ("-o", "file"), ("--silent", None), …
    operands: tuple[str, ...]

    def names(self) -> frozenset[str]:
        return frozenset(name for name, _ in self.flags)

    def has(self, *names: str) -> bool:
        return any(name in names for name, _ in self.flags)

    def values(self, *names: str) -> list[str]:
        return [value or "" for name, value in self.flags if name in names]

    def value(self, *names: str) -> str | None:
        found = self.values(*names)
        return found[-1] if found else None


def read_options(args: list[str] | tuple[str, ...], spec: Spec = Spec()) -> Options:
    flags: list[tuple[str, str | None]] = []
    operands: list[str] = []
    ended = False
    i = 0
    while i < len(args):
        word = args[i]
        if ended or word == "-" or not word.startswith("-"):
            operands.append(word)
            ended = ended or not spec.permute
        elif word == "--":
            ended = True
        elif word.startswith("--"):
            name, eq, value = word.partition("=")
            if eq:
                flags.append((name, value))
            elif name in spec.long_valued:
                flags.append((name, args[i + 1] if i + 1 < len(args) else ""))
                i += 1
            else:
                flags.append((name, None))
        else:
            for j, letter in enumerate(word[1:], start=1):
                rest = word[j + 1:]
                if letter in spec.short_valued:
                    if rest:
                        flags.append((f"-{letter}", rest))
                    else:
                        flags.append((f"-{letter}", args[i + 1] if i + 1 < len(args) else ""))
                        i += 1
                    break
                if letter in spec.short_optional:
                    flags.append((f"-{letter}", rest or None))
                    break
                flags.append((f"-{letter}", None))
        i += 1
    return Options(tuple(flags), tuple(operands))
