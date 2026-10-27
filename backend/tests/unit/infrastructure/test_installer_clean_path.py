"""The AWS installer beta's clean path, here so that core-build runs it too.

core-build runs backend/tests/unit in a copy of the tree with no .git and no modules/.
One install against the fakes (the fake git archives that copy) must end installed,
make exactly the clean run's changes in order (D1), and leave the clone's bytes as they
were (D9). The fault sweeps and the rest run in backend/tests/installer_sweeps, which
core-build does not collect.
"""

from __future__ import annotations

import pytest

from backend.tests.installer_sweeps.harness import accounts, fake_aws, shapes
from backend.tests.installer_sweeps.harness.install_world import (
    CLEAN_WRITES,
    InstallWorld,
)

pytestmark = pytest.mark.unit


def test_a_clean_install_against_the_fakes(tmp_path):
    shapes_path = shapes.write(tmp_path / "shapes.json", fake_aws.modelled_operations())
    world = InstallWorld(
        tmp_path / "w", accounts.install_target(), shapes_path, stub_sweep=True
    )
    before = world.clone_bytes()
    done = world.install()
    assert done.code == 0, done.err
    assert (
        f"Controlvar {accounts.RELEASE} (beta) is installed and answering at:"
        in done.out
    )
    assert world.writes() == CLEAN_WRITES
    assert world.clone_bytes() == before, "the install changed the clone"
    assert world.unknown == ""
