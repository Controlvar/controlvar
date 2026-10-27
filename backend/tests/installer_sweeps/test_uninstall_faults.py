"""Uninstall's fault sweep: every read it makes fails closed (D2b, for S2b's guards (h) and
(i), D14's CDKToolkit read and every ownership read-back of S3).

A clean uninstall (from a copy of one clean install, InstallWorld.adopt) names its calls.
Then, for each read in turn, a new uninstall whose fake answers that read with nothing at
all (EMPTY: exit 0, nothing printed) or, where the read asked for one field, with the
whole response (NOQUERY). Each must end in a refusal or a failure (a non-zero exit: an item kept
because its read failed is residue the report names), and never delete anything the clean
uninstall would not: its deletes are the clean run's, in order, with some left out. A read whose answer is the service's own
error (a secret not there) has no field for a query to miss: NOQUERY there changes
nothing. The record of the account is taken from the fake's state in one process, so the
sweep's own reads are not among them (the sweep library has its own tests).
"""

from __future__ import annotations

import pytest

from backend.tests.installer_sweeps.harness import accounts
from backend.tests.installer_sweeps.harness.install_world import (
    LOCAL_TOOLS,
    InstallWorld,
    in_parallel,
    is_aws,
)

pytestmark = pytest.mark.unit

DONE = "Controlvar is uninstalled from account"


def _query(call: dict) -> str | None:
    argv = call["argv"]
    return argv[argv.index("--query") + 1] if "--query" in argv else None


def _is_read(call: dict) -> bool:
    if not is_aws(call) or call["service"] in LOCAL_TOOLS:
        return False
    if "http://127.0.0.1:9" in call["argv"]:
        return (
            False  # the probe: it must fail, and its own test makes it find credentials
        )
    return call["op"].startswith(("list-", "describe-", "get-"))


def _cases(calls: list[dict]) -> list[tuple[tuple, dict, bool]]:
    """(key, fault spec, answered) for each read of the clean uninstall."""
    seen: dict[tuple, int] = {}
    out = []
    for call in calls:
        if not _is_read(call):
            continue
        query = _query(call)
        identity = (call["service"], call["op"], call["region"], query)
        seen[identity] = seen.get(identity, 0) + 1
        spec = {
            "service": call["service"],
            "op": call["op"],
            "region": call["region"],
            "nth": seen[identity],
        }
        if query is not None:
            spec["has"] = [query]
        else:
            # The nth counts every call of the operation in the region, as the fake does.
            same = [
                c
                for c in calls
                if (c["service"], c["op"], c["region"]) == identity[:3]
                and "http://127.0.0.1:9" not in c["argv"]
            ]
            order = [c for c in same if _query(c) is None]
            spec["nth"] = same.index(order[seen[identity] - 1]) + 1
        out.append(((*identity, seen[identity]), spec, call["exit"] == 0))
    return out


def _subsequence(made: list, clean: list) -> bool:
    it = iter(clean)
    return all(any(m == c for c in it) for m in made)


class Sweep:
    def __init__(self, root, shapes) -> None:
        self.root, self.shapes = root, shapes
        self.template = self.world("template")
        done = self.template.install()
        assert done.code == 0, done.err
        clean = self.adopted("clean")
        done = clean.uninstall()
        assert done.code == 0 and clean.unknown == "", (done.err, clean.unknown)
        self.clean_deletes = clean.writes()
        self.cases = _cases(clean.calls)
        #: Each mode's runs, once: key -> (the problem or None, how the run ended).
        self.ran: dict[str, dict] = {}

    def world(self, name: str) -> InstallWorld:
        return InstallWorld(
            self.root / name, accounts.install_target(), self.shapes, stub_sweep="state"
        )

    def adopted(self, name: str) -> InstallWorld:
        world = self.world(name)
        world.adopt(self.template)
        return world

    def run(self, mode: str) -> dict:
        """The problems of every read failed in turn by ``mode`` (each mode run once)."""

        def one(i_key_spec):
            i, key, spec, answered = i_key_spec
            if (key[0], key[1]) in POLLS:
                spec = {k: v for k, v in spec.items() if k != "nth"}
            world = self.adopted(f"{mode}-{i}")
            world.fault(**spec, mode=mode)
            done = world.uninstall()
            return (
                key,
                judge(key, world, done, self.clean_deletes, mode, answered),
                done,
            )

        if mode not in self.ran:
            wanted = [
                (i, key, spec, answered)
                for i, (key, spec, answered) in enumerate(self.cases)
                if mode == "EMPTY" or key[3] is not None
            ]
            found = in_parallel([lambda x=x: one(x) for x in wanted])
            self.ran[mode] = {key: (problem, done) for key, problem, done in found}
        return {
            key: problem for key, (problem, _done) in self.ran[mode].items() if problem
        }


#: Reads uninstall repeats until an answer says the wait is over: one empty answer is a
#: "not yet", so every one of them is failed.
POLLS = {("route53", "get-change")}
#: The reads whose failure is harmless by design, exactly: none yet.
HARMLESS: set[tuple] = set()


def judge(key, world: InstallWorld, done, clean_deletes, mode: str, answered: bool):
    if world.unknown:
        return f"unmodelled: {world.unknown}"
    faulted = [c for c in world.calls if c["fault"] == mode]
    if not faulted:
        return "the fault never applied"
    made = world.writes()
    if not _subsequence(made, clean_deletes):
        return f"deletes beyond the clean run's: {made}"
    if mode == "NOQUERY" and not answered:
        # The answer is the service's error, which no query reaches: a clean run.
        if done.code != 0 or made != clean_deletes:
            return f"a query over an error changed the run: exit {done.code}, {done.err[:300]}"
        return None
    if key in HARMLESS:
        if done.code != 0 or made != clean_deletes:
            return f"a harmless read did not end as a clean run: exit {done.code}, {done.err[:300]}"
        return None
    if done.code == 0 or DONE in done.out:
        return f"exit {done.code} after a read that failed: that read fails open"
    if not done.err.startswith(("REFUSED: ", "FAILED: ")):
        return f"no message: {done.err[:300]}"
    return None


@pytest.fixture(scope="module")
def sweep(tmp_path_factory, shapes_file) -> Sweep:
    return Sweep(tmp_path_factory.mktemp("uninstall-faults"), shapes_file)


#: How many reads a clean uninstall makes (the sweep's aside): a change to it is a change
#: to what uninstall reads, and this sweep must see it.
READS = 26


def test_the_sweep_sees_every_read_of_a_clean_uninstall(sweep):
    assert len(sweep.cases) == READS, [k for k, _, _ in sweep.cases]


@pytest.mark.parametrize("mode", ["EMPTY", "NOQUERY"])
def test_every_uninstall_read_fails_closed(sweep, mode):
    """D2b: an empty answer, or the whole response where the read asked for one field,
    ends uninstall in a refusal or a failure (the item it read about kept), never in a
    delete the clean uninstall would not make."""
    wrong = sweep.run(mode)
    assert wrong == {}, "\n".join(f"{k}: {v}" for k, v in wrong.items())


def test_a_cdk_setup_stack_it_cannot_read_is_kept_and_said_so(sweep):
    """An empty answer to step 5's read of the CDK's setup stack keeps the stack, and the
    run says the installer could not tell it is this install's (not that it is another's),
    as the message that ends the run says of what it left."""
    sweep.run("EMPTY")
    keys = [
        key
        for key in sweep.ran["EMPTY"]
        if key[:2] == ("cloudformation", "describe-stacks")
        and key[3] == "Stacks[0].[StackStatus,StackId,CreationTime]"
    ]
    assert len(keys) == 1, keys
    _problem, done = sweep.ran["EMPTY"][keys[0]]
    assert (
        "  Kept: a CDK setup stack (CDKToolkit), which the installer could not tell is this install's."
        in done.out
    ), [line for line in done.out.splitlines() if "(CDKToolkit)" in line]
    first = done.err.splitlines()[0]
    assert first.startswith("FAILED: The install is removed, but the account holds "), (
        first
    )
    assert "that the installer could not tell are this install's" in first, first
