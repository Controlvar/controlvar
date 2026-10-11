"""A fake ``aws`` and a fake ``ffmpeg`` for the voice step's tests (#1066 voiceover).

Both are Python scripts put first on ``PATH``; each appends ``{"argv", "env"}``
to its own calls log beside it, and reads its behaviour from a JSON file beside
it (the voice step gives its children an exact environment, so a test cannot
steer a fake through one).

The fake ``aws`` answers ``polly synthesize-speech`` as the CLI does: an audio
file at the last argument and ``{"ContentType", "RequestCharacters"}`` on
stdout. Its "MP3" holds the request text and a sample count, so the fake
``ffmpeg`` can make a clip that carries the text it was made from (G8: a test
reads it back), stretched by exactly the ``atempo`` values it is given (G9).
For ``ebur128`` it prints one of the summaries captured from the real ffmpeg
(``fixtures/ebur128``), and nothing at ``-v error``, as the real one does.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any, Dict, List

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ebur128"
#: A profile name no AWS config can hold.
NO_SUCH_PROFILE = "showcase-voice-test-no-such-profile"

FAKE_AWS = """#!{python}
import json, os, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
argv = sys.argv[1:]
with open(here / "aws-calls.log", "a", encoding="utf-8") as log:
    log.write(json.dumps({{"argv": argv, "env": dict(os.environ)}}) + "\\n")
config_file = here / "aws.json"
config = json.loads(config_file.read_text()) if config_file.exists() else {{}}
if config.get("sleep"):
    time.sleep(config["sleep"])
if config.get("exit"):
    sys.stdout.write(config.get("stdout", ""))
    sys.stderr.write(config.get("stderr", ""))
    sys.exit(config["exit"])
text = argv[argv.index("--text") + 1]
seconds = 0.3 + len(text) / config.get("chars_per_second", 14.0)
body = json.dumps({{"text": text, "samples24": round(seconds * 24000)}})
Path(argv[-1]).write_bytes(b"FAKE-MP3\\n" + body.encode("utf-8"))
print(json.dumps({{
    "ContentType": "audio/mpeg",
    "RequestCharacters": len(text) + config.get("extra_characters", 0),
}}, indent=4))
"""

FAKE_FFMPEG = """#!{python}
import json, os, sys, wave
from pathlib import Path

here = Path(__file__).resolve().parent
fixtures = Path({fixtures!r})
argv = sys.argv[1:]
with open(here / "ffmpeg-calls.log", "a", encoding="utf-8") as log:
    log.write(json.dumps({{"argv": argv, "env": dict(os.environ)}}) + "\\n")
config_file = here / "ffmpeg.json"
config = json.loads(config_file.read_text()) if config_file.exists() else {{}}
if "-version" in argv:
    print("ffmpeg version fake")
    sys.exit(0)
source = argv[argv.index("-i") + 1]


def clip_text(path):
    with wave.open(path, "rb") as clip:
        frames = clip.readframes(clip.getnframes())
    values = [int.from_bytes(frames[i:i + 2], "little", signed=True) for i in range(0, len(frames), 2)]
    return bytes(values[1:1 + values[0]]).decode("utf-8")


if "ebur128=peak=true" in argv:
    if "-v" in argv and argv[argv.index("-v") + 1] in ("quiet", "panic", "fatal", "error", "warning"):
        sys.exit(0)
    name = config.get("ebur128", "typical")
    text = clip_text(source)
    for needle, fixture in config.get("ebur128_when", {{}}).items():
        if needle in text:
            name = fixture
    sys.stderr.write((fixtures / (name + ".txt")).read_text(encoding="utf-8"))
    sys.exit(0)
head, _, body = Path(source).read_bytes().partition(b"\\n")
if head != b"FAKE-MP3":
    sys.stderr.write(source + ": Invalid data found when processing input\\n")
    sys.exit(1)
made = json.loads(body)
samples = 2 * made["samples24"]
if "-f" in argv and argv[argv.index("-f") + 1] == "s16le":
    sys.stdout.buffer.write(b"\\0\\0" * samples)
    sys.exit(0)
filters = argv[argv.index("-af") + 1].split(",") if "-af" in argv else []
for item in filters:
    if item.startswith("atempo="):
        samples = round(samples / float(item[len("atempo="):])) - config.get("atempo_short", 1300)
out = Path(argv[-1])
if "-n" in argv and out.exists():
    sys.exit(1)
payload = list(made["text"].encode("utf-8"))
values = ([len(payload)] + payload)[:samples]
frames = b"".join(v.to_bytes(2, "little", signed=True) for v in values)
with wave.open(str(out), "wb") as clip:
    clip.setnchannels(1)
    clip.setsampwidth(2)
    clip.setframerate(48000)
    clip.writeframes(frames + b"\\0\\0" * (samples - len(values)))
"""


def interpreter_additions() -> Dict[str, str]:
    """What this Python adds to its own environment as it starts (on macOS
    ``LC_CTYPE``, from the C locale coercion, and ``__CF_USER_TEXT_ENCODING``;
    on Linux ``LC_CTYPE``), measured on a child given ``PATH`` alone."""
    shown = subprocess.run(
        [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    added = json.loads(shown)
    del added["PATH"]
    return added


class Fakes:
    """The two fakes in one directory, and their logs and settings."""

    def __init__(self, root: Path):
        self.dir = root
        root.mkdir(parents=True, exist_ok=True)
        self._write("aws", FAKE_AWS.format(python=sys.executable))
        self._write(
            "ffmpeg", FAKE_FFMPEG.format(python=sys.executable, fixtures=str(FIXTURES))
        )
        self.added = interpreter_additions()
        assert set(self.added) <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}, self.added

    def passed(self, call: Dict[str, Any]) -> Dict[str, str]:
        """A logged call's environment as the voice step passed it: without what
        the fake's own interpreter added when it started (same name, same value)."""
        return {
            key: value
            for key, value in call["env"].items()
            if self.added.get(key) != value
        }

    def _write(self, name: str, text: str) -> None:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def set(self, name: str, settings: Dict[str, Any]) -> None:
        (self.dir / f"{name}.json").write_text(json.dumps(settings), encoding="utf-8")

    def calls(self, name: str) -> List[Dict[str, Any]]:
        log = self.dir / f"{name}-calls.log"
        if not log.exists():
            return []
        return [
            json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
        ]


def clip_text(path: Path) -> str:
    """The request text a fake clip was made from."""
    with wave.open(str(path), "rb") as clip:
        frames = clip.readframes(clip.getnframes())
    values = [
        int.from_bytes(frames[i : i + 2], "little", signed=True)
        for i in range(0, len(frames), 2)
    ]
    return bytes(values[1 : 1 + values[0]]).decode("utf-8")
