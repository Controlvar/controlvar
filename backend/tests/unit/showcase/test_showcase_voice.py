"""The voice step (#1066 voiceover): ``python -m showcase.voice``.

It is run here as it runs on the operator's machine, in this process, with a
fake ``aws`` and a fake ``ffmpeg`` first on ``PATH`` (``voice_fakes``), ``HOME``
and the cache under ``tmp_path``, an ``AWS_PROFILE`` no AWS config can hold,
and the AWS config files pointed at empty files. Every run's exit status and
reason line are asserted exactly, and so is how often each fake was called:
without the fake ffmpeg these tests fail (the step refuses, or the real
ffmpeg cannot read the fake's audio); they are never skipped.

Gates: G1 (the narration checked again before any call), G8 (the key and the
cache entry), G9 (one 0.9 stretch), G10 (loudness before capture), G11 (the
AWS CLI's argv and environment, errors by class only, the timeout, the cache
outside any checkout), G18 (the character cap, a warm cache makes no call),
G21 (the fakes are the ones called; the call sites, counted).
"""

from __future__ import annotations

import ast
import json
import os
import pwd
import re
import shutil
import time
import wave
from pathlib import Path
from typing import List

import pytest
import yaml
from showcase import contract
from showcase.capture import config, storyboard
from showcase.voice import audio, cache, polly, step

from backend.tests.unit.showcase.voice_fakes import (
    FIXTURES,
    NO_SUCH_PROFILE,
    Fakes,
    clip_text,
)

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
SHOWCASE = REPO / "tests" / "acceptance" / "showcase"
SLUG = "09-voice-test"

#: A small narrated storyboard: a card with two beats, then a page whose beat
#: clicks Refresh. Six sentences, one said twice.
BOARD = {
    "slug": SLUG,
    "title": "Voice test",
    "seed": "demo",
    "seed_counts": {"experiments": 3, "feature_flags": 2, "users": 4},
    "end_state": {"signed_in_as": "admin@demo.com", "experiments_listed": 3},
    "scenes": [
        {
            "id": "later",
            "card": {
                "label": "Off camera",
                "headline": "63,000 shoppers later",
                "note": "About 2 weeks of traffic.",
            },
            "beats": [
                {
                    "cue": "63,000 shoppers later.",
                    "focus": {"css": "#headline"},
                    "say": [
                        "63,000 shoppers later: about 2 weeks of traffic.",
                        "The JavaScript SDK sent them.",
                    ],
                },
                {
                    "cue": "Then the result.",
                    "focus": {"css": "#card"},
                    "say": ["Then the result comes in.", "No winner yet."],
                },
            ],
        },
        {
            "id": "arriving",
            "open": "/experiments",
            "beats": [
                {
                    "cue": "Each refresh shows more shoppers.",
                    "focus": {"testid": "live-results-panel"},
                    "steps": [{"click": {"testid": "refresh-button"}}],
                    "say": ["Each Refresh shows more shoppers.", "No winner yet."],
                }
            ],
        },
    ],
}
#: The distinct texts Polly is sent for BOARD, in order.
REQUESTS = [
    "63,000 shoppers later: about 2 weeks of traffic.",
    "The JavaScript S.D.K. sent them.",
    "Then the result comes in.",
    "No winner yet.",
    "Each Refresh shows more shoppers.",
]


class Voice:
    """One test's voice step: its fakes, its HOME, its storyboard."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.fakes = Fakes(tmp_path / "fakes")
        empty = tmp_path / "aws-empty"
        empty.mkdir()
        (empty / "config").write_text("", encoding="utf-8")
        (empty / "shared").write_text("", encoding="utf-8")
        self.config_file = str(empty / "config")
        self.credentials_file = str(empty / "shared")
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("PATH", f"{self.fakes.dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("AWS_PROFILE", NO_SUCH_PROFILE)
        monkeypatch.setenv("AWS_CONFIG_FILE", self.config_file)
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", self.credentials_file)
        # Never passed on: key variables override the profile.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "voice-test-key-id-sentinel")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "voice-test-secret-sentinel")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "voice-test-token-sentinel")
        monkeypatch.setenv("SHOWCASE_VOICE_SENTINEL", "must-not-pass")
        self.board_path = tmp_path / "boards" / f"{SLUG}.yaml"
        self.write_board(BOARD)
        monkeypatch.setattr(config, "VIDEOS", {SLUG: str(self.board_path)})

    @property
    def root(self) -> Path:
        return self.home / ".cache" / "experimently-showcase" / "voice"

    def write_board(self, board: dict) -> None:
        self.board_path.parent.mkdir(exist_ok=True)
        self.board_path.write_text(yaml.safe_dump(board), encoding="utf-8")

    def run(self, capsys, *args: str) -> tuple:
        """One run, as ``make showcase-voice`` makes it; ``os.environ`` is handed
        back afterwards (``main`` cuts it to the allow-list), so a second run in
        the same test starts from the same settings."""
        saved = dict(os.environ)
        try:
            status = step.main([SLUG, "--voice", "Ruth", *args])
        finally:
            os.environ.clear()
            os.environ.update(saved)
        out, err = capsys.readouterr()
        return status, out + err

    def entries(self) -> List[Path]:
        if not self.root.is_dir():
            return []
        return sorted(
            p for p in self.root.iterdir() if re.fullmatch(r"[0-9a-f]{64}", p.name)
        )

    def others(self) -> List[str]:
        """What the cache root holds besides finished entries and the run lock."""
        if not self.root.is_dir():
            return []
        return sorted(
            p.name
            for p in self.root.iterdir()
            if not re.fullmatch(r"[0-9a-f]{64}", p.name) and p.name != "showcase.lock"
        )


@pytest.fixture
def voice(tmp_path, monkeypatch) -> Voice:
    return Voice(tmp_path, monkeypatch)


def board_with(say: List[str]) -> dict:
    board = json.loads(json.dumps(BOARD))
    board["scenes"][0]["beats"][1]["say"] = say
    return board


# --- Where the cache is ----------------------------------------------------------------------


def test_the_default_cache_root_is_under_tmp_path_in_every_voice_test(voice, tmp_path):
    root = cache.default_root(os.environ)
    assert root.resolve().is_relative_to(tmp_path.resolve())
    assert root == voice.home / ".cache" / "experimently-showcase" / "voice"


def test_no_showcase_test_sees_the_real_home(tmp_path_factory):
    """The conftest's HOME, for every showcase test: never the account's own."""
    real = Path(pwd.getpwuid(os.getuid()).pw_dir)
    root = cache.default_root(os.environ).resolve()
    assert root.is_relative_to(tmp_path_factory.getbasetemp().resolve())
    assert root != (real / ".cache" / "experimently-showcase" / "voice").resolve()


# --- A cold run, then a warm one ------------------------------------------------------------


def test_a_cold_run_makes_each_clip_once_and_a_warm_run_calls_polly_never(
    voice, capsys
):
    status, out = voice.run(capsys)
    assert status == step.OK, out
    calls = voice.fakes.calls("aws")
    assert [c["argv"][c["argv"].index("--text") + 1] for c in calls] == REQUESTS
    assert len(voice.entries()) == len(REQUESTS)
    assert voice.others() == []
    ffmpeg = voice.fakes.calls("ffmpeg")
    assert ffmpeg, "the fake ffmpeg was never called"
    characters = sum(len(t) for t in REQUESTS)
    assert f"[voice] 5 clips to make, {characters} characters (cap 10,000)" in out
    assert out.count(" new ") == 6 and " cached " not in out
    assert f"[voice] Polly calls: 5; characters sent: {characters};" in out
    assert re.search(
        rf"\[voice\] {SLUG}: 6 lines; projected \d+\.\d s of 90 s \(refused above 87\.3 s\)",
        out,
    )

    status, out = voice.run(capsys)
    assert status == step.OK, out
    assert len(voice.fakes.calls("aws")) == len(REQUESTS), "a warm cache called Polly"
    assert len(voice.fakes.calls("ffmpeg")) > len(ffmpeg), (
        "the warm run measured nothing"
    )
    assert out.count(" cached ") == 6 and " new " not in out
    assert "[voice] Polly calls: 0; characters sent: 0;" in out


def test_each_entry_holds_the_audio_of_its_own_text(voice, capsys):
    status, out = voice.run(capsys)
    assert status == step.OK, out
    for text in REQUESTS:
        path = voice.root / contract.clip_key("Ruth", text)
        assert clip_text(path / "clip.wav") == text
        assert json.loads((path / "entry.json").read_text())["text"] == text


def test_an_edited_sentence_makes_exactly_one_new_clip(voice, capsys):
    assert voice.run(capsys)[0] == step.OK
    voice.write_board(
        board_with(["Then the result comes in, at last.", "No winner yet."])
    )
    status, out = voice.run(capsys)
    assert status == step.OK, out
    calls = voice.fakes.calls("aws")
    assert len(calls) == len(REQUESTS) + 1
    assert calls[-1]["argv"][calls[-1]["argv"].index("--text") + 1] == (
        "Then the result comes in, at last."
    )


def test_entry_json_holds_exactly_these_keys_and_nothing_about_the_caller(
    voice, capsys
):
    assert voice.run(capsys)[0] == step.OK
    assert cache.ENTRY_KEYS == {
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
    for path in voice.entries():
        raw = (path / "entry.json").read_text()
        data = json.loads(raw)
        assert set(data) == cache.ENTRY_KEYS
        assert data["key"] == path.name == contract.clip_key("Ruth", data["text"])
        for word in (
            NO_SUCH_PROFILE,
            "us-west-2",
            "profile",
            "region",
            "account",
            "request_id",
            "RequestId",
        ):
            assert word not in raw, word
        assert sorted(p.name for p in path.iterdir()) == [
            "clip.wav",
            "entry.json",
            "source.mp3",
        ]
    assert (voice.root.stat().st_mode & 0o777) == 0o700


# --- G11: the AWS CLI's argv and environment ---------------------------------------------------


def test_the_aws_childs_argv_is_exactly_the_synthesis_with_only_the_text_and_file_varying(
    voice, capsys
):
    assert voice.run(capsys)[0] == step.OK
    calls = voice.fakes.calls("aws")
    assert len(calls) == len(REQUESTS)
    for call, text in zip(calls, REQUESTS):
        argv = call["argv"]
        out = Path(argv[-1])
        assert argv == [
            "polly",
            "synthesize-speech",
            "--engine",
            "generative",
            "--voice-id",
            "Ruth",
            "--output-format",
            "mp3",
            "--sample-rate",
            "24000",
            "--text",
            text,
            "--output",
            "json",
            str(out),
        ]
        assert out.name == "source.mp3"
        assert out.parent.parent == voice.root.resolve()
        assert out.parent.name.startswith(cache.UNFINISHED)


def test_the_aws_childs_environment_is_exactly_the_allow_list(voice, capsys):
    assert voice.run(capsys)[0] == step.OK
    for call in voice.fakes.calls("aws"):
        env = voice.fakes.passed(call)
        assert set(env) == {
            "PATH",
            "HOME",
            "AWS_PROFILE",
            "AWS_REGION",
            "AWS_PAGER",
            "AWS_CONFIG_FILE",
            "AWS_SHARED_CREDENTIALS_FILE",
        }, sorted(env)
        assert env["AWS_PROFILE"] == NO_SUCH_PROFILE
        assert env["AWS_REGION"] == "us-west-2"
        assert env["AWS_PAGER"] == ""
        assert env["HOME"] == str(voice.home)
        assert env["AWS_CONFIG_FILE"] == voice.config_file
        assert env["AWS_SHARED_CREDENTIALS_FILE"] == voice.credentials_file
        assert env["PATH"].split(os.pathsep)[0] == str(voice.fakes.dir)


def test_without_aws_config_files_set_the_aws_child_gets_neither(
    voice, capsys, monkeypatch
):
    monkeypatch.delenv("AWS_CONFIG_FILE")
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE")
    assert voice.run(capsys)[0] == step.OK
    for call in voice.fakes.calls("aws"):
        env = voice.fakes.passed(call)
        assert set(env) == {"PATH", "HOME", "AWS_PROFILE", "AWS_REGION", "AWS_PAGER"}


def test_the_argv_is_built_from_the_keys_options_and_nothing_else():
    assert polly.synthesize_argv(
        "Ruth", "Hello.", Path("/c/.unfinished-x/source.mp3")
    ) == [
        "aws",
        "polly",
        "synthesize-speech",
        "--engine",
        "generative",
        "--voice-id",
        "Ruth",
        "--output-format",
        "mp3",
        "--sample-rate",
        "24000",
        "--text",
        "Hello.",
        "--output",
        "json",
        "/c/.unfinished-x/source.mp3",
    ]


def test_aws_env_is_the_allow_list_and_drops_key_variables():
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/Users/someone",
        "AWS_PROFILE": NO_SUCH_PROFILE,
        "AWS_ACCESS_KEY_ID": "voice-test-key-id-sentinel",
        "AWS_SECRET_ACCESS_KEY": "x",
        "AWS_SESSION_TOKEN": "x",
        "AWS_DEFAULT_PROFILE": "other",
        "AWS_REGION": "eu-west-1",
        "TMPDIR": "/tmp",
        "LANG": "C",
    }
    assert polly.aws_env(environ) == {
        "PATH": "/usr/bin",
        "HOME": "/Users/someone",
        "AWS_PROFILE": NO_SUCH_PROFILE,
        "AWS_REGION": "us-west-2",
        "AWS_PAGER": "",
    }


def test_ffmpeg_children_get_path_home_and_tmpdir_only(voice, capsys):
    assert voice.run(capsys)[0] == step.OK
    for call in voice.fakes.calls("ffmpeg"):
        env = voice.fakes.passed(call)
        assert set(env) <= {"PATH", "HOME", "TMPDIR"}, sorted(env)
        assert {"PATH", "HOME"} <= set(env)
        assert not any(key.startswith("AWS") for key in call["env"])


def test_a_missing_profile_is_refused_before_any_call_and_a_warm_cache_needs_none(
    voice, capsys, monkeypatch
):
    monkeypatch.delenv("AWS_PROFILE")
    status, out = voice.run(capsys)
    assert status == step.STOPPED
    assert out.splitlines()[-1] == (
        "[voice] refused: 5 clips to make, and AWS_PROFILE is not set: run"
        " AWS_PROFILE=<your profile> make showcase-voice VIDEO=<slug|all>"
    )
    assert voice.fakes.calls("aws") == []
    monkeypatch.setenv("AWS_PROFILE", NO_SUCH_PROFILE)
    assert voice.run(capsys)[0] == step.OK
    monkeypatch.delenv("AWS_PROFILE")
    status, out = voice.run(capsys)
    assert status == step.OK, out
    assert len(voice.fakes.calls("aws")) == len(REQUESTS)


# --- G11: errors by class only ------------------------------------------------------------------

ACCESS_DENIED = (
    "\nAn error occurred (AccessDeniedException) when calling the SynthesizeSpeech"
    " operation: User: arn:aws:sts::123456789012:assumed-role/"
    "AWSReservedSSO_AdministratorAccess_0123456789abcdef/someone@example.com is not"
    " authorized to perform: polly:SynthesizeSpeech\n"
)


def no_account_details(text: str) -> None:
    assert "arn:" not in text
    assert "@" not in text
    assert not re.search(r"[0-9]{12}", text), re.search(r"[0-9]{12}", text)


def test_access_denied_is_reported_by_class_with_no_account_details(voice, capfd):
    voice.fakes.set(
        "aws", {"exit": 254, "stderr": ACCESS_DENIED, "stdout": ACCESS_DENIED}
    )
    status = step.main([SLUG, "--voice", "Ruth"])
    out, err = capfd.readouterr()
    everything = out + err
    assert status == step.STOPPED
    assert (
        f"[voice] STOPPED {SLUG} later beat 1 line 1: Polly refused"
        " (AccessDeniedException). Nothing was cached for this line; the clips"
        " already made are kept"
    ) in everything
    no_account_details(everything)
    assert len(voice.fakes.calls("aws")) == 1
    assert voice.entries() == [] and voice.others() == []


@pytest.mark.parametrize(
    "stderr, expected",
    [
        (
            "The SSO session associated with this profile has expired or is otherwise"
            " invalid. To refresh this SSO session run aws sso login with the"
            " corresponding profile.",
            polly.SIGN_IN_AGAIN,
        ),
        (
            "Error loading SSO Token: Token for https://d-1234567890.awsapps.com/start"
            " does not exist",
            polly.SIGN_IN_AGAIN,
        ),
        (
            f"The config profile ({NO_SUCH_PROFILE}) could not be found",
            "AWS_PROFILE names a profile your AWS config does not have",
        ),
        (
            "Unable to locate credentials. You can configure credentials by running",
            "no AWS sign-in was found for AWS_PROFILE",
        ),
        (
            'Could not connect to the endpoint URL: "https://polly.us-west-2.amazonaws.com/v1/speech"',
            "the AWS CLI could not reach Polly",
        ),
        (
            "An error occurred (ExpiredTokenException) when calling the SynthesizeSpeech"
            " operation: The token in the request has expired",
            f"Polly refused (ExpiredTokenException): {polly.SIGN_IN_AGAIN}",
        ),
        (
            "An error occurred (ThrottlingException) when calling the SynthesizeSpeech operation (reached max retries: 2): Rate exceeded",
            "Polly refused (ThrottlingException)",
        ),
        (
            "something else entirely: 123456789012 someone@example.com",
            "the AWS CLI exited 255; its message is not shown",
        ),
    ],
)
def test_every_failure_is_named_in_the_tools_words(stderr, expected):
    said = polly.failure_class(255, stderr.encode())
    assert said == expected
    assert NO_SUCH_PROFILE not in said and "awsapps" not in said and "https" not in said
    no_account_details(said)


def test_an_expired_sign_in_stops_the_run_with_the_login_to_run(voice, capsys):
    voice.fakes.set(
        "aws",
        {
            "exit": 255,
            "stderr": "Error when retrieving token from sso: Token has expired and refresh failed\n",
        },
    )
    status, out = voice.run(capsys)
    assert status == step.STOPPED
    assert out.splitlines()[-1] == (
        f"[voice] STOPPED {SLUG} later beat 1 line 1: {polly.SIGN_IN_AGAIN}. Nothing was"
        " cached for this line; the clips already made are kept"
    )
    assert voice.entries() == [] and voice.others() == []


def test_a_call_that_hangs_is_stopped_at_the_timeout_and_nothing_is_cached(
    voice, capsys, monkeypatch
):
    assert polly.TIMEOUT_S == 60
    monkeypatch.setattr(polly, "TIMEOUT_S", 1)
    voice.fakes.set("aws", {"sleep": 20})
    started = time.monotonic()
    status, out = voice.run(capsys)
    assert time.monotonic() - started < 15
    assert status == step.STOPPED
    assert out.splitlines()[-1] == (
        f"[voice] STOPPED {SLUG} later beat 1 line 1: Polly did not answer within 1 s."
        " Nothing was cached for this line; the clips already made are kept"
    )
    assert voice.entries() == [] and voice.others() == []


def test_a_failed_call_keeps_the_clips_already_made(voice, capsys):
    assert voice.run(capsys)[0] == step.OK
    voice.write_board(board_with(["A new line comes first.", "And a second new line."]))
    # From its seventh call on (the first run made five), the fake refuses.
    original = (voice.fakes.dir / "aws").read_text()
    (voice.fakes.dir / "aws").write_text(
        original.replace(
            'if config.get("sleep"):',
            'if len((here / "aws-calls.log").read_text().splitlines()) > 6:\n'
            '    sys.stderr.write("An error occurred (ThrottlingException) x")\n'
            "    sys.exit(254)\n"
            'if config.get("sleep"):',
        )
    )
    status, out = voice.run(capsys)
    assert status == step.STOPPED
    assert out.splitlines()[-1] == (
        f"[voice] STOPPED {SLUG} later beat 2 line 2: Polly refused"
        " (ThrottlingException). Nothing was cached for this line; the clips already"
        " made are kept"
    )
    assert len(voice.fakes.calls("aws")) == len(REQUESTS) + 2
    assert len(voice.entries()) == len(REQUESTS) + 1
    assert voice.others() == []


# --- G1 before any call ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence, reason",
    [
        ("The password is Demo1234!", "holds a secret value (static needle 1 of 4)"),
        (
            "Type Demo1234 as the password.",
            "holds a secret value (static needle 1 of 4)",
        ),
        ("Open localhost 3100 in your browser.", "names a host or a port"),
        ("About three weeks of shoppers.", "says 'three' as a word"),
        ("Results update live as shoppers arrive.", "says 'live'"),
    ],
)
def test_a_refused_narration_stops_the_run_with_no_call(
    voice, capsys, sentence, reason
):
    voice.write_board(board_with([sentence]))
    status, out = voice.run(capsys)
    assert status == step.STOPPED
    assert out.splitlines()[0].startswith(
        f"[voice] refused: {SLUG}.yaml: the narration is refused:"
    )
    assert f"- scene later beat 2 say 1: {reason}" in out
    assert voice.fakes.calls("aws") == []
    assert voice.entries() == []


def test_the_voice_step_runs_the_narration_check_itself(voice, capsys, monkeypatch):
    """With the load's check taken away, the step's own check still refuses, with no call."""
    voice.write_board(board_with(["The password is Demo1234!"]))

    def load_unchecked(path):
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return storyboard.Storyboard.model_validate(data, strict=False)

    monkeypatch.setattr(storyboard, "load", load_unchecked)
    status, out = voice.run(capsys)
    assert status == step.STOPPED
    assert out.splitlines()[-1] == (
        f"[voice] refused: {SLUG}: the narration is refused: scene later beat 2 say 1:"
        " holds a secret value (static needle 1 of 4)"
    )
    assert voice.fakes.calls("aws") == []


def test_no_request_holds_prosody(voice, capsys, monkeypatch):
    monkeypatch.setattr(
        contract, "polly_text", lambda text: f'<prosody rate="90%">{text}</prosody>'
    )
    status, out = voice.run(capsys)
    assert status == step.STOPPED
    assert (
        out.splitlines()[-1]
        == f"[voice] refused: {SLUG} later beat 1 line 1: holds <prosody"
    )
    assert voice.fakes.calls("aws") == []


def test_an_unknown_voice_or_video_is_refused(voice, capsys):
    assert step.main([SLUG, "--voice", "Joanna"]) == step.STOPPED
    assert (
        capsys.readouterr().out
        == "[voice] refused: unknown voice 'Joanna'; one of: Ruth\n"
    )
    assert step.main(["99-nothing", "--voice", "Ruth"]) == step.STOPPED
    assert capsys.readouterr().out == (
        f"[voice] refused: unknown video '99-nothing'; one of: {SLUG}, all\n"
    )
    assert voice.fakes.calls("aws") == [] and voice.fakes.calls("ffmpeg") == []


# --- G18: the character cap ----------------------------------------------------------------------


def test_the_character_cap_refuses_before_any_call(voice, capsys, monkeypatch):
    assert contract.VOICE_CHARACTER_CAP == 10_000
    monkeypatch.setattr(contract, "VOICE_CHARACTER_CAP", 1)
    status, out = voice.run(capsys)
    assert status == step.STOPPED
    characters = sum(len(t) for t in REQUESTS)
    assert out.splitlines()[-1] == (
        f"[voice] refused: 5 clips to make, {characters} characters: above the cap of 1"
        " a run; nothing was sent"
    )
    assert voice.fakes.calls("aws") == []


def test_polly_billing_other_than_the_characters_sent_caches_nothing(voice, capsys):
    voice.fakes.set("aws", {"extra_characters": 1})
    status, out = voice.run(capsys)
    assert status == step.REFUSED
    text = REQUESTS[0]
    assert out.splitlines()[-1] == (
        f"[voice] REFUSED {SLUG} later beat 1 line 1: Polly billed {len(text) + 1}"
        f" characters for {len(text)} sent; nothing was cached"
    )
    assert voice.entries() == [] and voice.others() == []


# --- G9: one 0.9 stretch -----------------------------------------------------------------------


def test_a_clip_outside_the_stretch_window_is_not_cached(voice, capsys):
    voice.fakes.set("ffmpeg", {"atempo_short": 2200})  # 45.8 ms short
    status, out = voice.run(capsys)
    assert status == step.REFUSED
    assert re.fullmatch(
        rf"\[voice\] REFUSED {SLUG} later beat 1 line 1: G9:"
        r" the clip is \d+\.\d{3} s from \d+\.\d{3} s of speech \(\d\.\d{4}x\); one 0\.9"
        r" stretch makes \d+\.\d{3} s, from 45 ms shorter to 5 ms longer; nothing was cached",
        out.splitlines()[-1],
    ), out
    assert voice.entries() == [] and voice.others() == []


def test_the_stretch_is_atempo_once_at_0_9():
    args = audio.stretch_args(Path("in.mp3"), Path("out.wav"))
    assert [a for a in args if "atempo" in a] == ["atempo=0.9"]
    assert args[args.index("-af") + 1] == "atempo=0.9"
    assert args[args.index("-ar") + 1] == "48000" and args[args.index("-ac") + 1] == "1"
    assert args[args.index("-c:a") + 1] == "pcm_s16le"


def test_a_cached_clip_restretched_with_its_entry_rewritten_is_refused(voice, capsys):
    assert voice.run(capsys)[0] == step.OK
    entry = voice.root / contract.clip_key("Ruth", REQUESTS[0])
    clip = entry / "clip.wav"
    with wave.open(str(clip), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        count = handle.getnframes()
    longer = round(count / 0.9)
    with wave.open(str(clip), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(frames + b"\0\0" * (longer - count))
    data = json.loads((entry / "entry.json").read_text())
    data.update(samples=longer, clip_sha256=cache.sha256_of(clip))
    (entry / "entry.json").write_text(json.dumps(data))
    status, out = voice.run(capsys)
    assert status == step.REFUSED
    assert out.splitlines()[-1].startswith(
        f"[voice] REFUSED {SLUG} later beat 1 line 1: G9: the cached clip {entry.name[:12]} is "
    )


# --- G8: the cache entry ------------------------------------------------------------------------


def test_a_clip_copied_over_another_is_refused_and_left_for_a_person(voice, capsys):
    assert voice.run(capsys)[0] == step.OK
    third = voice.root / contract.clip_key("Ruth", REQUESTS[2])
    fourth = voice.root / contract.clip_key("Ruth", REQUESTS[3])
    shutil.copy(third / "clip.wav", fourth / "clip.wav")
    before = (fourth / "clip.wav").read_bytes()
    status, out = voice.run(capsys)
    assert status == step.REFUSED
    assert out.splitlines()[-1] == (
        f"[voice] REFUSED {SLUG} later beat 2 line 2: cache entry {fourth.name[:12]} is"
        " refused: clip.wav is not the clip made from it. The tool never repairs or"
        f" overwrites an entry: remove {voice.root.resolve() / fourth.name} by hand (it is"
        f" under {voice.root.resolve()})"
        " and run again"
    )
    assert (fourth / "clip.wav").read_bytes() == before
    assert len(voice.fakes.calls("aws")) == len(REQUESTS)


def test_an_entry_json_with_a_profile_key_is_refused(voice, capsys):
    assert voice.run(capsys)[0] == step.OK
    entry = voice.root / contract.clip_key("Ruth", REQUESTS[0])
    data = json.loads((entry / "entry.json").read_text())
    data["profile"] = NO_SUCH_PROFILE
    (entry / "entry.json").write_text(json.dumps(data))
    status, out = voice.run(capsys)
    assert status == step.REFUSED
    assert (
        "entry.json has keys missing [] and unexpected ['profile']"
        in out.splitlines()[-1]
    )


def test_the_key_names_every_field_a_clip_is_made_from():
    fields = contract.clip_key_fields("Ruth", "Hello.")
    assert fields == {
        "recipe": 1,
        "engine": "generative",
        "voice": "Ruth",
        "format": "mp3",
        "polly_rate": "24000",
        "tempo": "0.9",
        "rate": 48000,
        "text": "Hello.",
    }
    base = contract.clip_key("Ruth", "Hello.")
    assert re.fullmatch(r"[0-9a-f]{64}", base)
    assert contract.clip_key("Danielle", "Hello.") != base
    assert contract.clip_key("Ruth", "Hello!") != base
    for name in (
        "VOICE_RECIPE",
        "VOICE_ENGINE",
        "VOICE_FORMAT",
        "POLLY_RATE",
        "TEMPO",
        "VOICE_RATE",
    ):
        value = getattr(contract, name)
        changed = value + 1 if isinstance(value, int) else value + "x"
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(contract, name, changed)
            assert contract.clip_key("Ruth", "Hello.") != base, name


# --- The cache directory -------------------------------------------------------------------------


def test_a_cache_inside_a_git_checkout_is_refused(voice, capsys, tmp_path):
    checkout = tmp_path / "clone"
    (checkout / ".git").mkdir(parents=True)
    status, out = voice.run(capsys, "--cache", str(checkout / "voice-cache"))
    assert status == step.STOPPED
    assert out.splitlines()[-1] == (
        f"[voice] refused: the voice cache {(checkout / 'voice-cache').resolve()} is inside"
        f" the git checkout {checkout.resolve()}; give --cache a directory outside any checkout"
    )
    assert not (checkout / "voice-cache").exists()
    assert voice.fakes.calls("aws") == []


def test_a_cache_directory_others_can_read_is_refused(voice, capsys, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    status, out = voice.run(capsys, "--cache", str(shared))
    assert status == step.STOPPED
    assert out.splitlines()[-1] == (
        f"[voice] refused: the voice cache {shared.resolve()} has mode 0755; it must be"
        " 0700 (chmod 700 it)"
    )


def test_the_one_deletion_refuses_anything_but_an_unfinished_entry(tmp_path):
    root = cache.CacheRoot(tmp_path / "cache")
    outside = tmp_path / "outside" / f"{cache.UNFINISHED}planted"
    outside.mkdir(parents=True)
    with pytest.raises(cache.CacheRefused, match="it is not under"):
        root.remove_unfinished(outside)
    assert outside.is_dir()
    with pytest.raises(cache.CacheRefused, match="it is not under"):
        root.remove_unfinished(root.path)
    finished = root.path / ("a" * 64)
    finished.mkdir()
    with pytest.raises(
        cache.CacheRefused, match="only an unfinished entry is ever deleted"
    ):
        root.remove_unfinished(finished)
    assert finished.is_dir()
    deeper = root.path / ("b" * 64) / f"{cache.UNFINISHED}x"
    deeper.mkdir(parents=True)
    with pytest.raises(
        cache.CacheRefused, match="only an unfinished entry is ever deleted"
    ):
        root.remove_unfinished(deeper)
    link = root.path / f"{cache.UNFINISHED}link"
    link.symlink_to(outside)
    root.remove_unfinished(link)
    assert not link.is_symlink() and outside.is_dir()
    unfinished = root.unfinished("c" * 64)
    (unfinished / "source.mp3").write_bytes(b"x")
    root.remove_unfinished(unfinished)
    assert not unfinished.exists()


def test_a_second_publish_under_an_existing_key_leaves_the_first_entry_as_it_is(
    tmp_path,
):
    root = cache.CacheRoot(tmp_path / "cache")
    key = "d" * 64
    first = root.path / key
    first.mkdir()
    for name, body in (("source.mp3", b"first"), ("clip.wav", b"first clip")):
        (first / name).write_bytes(body)
    (first / "entry.json").write_text('{"first": true}')
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    second = root.unfinished(key)
    for name in ("source.mp3", "clip.wav", "entry.json"):
        (second / name).write_bytes(b"second")
    with pytest.raises(cache.CacheRefused) as caught:
        root.publish(second, key)
    assert str(caught.value) == (
        f"cache entry {key[:12]} appeared while it was being made; it is kept as it is"
    )
    assert {p.name: p.read_bytes() for p in first.iterdir()} == before
    assert second.is_dir()


def test_a_billed_count_that_is_not_a_plausible_number_is_not_read(voice, capsys):
    voice.fakes.set("aws", {"extra_characters": 123456789012})
    status, out = voice.run(capsys)
    assert status == step.STOPPED
    assert out.splitlines()[-1] == (
        f"[voice] STOPPED {SLUG} later beat 1 line 1: Polly's answer could not be read."
        " Nothing was cached for this line; the clips already made are kept"
    )
    no_account_details(out)
    assert voice.entries() == [] and voice.others() == []


def test_leftovers_of_a_stopped_run_are_removed_and_entries_kept(voice, capsys):
    assert voice.run(capsys)[0] == step.OK
    leftover = voice.root / f"{cache.UNFINISHED}0123456789ab-zzz"
    leftover.mkdir()
    (leftover / "source.mp3").write_bytes(b"half")
    assert voice.run(capsys)[0] == step.OK
    assert not leftover.exists()
    assert len(voice.entries()) == len(REQUESTS)


def test_a_second_run_on_the_same_cache_is_refused(voice, capsys):
    voice.root.mkdir(parents=True, mode=0o700)
    from showcase.capture import guard

    lock = guard.RunLock(voice.root.resolve())
    lock.acquire()
    try:
        status, out = voice.run(capsys)
    finally:
        lock.release()
    assert status == step.STOPPED
    assert out.splitlines()[-1] == (
        f"[voice] refused: another showcase run holds {voice.root.resolve() / 'showcase.lock'};"
        " one run at a time"
    )


# --- G10: loudness before capture ---------------------------------------------------------------


def test_a_hot_clip_is_refused_before_capture(voice, capsys):
    voice.fakes.set("ffmpeg", {"ebur128_when": {"Then the result": "hot"}})
    status, out = voice.run(capsys)
    assert status == step.REFUSED
    assert out.splitlines()[-2] == (
        f"[voice] REFUSED {SLUG}: later beat 2 line 1: true peak -1.5 dBTP is 3.0 dBTP"
        " with the 4.5 dB gain; at most -1.3"
    )
    assert len(voice.entries()) == len(REQUESTS)


def test_quiet_clips_are_refused_before_capture(voice, capsys):
    voice.fakes.set("ffmpeg", {"ebur128": "quiet"})
    status, out = voice.run(capsys)
    assert status == step.REFUSED
    assert out.splitlines()[-2] == (
        f"[voice] REFUSED {SLUG}: the clips' mean loudness -33.0 LUFS is -28.5 LUFS with"
        " the 4.5 dB gain; the target is -19 +-0.7"
    )


def test_the_loudness_reading_runs_at_the_info_level():
    args = audio.loudness_args(Path("clip.wav"))
    assert "-v" not in args and "-loglevel" not in args
    assert args[args.index("-af") + 1] == "ebur128=peak=true"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


def test_the_parser_reads_the_real_ffmpegs_summaries():
    commands = json.loads((FIXTURES / "commands.json").read_text())
    assert commands["ffmpeg"].startswith("ffmpeg version 8.")
    for name in ("typical", "hot", "quiet"):
        expected = commands[f"{name}.txt"]
        assert audio.parse_ebur128(fixture(name)) == (
            expected["integrated_lufs"],
            expected["true_peak_dbtp"],
        )
        assert expected["measure"].endswith("-af ebur128=peak=true -f null -")
        assert " -v " not in expected["measure"]


@pytest.mark.parametrize(
    "text, reason",
    [
        ("", "ffmpeg printed no loudness summary"),
        (fixture("v-error"), "ffmpeg printed no loudness summary"),
        (
            fixture("typical").replace(" dBFS\n[out", " dBTP\n[out"),
            "no true peak in dBFS",
        ),
        (fixture("typical").replace("    I: ", "    J: "), "no integrated loudness"),
    ],
)
def test_the_parser_refuses_a_missing_summary_or_label(text, reason):
    with pytest.raises(audio.AudioFailed, match=reason):
        audio.parse_ebur128(text)


def test_the_gain_and_the_precheck_are_the_plans():
    assert contract.VOICE_GAIN_DB == 4.5
    assert contract.PRECHECK_PEAK_DBTP == -1.3
    assert (contract.LOUDNESS_TARGET_LUFS, contract.PRECHECK_LOUDNESS_LU) == (
        -19.0,
        0.7,
    )


# --- The projection ---------------------------------------------------------------------------


def test_a_video_projected_over_the_cap_is_refused(voice, capsys, monkeypatch):
    monkeypatch.setattr(contract, "MAX_VIDEO_MS", 10_000)
    status, out = voice.run(capsys)
    assert status == step.REFUSED
    assert re.fullmatch(
        rf"\[voice\] REFUSED {SLUG}: projected \d+\.\d s, above 9\.7 s: cut narration by the UX cut list",
        out.splitlines()[-2],
    ), out


# --- G21: the call sites, counted ------------------------------------------------------------------

VOICE = SHOWCASE / "voice"


def calls_in(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            yield node


def named(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id
    return ""


def test_the_voice_step_starts_programs_in_exactly_two_places():
    for path in sorted(VOICE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in ("subprocess", "os"), (path.name, node.lineno)
            if isinstance(node, ast.Import):
                assert all(a.asname is None for a in node.names), (
                    path.name,
                    node.lineno,
                )
    found = [
        (path.name, named(call), call)
        for path in sorted(VOICE.glob("*.py"))
        for call in calls_in(path)
        if named(call).startswith(
            ("subprocess.", "os.system", "os.exec", "os.spawn", "os.popen", "os.fork")
        )
    ]
    assert [(name, how) for name, how, _call in found] == [
        ("audio.py", "subprocess.run"),
        ("polly.py", "subprocess.run"),
    ]
    aws = {k.arg: k.value for k in found[1][2].keywords}
    assert named(aws["env"]) == "aws_env"
    assert [a.id for a in aws["env"].args if isinstance(a, ast.Name)] == ["environ"]
    assert isinstance(aws["timeout"], ast.Name) and aws["timeout"].id == "TIMEOUT_S"
    assert "shell" not in aws
    ffmpeg = {k.arg: k.value for k in found[0][2].keywords}
    assert named(ffmpeg["env"]) == "guard.tool_env"
    assert "shell" not in ffmpeg
    count = sum(
        1
        for path in VOICE.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and node.value == "aws"
    )
    assert count == 1


def test_capture_and_render_never_reach_the_voice_step_or_aws():
    # The contract and the package's own __init__ too: the capture and the
    # render import both, so either importing the voice step would reach it.
    for path in (
        sorted(SHOWCASE.glob("capture/*.py"))
        + sorted(SHOWCASE.glob("render/*.py"))
        + [SHOWCASE / "contract.py", SHOWCASE / "__init__.py"]
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("showcase.voice"), path.name
                assert not (
                    node.module == "showcase"
                    and any(a.name == "voice" for a in node.names)
                ), path.name
            if isinstance(node, ast.Import):
                assert not any(
                    a.name.startswith("showcase.voice") for a in node.names
                ), path.name
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in ("aws", "polly"), (path.name, node.lineno)


def recipe(target: str) -> List[str]:
    lines = (REPO / "Makefile").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        body.append(line)
    return body


def test_make_showcase_never_calls_aws_and_showcase_voice_is_the_one_target_that_does():
    showcase = "\n".join(recipe("showcase"))
    assert "showcase.capture" in showcase
    for word in ("aws", "AWS", "polly", "showcase.voice", "showcase-voice"):
        assert word not in showcase, word
    voiced = recipe("showcase-voice")
    assert voiced == [
        '\tcd tests/acceptance && ../../$(PY) -m showcase.voice "$(VIDEO)" --voice Ruth'
    ]
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert makefile.count("-m showcase.voice") == 1
    assert "--profile" not in voiced[0] and "AWS_PROFILE" not in voiced[0]


def test_deleting_the_fake_ffmpeg_fails_the_step_rather_than_passing(
    voice, capsys, monkeypatch
):
    (voice.fakes.dir / "ffmpeg").unlink()
    monkeypatch.setenv("PATH", str(voice.fakes.dir))
    status, out = voice.run(capsys)
    assert status == step.STOPPED
    assert (
        out.splitlines()[-1]
        == "[voice] refused: ffmpeg is not installed (brew install ffmpeg)"
    )
    assert voice.fakes.calls("aws") == []
