"""
GPG symmetric encryption + chunking.

Encrypted blobs are written as `part-NNN.gpg` files of at most
``CHUNK_SIZE_BYTES`` each. Splitting into chunks is what keeps a single
file under GitHub's 100 MB upload limit; the encryption itself doesn't
need it. Restore reassembles parts in numeric order before decrypting,
verifying the manifest's sha256 first so corruption can't reach gpg.

The passphrase is passed via `--passphrase-fd 0` (stdin) so it never
appears in argv or process listings. We also disable gpg-agent caching
(`--no-symkey-cache`) so a leaked agent session can't decrypt later
backups silently.
"""

from __future__ import annotations

import asyncio
from utils.commands import run
import shutil
from pathlib import Path

from .config import CHUNK_SIZE_BYTES


_PART_PREFIX = "part-"
_PART_SUFFIX = ".gpg"
_PART_DIGITS = 3  # part-000, part-001, ... up to part-999
_READ_BUFFER = 1024 * 1024  # 1 MB streaming reads


class GpgUnavailableError(RuntimeError):
    """`gpg` and/or `gpg-agent` are not on PATH."""


class GpgFailedError(RuntimeError):
    """gpg exited non-zero. stderr is the message (passphrases never appear there)."""


def check_gpg_available() -> None:
    """Raise GpgUnavailableError unless both `gpg` and `gpg-agent` are on PATH.

    Both are required: `gpg` does the work but invokes `gpg-agent` for
    passphrase handling even with `--passphrase-fd`. Without the agent,
    `gpg` fails with a confusing 'no pinentry' error on Debian/Ubuntu.
    """
    missing = [b for b in ("gpg", "gpg-agent") if shutil.which(b) is None]
    if missing:
        raise GpgUnavailableError(
            f"Required binaries not on PATH: {', '.join(missing)}. "
            "Install with: sudo apt-get update && sudo apt-get install -y gnupg gpg-agent"
        )


def _part_name(index: int) -> str:
    return f"{_PART_PREFIX}{index:0{_PART_DIGITS}d}{_PART_SUFFIX}"


def list_parts(snapshot_dir: Path) -> list[Path]:
    """Return part files in numeric order. Empty list if none exist."""
    return sorted(snapshot_dir.glob(f"{_PART_PREFIX}*{_PART_SUFFIX}"))


async def encrypt_to_chunks(
    plaintext_path: Path,
    output_dir: Path,
    passphrase: str,
) -> list[Path]:
    """Symmetrically encrypt ``plaintext_path`` and split into chunks under ``output_dir``.

    Steps:
      1. ``gpg --symmetric ...`` writes the full ciphertext to a temp file.
      2. The ciphertext is split into ``CHUNK_SIZE_BYTES`` parts and the
         temp file is deleted.

    Returns the ordered list of part paths. Caller is responsible for
    writing a manifest with the part list + sha256 of the concatenation.
    """
    check_gpg_available()
    output_dir.mkdir(parents=True, exist_ok=True)
    ciphertext_tmp = output_dir / "_ciphertext.tmp"
    if ciphertext_tmp.exists():
        ciphertext_tmp.unlink()

    res = await run(
        [
            "gpg", "--batch", "--yes", "--quiet", "--no-symkey-cache",
            "--cipher-algo", "AES256", "--symmetric", "--passphrase-fd", "0",
            "-o", str(ciphertext_tmp), str(plaintext_path),
        ],
        input=passphrase.encode("utf-8"), separate_stderr=True,
    )
    if res.binary_missing:
        raise FileNotFoundError(res.output)
    if res.returncode != 0:
        # Clean up partial output before raising so the next attempt isn't confused.
        if ciphertext_tmp.exists():
            ciphertext_tmp.unlink()
        raise GpgFailedError(f"gpg encrypt failed: {(res.stderr or res.output).strip()}")

    parts = _split_into_parts(ciphertext_tmp, output_dir)
    ciphertext_tmp.unlink()
    return parts


def _split_into_parts(source: Path, output_dir: Path) -> list[Path]:
    """Stream ``source`` into ``part-NNN.gpg`` files of CHUNK_SIZE_BYTES each.

    Always produces at least one part (``part-000.gpg``), even for an
    empty file, so the manifest's `parts` list never ends up empty.
    """
    parts: list[Path] = []
    index = 0
    remaining_in_part = CHUNK_SIZE_BYTES
    current_path = output_dir / _part_name(index)
    parts.append(current_path)
    current = current_path.open("wb")
    try:
        with source.open("rb") as src:
            while True:
                to_read = min(_READ_BUFFER, remaining_in_part)
                chunk = src.read(to_read)
                if not chunk:
                    break
                current.write(chunk)
                remaining_in_part -= len(chunk)
                if remaining_in_part == 0:
                    current.close()
                    index += 1
                    current_path = output_dir / _part_name(index)
                    parts.append(current_path)
                    current = current_path.open("wb")
                    remaining_in_part = CHUNK_SIZE_BYTES
    finally:
        current.close()
    return parts


async def decrypt_from_chunks(
    parts: list[Path],
    output_path: Path,
    passphrase: str,
) -> None:
    """Concatenate ``parts`` and decrypt into ``output_path``.

    Caller MUST have already verified the manifest's sha256 against the
    concatenated parts before calling this — gpg's own integrity check
    is end-of-message-only and won't catch a part swapped out by an
    attacker who knew the passphrase format.
    """
    check_gpg_available()
    if not parts:
        raise ValueError("decrypt_from_chunks: no parts provided")

    ciphertext_tmp = output_path.parent / "_ciphertext.tmp"
    if ciphertext_tmp.exists():
        ciphertext_tmp.unlink()

    # Reassemble. We stream rather than read-all-then-write so very large
    # snapshots don't blow the heap.
    with ciphertext_tmp.open("wb") as dst:
        for p in parts:
            with p.open("rb") as src:
                while True:
                    chunk = src.read(_READ_BUFFER)
                    if not chunk:
                        break
                    dst.write(chunk)

    res = await run(
        [
            "gpg", "--batch", "--yes", "--quiet", "--no-symkey-cache",
            "--decrypt", "--passphrase-fd", "0",
            "-o", str(output_path), str(ciphertext_tmp),
        ],
        input=passphrase.encode("utf-8"), separate_stderr=True,
    )
    if res.binary_missing:
        raise FileNotFoundError(res.output)
    ciphertext_tmp.unlink()  # always remove the reassembled ciphertext
    if res.returncode != 0:
        if output_path.exists():
            output_path.unlink()  # don't leave a partial plaintext
        raise GpgFailedError(f"gpg decrypt failed: {(res.stderr or res.output).strip()}")
