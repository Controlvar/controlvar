"""The harness's one-process record of a fake account is the sweep library's own.

The uninstall tests record the account through harness/state_sweep.py, not the real
lib/sweep.sh, which would make 131 calls each time. They may do so only while both write
the same files byte for byte: here the real library's sweep and the state sweep are taken
of the same account, and compared file by file, on the new and uninstalled accounts
(harness/accounts.py, the run's measured before and residue) and on the account an install
against the fakes leaves, then its uninstall.
"""

from __future__ import annotations

import json

import pytest

from backend.tests.installer_sweeps.harness import accounts, state_sweep
from backend.tests.installer_sweeps.harness.install_world import InstallWorld
from backend.tests.installer_sweeps.harness.world import World

pytestmark = pytest.mark.unit


def _both(world: World, shapes_file, label: str) -> tuple[dict, dict]:
    done = world.sweep(label)
    assert done.code == 0 and world.unknown == "", (done.err, world.unknown)
    real = world.files(label)
    folder = world.root / "state-sweep" / label
    reads, failed = state_sweep.take(
        world.state, json.loads(shapes_file.read_text()), folder
    )
    assert failed == 0
    assert f"{reads} reads, 0 failed" in done.out, (reads, done.out)
    mine = {
        str(p.relative_to(folder)): p.read_text().splitlines()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }
    return real, mine


@pytest.mark.parametrize(
    "label, state",
    [
        ("new", accounts.new_account),
        ("uninstalled", accounts.uninstalled_account),
    ],
)
def test_the_state_sweep_is_the_librarys_on_the_fixture_accounts(
    make_world, shapes_file, label, state
):
    world = make_world(state(), name=label)
    real, mine = _both(world, shapes_file, label)
    assert sorted(real) == sorted(mine)
    assert {k: v for k, v in mine.items() if real[k] != v} == {}


def test_the_state_sweep_is_the_librarys_through_an_install_and_its_uninstall(
    tmp_path, shapes_file
):
    """The accounts the uninstall tests compare: after an install against the fakes, and
    after its uninstall."""
    world = InstallWorld(
        tmp_path / "w", accounts.install_target(), shapes_file, stub_sweep="state"
    )
    for step, label in (
        (world.install, "installed"),
        (world.uninstall, "uninstalled"),
    ):
        done = step()
        assert done.code == 0, done.err
        real, mine = _both(world, shapes_file, label)
        assert sorted(real) == sorted(mine), label
        assert {k: v for k, v in mine.items() if real[k] != v} == {}, label
    assert world.unknown == ""


def test_the_state_sweep_reads_what_the_library_reads():
    """Every read line of lib/sweep.sh's sweep_take is one the state sweep makes: a line
    the parser stopped seeing would leave a file out of both records alike."""
    text = state_sweep.SWEEP.read_text()
    body = text[text.index("sweep_take() {") :]
    body = body[: body.index("\n}\n")]
    lines = [
        line
        for line in body.splitlines()
        if line.strip().startswith(("_sweep_q ", "_sweep_each "))
    ]
    assert len(state_sweep.reads()) == len(lines) == 68
