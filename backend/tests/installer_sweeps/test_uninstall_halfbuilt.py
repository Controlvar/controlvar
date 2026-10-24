"""Uninstall from any half-built install, and a re-install converging (E2, E1, E3).

A clean install against the fakes names its changes (CLEAN_WRITES, 18 of them). Then, for
each change j in turn, two installs in fake accounts of their own:

* **The change fails** (AccessDenied at j). The install stops with exit 1, and a second
  install over its record refuses (UX-B9, exit 2) and calls nothing at all (E1, the half
  that needs no uninstall). Then uninstall: exit 0, its report ends "Not explained: none",
  and the account is the account before the install plus exactly the kept list it printed,
  both ways (E2, F3). Then install again: it installs, with the clean run's changes (the
  CDK's setup stack, if the failed install made it, kept and used), and the account is a
  clean install's but for what the uninstall said AWS keeps (E1's convergence half).
* **The computer stops dead** just after j's call has done its work and before the
  installer reads its answer (a SIGKILL to the installer, harness effect kill_session):
  nothing it would have recorded after the call exists, its lock and tmp/ are left as they
  were. Then uninstall: exit 0, "Not explained: none", the account the account before the
  install plus exactly the kept list (E3's SIGKILL half, with E2).

The record of the account is taken from the fake's state in one process (stub_sweep
"state"), the same files the sweep library writes (test_state_sweep.py), and compared by
the installer's own verdict. The runs go in parallel, each with its own fake account.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.tests.installer_sweeps.harness import accounts
from backend.tests.installer_sweeps.harness.install_world import (
    CLEAN_WRITES,
    InstallWorld,
    in_parallel,
    is_write,
)
from backend.tests.installer_sweeps.test_uninstall_run import (
    NOTHING_EXPLAINED,
    account_against,
    baseline_files,
    kept_list_is_the_residue,
    report_lines,
)

pytestmark = pytest.mark.unit

SUCCESS = f"Controlvar {accounts.RELEASE} (beta) is installed and answering at:"
#: How many changes a clean install makes: each is failed, then cut short, in turn.
WRITES = 18


def _specs(clean_calls: list[dict]) -> list[tuple[str, dict]]:
    """(a name, the match) for each change of the clean run, in order: the service and
    operation, and which of that operation's calls it is."""
    seen: dict[tuple, int] = {}
    out = []
    for call in clean_calls:
        if not is_write(call):
            continue
        key = (call["service"], call["op"])
        seen[key] = seen.get(key, 0) + 1
        out.append(
            (
                f"{len(out) + 1:02d}-{call['service']}-{call['op'].split(' ')[0]}-{seen[key]}",
                {"service": call["service"], "op": call["op"], "nth": seen[key]},
            )
        )
    return out


def _normal(files: dict[str, list[str]]) -> dict[str, list[str]]:
    """A record with what a re-install makes anew in another name set aside: the KMS keys'
    and aliases' key IDs, and the tagging index (whose lines are lag)."""
    out = {}
    for name, lines in files.items():
        if name.endswith("tagged-resources.txt"):
            continue
        if name.endswith("kms-keys.txt"):
            lines = [line.split("\t", 1)[-1] for line in lines]
        if name.endswith("kms-aliases.txt"):
            lines = [line.split("\t", 1)[0] for line in lines]
        out[name] = sorted(lines)
    return out


class HalfBuilt:
    def __init__(self, root: Path, shapes: Path) -> None:
        self.root, self.shapes = root, shapes
        clean = self.world("clean")
        done = clean.install()
        assert done.code == 0 and clean.unknown == "", (done.err, clean.unknown)
        assert clean.writes() == CLEAN_WRITES
        self.specs = _specs(clean.calls)
        self.clean_final = _normal(clean.sweep_files())
        self.before = baseline_files(shapes)
        self.failed = dict(
            in_parallel(
                [lambda n=n, s=s: (n, self.fail_at(n, s)) for n, s in self.specs]
            )
        )
        self.killed = dict(
            in_parallel(
                [lambda n=n, s=s: (n, self.kill_after(n, s)) for n, s in self.specs]
            )
        )

    def world(self, name: str) -> InstallWorld:
        return InstallWorld(
            self.root / name, accounts.install_target(), self.shapes, stub_sweep="state"
        )

    def fail_at(self, name: str, spec: dict) -> dict:
        world = self.world(f"fail-{name}")
        world.fault(**spec, mode="AccessDenied")
        first = world.install()
        world.clear_logs()
        second = world.install()
        second_calls = world.calls
        world.edit(lambda s: s.update(faults=[]))
        world.clear_logs()
        uninstall = world.uninstall()
        residue = kept_list_is_the_residue(world, self.before)
        kept = report_lines(world, "kept") | report_lines(world, "lag")
        state = world.state
        toolkit = any(
            s["StackName"] == "CDKToolkit" and s["StackStatus"] != "DELETE_COMPLETE"
            for s in state["regions"]["us-west-2"].get("stacks", [])
        )
        role = any(
            r["RoleName"] == "AWSServiceRoleForECS" for r in state["global"]["roles"]
        )
        world.clear_logs()
        third = world.install()
        return {
            "world": world,
            "first": first,
            "second": second,
            "second_calls": second_calls,
            "uninstall": uninstall,
            "residue": residue,
            "kept": kept,
            "toolkit": toolkit,
            "role": role,
            "third": third,
            "third_writes": world.writes(),
            "final": _normal(world.sweep_files()),
        }

    def kill_after(self, name: str, spec: dict) -> dict:
        world = self.world(f"kill-{name}")
        world.effect(**spec, kill_session=True)
        first = world.install()
        world.clear_logs()
        uninstall = world.uninstall()
        return {
            "world": world,
            "first": first,
            "uninstall": uninstall,
            "residue": kept_list_is_the_residue(world, self.before),
        }


@pytest.fixture(scope="module")
def half(tmp_path_factory, shapes_file) -> HalfBuilt:
    return HalfBuilt(tmp_path_factory.mktemp("uninstall-halfbuilt"), shapes_file)


def test_every_change_of_a_clean_install_is_cut_short_in_turn(half):
    """W pinned: a change install adds or drops changes this number."""
    assert len(half.specs) == WRITES, [n for n, _ in half.specs]


def test_after_any_failed_change_a_second_install_refuses(half):
    """E1, the half that needs no uninstall: whichever change fails, the install stops
    (exit 1) and a second install over its record refuses (UX-B9, exit 2) and calls
    nothing at all."""
    wrong = {}
    for name, run in half.failed.items():
        problems = []
        if run["first"].code != 1:
            problems.append(f"the failed install exited {run['first'].code}")
        if (
            run["second"].code != 2
            or "This computer holds the record of an install" not in run["second"].err
        ):
            problems.append(
                f"the second install did not refuse: exit {run['second'].code}, {run['second'].err[:200]}"
            )
        if run["second_calls"]:
            problems.append(
                f"the second install called {[(c['service'], c['op']) for c in run['second_calls']]}"
            )
        if problems:
            wrong[name] = problems
    assert wrong == {}, wrong


def test_uninstall_from_any_failed_change_leaves_only_the_kept_list(half):
    """E2: after a failed change j, uninstall ends the account as it was before the install
    plus exactly the kept list it printed, its report ending "Not explained: none"."""
    wrong = {}
    for name, run in half.failed.items():
        done = run["uninstall"]
        if done.code != 0 or NOTHING_EXPLAINED not in done.out:
            wrong[name] = f"exit {done.code}: {done.err[:400]} {done.out[-400:]}"
        elif run["residue"]:
            wrong[name] = run["residue"]
        elif run["world"].unknown:
            wrong[name] = run["world"].unknown
    assert wrong == {}, "\n".join(f"{k}: {v}" for k, v in wrong.items())


def test_after_an_uninstall_a_reinstall_converges(half):
    """E1's convergence half: fail at j, uninstall, install: it installs with the clean
    run's changes (what the uninstall kept is used, not made again: the CDK's setup stack
    the failed install made, and the ECS service-linked role AWS keeps), and the account
    is a clean install's but for what the uninstall said AWS keeps."""
    wrong = {}
    for name, run in half.failed.items():
        done = run["third"]
        want = [
            w
            for w in CLEAN_WRITES
            if not (w == ("cdk", "bootstrap") and run["toolkit"])
            and not (w == ("iam", "create-service-linked-role") and run["role"])
        ]
        if done.code != 0 or SUCCESS not in done.out:
            wrong[name] = f"exit {done.code}: {done.err[:400]}"
            continue
        if run["third_writes"] != want:
            wrong[name] = f"changes {run['third_writes']}"
            continue
        kept = _normal(
            {
                f: [line for (g, _sign, line) in run["kept"] if g == f]
                for f in {g for g, _s, _l in run["kept"]}
            }
        )
        allowed = {(f, line) for f, lines in kept.items() for line in lines}
        differ = {
            (f, line)
            for (f, _sign, line) in account_against(half.clean_final, run["final"])
        }
        if differ - allowed:
            wrong[name] = f"not a clean install's: {sorted(differ - allowed)}"
    assert wrong == {}, "\n".join(f"{k}: {v}" for k, v in wrong.items())


def test_uninstall_after_the_computer_stops_dead_after_any_change(half):
    """E3's SIGKILL half, with E2: stopped dead just after change j's call, before its
    record, uninstall still finds what j made (by its name and this install's tag, its
    pending record, or DEPLOY_STARTED) and ends with the account as it was plus exactly
    the kept list."""
    wrong = {}
    for name, run in half.killed.items():
        if run["first"].code != -9:
            wrong[name] = f"the install was not stopped dead: exit {run['first'].code}"
            continue
        done = run["uninstall"]
        if done.code != 0 or NOTHING_EXPLAINED not in done.out:
            wrong[name] = f"exit {done.code}: {done.err[:400]} {done.out[-400:]}"
        elif run["residue"]:
            wrong[name] = run["residue"]
        elif run["world"].unknown:
            wrong[name] = run["world"].unknown
    assert wrong == {}, "\n".join(f"{k}: {v}" for k, v in wrong.items())


def test_a_stop_dead_leaves_nothing_recorded_after_its_call(half):
    """The cut is where E3 asks for it: after the call, before its record. A stop after
    the certificate request leaves no CERTIFICATE_ARN; one after the deploy's start has
    DEPLOY_STARTED (written before it) and no DEPLOY_DONE."""
    by_op = {spec["op"]: name for name, spec in half.specs if spec["nth"] == 1}
    certificate = half.killed[by_op["request-certificate"]]["world"]
    deploy = half.killed[by_op["deploy"]]["world"]
    assert not (certificate.record / "env.d" / "CERTIFICATE_ARN.sh").exists()
    assert (deploy.record / "env.d" / "DEPLOY_STARTED.sh").exists()
    assert not (deploy.record / "env.d" / "DEPLOY_DONE.sh").exists()
