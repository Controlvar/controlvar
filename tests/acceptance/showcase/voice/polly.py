"""The showcase tool's one call to AWS: ``aws polly synthesize-speech`` (#1066 voiceover).

* argv: ``aws polly synthesize-speech`` with the options the cache key names
  (engine, voice, output format, sample rate, the text) and the output file,
  plus ``--output json`` so the answer is read the same way whatever output
  format the operator's AWS config sets. Never ``--profile`` or ``--debug``.
* environment, exactly: ``PATH``, ``HOME``, ``AWS_PROFILE`` (the operator's),
  ``AWS_REGION`` (``contract.POLLY_REGION``), ``AWS_PAGER`` (empty), and
  ``AWS_CONFIG_FILE`` / ``AWS_SHARED_CREDENTIALS_FILE`` when the caller set
  them. Key variables (``AWS_ACCESS_KEY_ID`` and the rest) are never passed:
  the CLI lets them override the profile, which could bill another account.
* a timeout of ``TIMEOUT_S`` per call.
* errors by class only (``PollyFailed``): the CLI's own messages name the
  sign-in portal, the profile, the role and the user, so none of its text is
  printed, written or kept.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Mapping

from showcase import contract

#: Seconds one call may take before it is stopped and reported.
TIMEOUT_S = 60
#: Passed to the CLI from the caller's environment, and only these.
PASSED = ("PATH", "HOME", "AWS_PROFILE")
PASSED_IF_SET = ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE")
#: How much of the CLI's answer is read, at most.
READ_BYTES = 4096


class PollyFailed(Exception):
    """A call that did not give a clip. The message is the tool's own words."""


def aws_env(environ: Mapping[str, str]) -> Dict[str, str]:
    """The CLI's whole environment, from the caller's *environ*."""
    env = {key: environ[key] for key in PASSED if environ.get(key)}
    env["AWS_REGION"] = contract.POLLY_REGION
    env["AWS_PAGER"] = ""
    for key in PASSED_IF_SET:
        if environ.get(key):
            env[key] = environ[key]
    return env


def synthesize_argv(voice: str, text: str, out: Path) -> List[str]:
    """The CLI's argv: the sentence's text and the output file are the only variables."""
    return [
        "aws",
        "polly",
        "synthesize-speech",
        "--engine",
        contract.VOICE_ENGINE,
        "--voice-id",
        voice,
        "--output-format",
        contract.VOICE_FORMAT,
        "--sample-rate",
        contract.POLLY_RATE,
        "--text",
        text,
        "--output",
        "json",
        str(out),
    ]


#: An AWS error code in the CLI's message: letters only, so nothing else of the
#: message (an account, an address, a name) can be carried out with it.
_ERROR_CODE = re.compile(rb"An error occurred \(([A-Za-z]{1,64})\)")
_SIGN_IN_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "InvalidSignatureException",
}
SIGN_IN_AGAIN = "the AWS sign-in has expired or is missing: run aws sso login and retry"


def failure_class(returncode: int, stderr: bytes) -> str:
    """What a failed call means, in the tool's own words; never the CLI's text."""
    text = stderr[:READ_BYTES]
    lowered = text.lower()
    if b"sso" in lowered and (
        b"expired" in lowered
        or b"error loading sso token" in lowered
        or b"does not exist" in lowered
        or b"sso login" in lowered
    ):
        return SIGN_IN_AGAIN
    if b"config profile" in lowered and b"could not be found" in lowered:
        return "AWS_PROFILE names a profile your AWS config does not have"
    if b"unable to locate credentials" in lowered:
        return "no AWS sign-in was found for AWS_PROFILE"
    if b"could not connect to the endpoint" in lowered or b"connect timeout" in lowered:
        return "the AWS CLI could not reach Polly"
    code = _ERROR_CODE.search(text)
    if code:
        name = code.group(1).decode("ascii")
        if name in _SIGN_IN_CODES:
            return f"Polly refused ({name}): {SIGN_IN_AGAIN}"
        return f"Polly refused ({name})"
    return f"the AWS CLI exited {returncode}; its message is not shown"


def synthesize(voice: str, text: str, out: Path, environ: Mapping[str, str]) -> int:
    """Ask Polly to speak *text* into *out*; Polly's billed character count.

    Raises ``PollyFailed`` (by class only) for anything but an answer with a
    non-empty file and a readable count.
    """
    try:
        done = subprocess.run(
            synthesize_argv(voice, text, out),
            env=aws_env(environ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        raise PollyFailed("the AWS CLI is not installed (aws)") from None
    except subprocess.TimeoutExpired:
        raise PollyFailed(f"Polly did not answer within {TIMEOUT_S} s") from None
    if done.returncode != 0:
        raise PollyFailed(failure_class(done.returncode, done.stderr))
    try:
        answer = json.loads(done.stdout[:READ_BYTES])
        billed = answer["RequestCharacters"]
    except (ValueError, TypeError, KeyError):
        raise PollyFailed("Polly's answer could not be read") from None
    if (
        not isinstance(billed, int)
        or isinstance(billed, bool)
        or not 0 <= billed < 10**6
    ):
        raise PollyFailed("Polly's answer could not be read")
    if not out.is_file() or out.stat().st_size == 0:
        raise PollyFailed("Polly answered with no audio")
    return billed
