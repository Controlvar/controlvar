"""The sweep library's record of a fake account, made in one process: what
deploy/aws-installer/lib/sweep.sh's ``sweep_take`` writes, file for file, without a call.

The uninstall tests compare the account before an install with the account after its
uninstall, through the installer's own verdict (lib/sweep.sh's ``sweep_verdict``), over
many half-built installs. The real sweep makes 131 calls each time, which those tests
cannot afford by the dozen; this makes the same files from the fake's state:

* every read the library's text holds (its ``_sweep_q`` and ``_sweep_each`` lines, with
  ``$r`` for each of its regions), answered by the fake's own ``respond`` against a copy
  of the state, with no fault and no log line;
* each answer flattened as the library's jq filter flattens it (a row per item, a list's
  columns joined by tabs, ``tostring`` for anything else), then sorted bytewise.

test_state_sweep.py holds it equal to the real library's sweep, file for file, on the
new, installed and uninstalled accounts and on an account an install and an uninstall
went through. The shim on PATH runs ``main_argv([<state>, <shapes>, <folder>,
<account>])`` (install_world's ``stub_sweep="state"``).
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import sys
from pathlib import Path

if __package__:
    from . import fake_aws as _fake
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fake_aws as _fake  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[4]
SWEEP = REPO_ROOT / "deploy" / "aws-installer" / "lib" / "sweep.sh"


def reads(
    text: str | None = None,
) -> list[tuple[str, str, list[str], list[str] | None]]:
    """The library's reads, in its order: (folder, type, the list call's arguments, the
    per-item call's arguments or None), ``$r`` left for the region."""
    text = SWEEP.read_text() if text is None else text
    body = text[
        text.index("sweep_take() {") : text.index("\n}\n", text.index("sweep_take() {"))
    ]
    found = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith(("_sweep_q ", "_sweep_each ")):
            continue
        words = shlex.split(line)
        kind, folder, name, rest = words[0], words[1], words[2], words[3:]
        if kind == "_sweep_q":
            found.append((folder, name, rest, None))
        else:
            split = rest.index("--")
            found.append((folder, name, rest[:split], rest[split + 1 :]))
    return found


def regions(text: str | None = None) -> list[str]:
    text = SWEEP.read_text() if text is None else text
    line = next(x for x in text.splitlines() if x.startswith("_SWEEP_REGIONS=("))
    return line[line.index("(") + 1 : line.rindex(")")].split()


def _tostring(value) -> str:
    """jq's tostring: a string as it is, anything else as compact JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _rows(value) -> list[str]:
    """The library's filter: (. // []) | if array then .[] else . end | a row."""
    if value is None:
        value = []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        if isinstance(item, list):
            out.append("\t".join(_tostring(x) for x in item))
        else:
            out.append(_tostring(item))
    return out


class Failed(Exception):
    pass


def _ask(state: dict, shapes: dict, argv: list[str]):
    """One read, as the fake answers it, parsed: exactly one JSON list or null."""
    answer = _fake.Answer()
    env = {
        "AWS_REGION": "us-west-2",
        "AWS_ACCESS_KEY_ID": "state-sweep",
        "AWS_SECRET_ACCESS_KEY": "state-sweep",
        "HOME": "/nonexistent",
    }
    try:
        _fake.respond(state, shapes, [*argv, "--output", "json"], env, answer)
    except (
        _fake.ServiceError,
        _fake.Usage,
        _fake.Unmodelled,
        _fake.CliFailure,
    ) as error:
        raise Failed(f"{type(error).__name__}: {error}") from error
    if answer.status != 0 or not answer.out:
        raise Failed(f"exit {answer.status}, {answer.out!r}")
    value = json.loads(answer.out)
    if value is not None and not isinstance(value, list):
        raise Failed("not one JSON list")
    return value


def take(
    state: dict, shapes: dict, folder: Path, text: str | None = None
) -> tuple[int, int]:
    """The sweep's files in ``folder`` (which must not exist); (reads, failed)."""
    state = copy.deepcopy(state)
    state["faults"] = []
    state["effects"] = []
    state["calls"] = 0
    state.pop("settle", None)
    count = failed = 0
    plan = reads(text)
    for region in regions(text):
        (folder / region).mkdir(parents=True, exist_ok=True)
    (folder / "global").mkdir(parents=True, exist_ok=True)
    for region in [*regions(text), "global"]:
        for where, name, listing, each in plan:
            if (where == "global") != (region == "global"):
                continue
            if where not in ("global", "$r"):
                raise ValueError(f"a read in {where}")

            def fill(words: list[str], region: str = region) -> list[str]:
                return [w.replace("$r", region) for w in words]

            rows: list[str] = []
            count += 1
            try:
                # state["sweep_fails"]: a list the record cannot read, as "<folder>/<type>".
                if f"{region}/{name}" in state.get("sweep_fails", []):
                    raise Failed("a read the test fails")
                value = _ask(state, shapes, fill(listing))
                if each is None:
                    rows = _rows(value)
                else:
                    for item in [_tostring(x) for x in (value or [])]:
                        count += 1
                        argv = [item if w == "{}" else w for w in fill(each)]
                        rows += _rows(_ask(state, shapes, argv))
            except Failed as error:
                failed += 1
                (folder / where.replace("$r", region) / f"{name}.txt").write_text(
                    f"READ FAILED: aws {' '.join(fill(listing))}\n{error}\n"
                )
                continue
            rows.sort(key=lambda s: s.encode())
            path = folder / (region if where == "$r" else "global") / f"{name}.txt"
            path.write_text("".join(row + "\n" for row in rows))
    return count, failed


def main_argv(argv: list[str]) -> int:
    """``<state> <shapes> <folder> <account>``: sweep_take's output line and exit status
    (0 every read answered, 1 any failed, 2 could not start)."""
    state_path, shapes_path, folder, account = (
        Path(argv[0]),
        Path(argv[1]),
        Path(argv[2]),
        argv[3],
    )
    if folder.exists():
        sys.stderr.write(f"sweep: {folder} exists: use a new folder\n")
        return 2
    with _fake._locked(state_path):
        state = json.loads(state_path.read_text())
    if state["account"] != account:
        sys.stderr.write(
            f"STOP: the profile signs in to account {state['account']}, not {account}; nothing was recorded\n"
        )
        return 2
    count, failed = take(state, json.loads(shapes_path.read_text()), folder)
    sys.stdout.write(
        f"sweep {folder.name}: {count} reads, {failed} failed -> {folder}\n"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_argv(sys.argv[1:]))
