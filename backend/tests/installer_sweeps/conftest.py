"""Fixtures for the AWS installer beta's harness tests.

The shapes document is extracted once per session from botocore (harness/shapes.py)
and every fake call checks its response against it. ``baseline`` sweeps the three
accounts of harness/accounts.py once per session, for the tests that only read
those sweeps; a test that changes an account sweeps its own World.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.tests.installer_sweeps.harness import accounts, fake_aws, shapes
from backend.tests.installer_sweeps.harness.world import World


@pytest.fixture(scope="session")
def shapes_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("installer-shapes") / "shapes.json"
    return shapes.write(path, fake_aws.modelled_operations())


class Baseline:
    """One sweep each of the new, installed and uninstalled accounts: ``before``,
    ``mid`` and ``after``, as an install and its uninstall take them."""

    def __init__(self, root: Path, shapes_path: Path) -> None:
        self.worlds = {}
        for label, state in (
            ("before", accounts.new_account()),
            ("mid", accounts.installed_account()),
            ("after", accounts.uninstalled_account()),
        ):
            world = World(root / label, state, shapes_path)
            done = world.sweep(label)
            assert done.code == 0 and world.unknown == "", (label, done, world.unknown)
            self.worlds[label] = world

    def path(self, label: str) -> Path:
        return self.worlds[label].root / "sweep" / label

    def copy(self, label: str, into: World, name: str | None = None) -> Path:
        """The baseline's ``label`` sweep, copied into ``into``'s sweep folder."""
        target = into.root / "sweep" / (name or label)
        shutil.copytree(self.path(label), target)
        return target


@pytest.fixture(scope="session")
def baseline(tmp_path_factory, shapes_file) -> Baseline:
    return Baseline(tmp_path_factory.mktemp("installer-baseline"), shapes_file)


@pytest.fixture
def make_world(tmp_path, shapes_file):
    """``make_world(state)``: a World in this test's scratch folder."""
    made = []

    def make(state: dict, name: str = "world") -> World:
        world = World(tmp_path / name, state, shapes_file)
        made.append(world)
        return world

    yield make
    # A test that leaves an unmodelled call behind has used the fake wrongly,
    # whatever it asserted.
    for world in made:
        assert world.unknown == "", world.unknown
