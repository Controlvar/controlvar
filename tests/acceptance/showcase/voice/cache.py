"""The voice cache (#1066 voiceover): one directory per clip, never overwritten.

The root is ``~/.cache/experimently-showcase/voice`` unless ``--cache`` names
another; it is a private directory (mode 0700) and is refused inside a git
checkout, so no clip can be committed by accident. An entry is
``<root>/<key>/``, ``key`` being ``contract.clip_key`` of the text sent to
Polly:

``source.mp3``
    Polly's answer, as it came.
``clip.wav``
    The answer at 90% pace (one ``atempo``), 48 kHz, mono, 16-bit.
``entry.json``
    Exactly ``ENTRY_KEYS``: what the clip is made from (the key's fields), what
    Polly billed, and the two files' sha256 and sample counts. Nothing about
    who made it or where: no profile, region, account or request id.

An entry is written into ``<root>/.unfinished-...`` and renamed into place
once complete, and an existing entry is never replaced: the same key gives the
same audio on every run. ``CacheRoot.remove_unfinished`` is the voice step's
one deletion; it refuses anything but an unfinished entry directly under the
root. A damaged entry is refused, not repaired: it is removed by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from showcase import contract
from showcase.capture import guard
from showcase.voice import audio

#: Under ``$HOME``.
DEFAULT_ROOT = (".cache", "experimently-showcase", "voice")
ROOT_MODE = 0o700
UNFINISHED = ".unfinished-"
SOURCE = "source.mp3"
CLIP = "clip.wav"
ENTRY = "entry.json"
ENTRY_KEYS = frozenset(
    {
        "key",
        "recipe",
        "engine",
        "voice",
        "format",
        "polly_rate",
        "tempo",
        "rate",
        "text",
        "request_characters",
        "source_sha256",
        "raw_samples",
        "clip_sha256",
        "samples",
    }
)


class CacheRefused(Exception):
    """A cache directory or entry the voice step will not use. The message says why."""


def default_root(environ: Mapping[str, str]) -> Path:
    home = environ.get("HOME")
    if not home:
        raise CacheRefused(
            "HOME is not set, so there is no default voice cache; give --cache"
        )
    return Path(home).joinpath(*DEFAULT_ROOT)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class Entry:
    key: str
    path: Path
    text: str
    samples: int
    raw_samples: int
    request_characters: int

    @property
    def source(self) -> Path:
        return self.path / SOURCE

    @property
    def clip(self) -> Path:
        return self.path / CLIP


class CacheRoot:
    """The voice cache's directory: where entries are found, written and (unfinished) removed."""

    def __init__(self, path: Path):
        resolved = Path(os.path.realpath(path))
        checkout = guard.inside_git_checkout(resolved)
        if checkout is not None:
            raise CacheRefused(
                f"the voice cache {resolved} is inside the git checkout {checkout};"
                " give --cache a directory outside any checkout"
            )
        if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
            raise CacheRefused(f"the voice cache {resolved} is too broad")
        resolved.mkdir(mode=ROOT_MODE, parents=True, exist_ok=True)
        info = os.lstat(resolved)
        if not stat.S_ISDIR(info.st_mode):
            raise CacheRefused(f"the voice cache {resolved} is not a directory")
        mode = stat.S_IMODE(info.st_mode)
        if mode != ROOT_MODE:
            raise CacheRefused(
                f"the voice cache {resolved} has mode {mode:04o}; it must be"
                f" {ROOT_MODE:04o} (chmod 700 it)"
            )
        self.path = resolved

    def contains(self, path: Path) -> bool:
        """True when *path* (a link: where the link itself is) is under the root."""
        if path.is_symlink():
            resolved = Path(os.path.realpath(path.parent)) / path.name
        else:
            resolved = Path(os.path.realpath(path))
        return resolved != self.path and self.path in resolved.parents

    def remove_unfinished(self, path: Path) -> None:
        """Delete an unfinished entry. Refused for anything else: a path outside the
        root, the root itself, or a finished entry."""
        if not self.contains(path):
            raise CacheRefused(
                f"refusing to delete {path}: it is not under {self.path}"
            )
        if Path(os.path.realpath(path.parent)) != self.path or not path.name.startswith(
            UNFINISHED
        ):
            raise CacheRefused(
                f"refusing to delete {path}: only an unfinished entry is ever deleted"
            )
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def leftovers(self) -> List[Path]:
        """Unfinished entries a stopped run left."""
        return sorted(p for p in self.path.iterdir() if p.name.startswith(UNFINISHED))

    def unfinished(self, key: str) -> Path:
        return Path(tempfile.mkdtemp(prefix=f"{UNFINISHED}{key[:12]}-", dir=self.path))

    def publish(self, unfinished: Path, key: str) -> Path:
        """Rename a complete entry into place; never over an existing one."""
        final = self.path / key
        if final.exists() or final.is_symlink():
            raise CacheRefused(
                f"cache entry {key[:12]} appeared while it was being made; it is kept as it is"
            )
        os.replace(unfinished, final)
        return final

    def find(self, key: str, voice: str, text: str) -> Optional[Entry]:
        """The entry for *key*, checked, or None when there is none.

        CacheRefused for an entry that is not exactly what the key says it is.
        """
        path = self.path / key
        if not path.exists() and not path.is_symlink():
            return None
        problems = entry_problems(path, key, voice, text)
        if problems:
            raise CacheRefused(
                f"cache entry {key[:12]} is refused: {'; '.join(problems)}. The tool"
                f" never repairs or overwrites an entry: remove {path} by hand (it is"
                f" under {self.path}) and run again"
            )
        data = json.loads((path / ENTRY).read_text(encoding="utf-8"))
        return Entry(
            key=key,
            path=path,
            text=text,
            samples=data["samples"],
            raw_samples=data["raw_samples"],
            request_characters=data["request_characters"],
        )


def entry_record(
    *,
    key: str,
    voice: str,
    text: str,
    request_characters: int,
    source: Path,
    raw_samples: int,
    clip: Path,
    samples: int,
) -> Dict[str, Any]:
    """``entry.json``'s content: exactly ``ENTRY_KEYS``."""
    record = {
        "key": key,
        **contract.clip_key_fields(voice, text),
        "request_characters": request_characters,
        "source_sha256": sha256_of(source),
        "raw_samples": raw_samples,
        "clip_sha256": sha256_of(clip),
        "samples": samples,
    }
    assert set(record) == ENTRY_KEYS, sorted(set(record) ^ ENTRY_KEYS)
    return record


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def entry_problems(path: Path, key: str, voice: str, text: str) -> List[str]:
    """Why the directory at *path* is not the entry for *key*, or nothing."""
    if path.is_symlink() or not path.is_dir():
        return ["it is not a directory"]
    for name in (SOURCE, CLIP, ENTRY):
        member = path / name
        if member.is_symlink() or not member.is_file():
            return [f"{name} is missing or not a plain file"]
    try:
        data = json.loads((path / ENTRY).read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError):
        return [f"{ENTRY} is not JSON"]
    if not isinstance(data, dict) or set(data) != ENTRY_KEYS:
        found = set(data) if isinstance(data, dict) else set()
        return [
            f"{ENTRY} has keys missing {sorted(ENTRY_KEYS - found)} and unexpected"
            f" {sorted(found - ENTRY_KEYS)}"
        ]
    problems: List[str] = []
    if contract.clip_key(voice, text) != key or data["key"] != key:
        problems.append("its key is not the key of the text it is filed under")
    expected = contract.clip_key_fields(voice, text)
    differ = sorted(k for k, v in expected.items() if data[k] != v)
    if differ:
        problems.append(f"{ENTRY} says other {', '.join(differ)} than the key")
    if not all(
        _is_count(data[k]) for k in ("request_characters", "raw_samples", "samples")
    ):
        problems.append(f"{ENTRY} has a count that is not a positive whole number")
    elif data["request_characters"] != len(text):
        problems.append(
            f"Polly billed {data['request_characters']} characters for {len(text)}"
        )
    if sha256_of(path / SOURCE) != data["source_sha256"]:
        problems.append(f"{SOURCE} is not the file Polly answered with")
    if sha256_of(path / CLIP) != data["clip_sha256"]:
        problems.append(f"{CLIP} is not the clip made from it")
    else:
        try:
            if audio.wav_samples(path / CLIP) != data["samples"]:
                problems.append(f"{CLIP} does not hold {data['samples']} samples")
        except audio.AudioFailed as error:
            problems.append(str(error))
    return problems
