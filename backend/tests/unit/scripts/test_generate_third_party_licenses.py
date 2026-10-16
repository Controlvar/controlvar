"""`scripts/generate_third_party_licenses.py` — the dependency licence report.

The report is what a reader checks before trusting that nothing in the tree is
incompatible with distributing an Apache-2.0 work, so a wrong flag is worse
than no flag.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[4] / "scripts" / "generate_third_party_licenses.py"
)


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("gen_third_party", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_third_party"] = module
    spec.loader.exec_module(module)
    return module


class TestFlagsFor:
    @pytest.mark.parametrize(
        "lic",
        ["LGPL", "LGPL-2.1", "LGPL-3.0-or-later", "GNU Lesser General Public (LGPL)"],
    )
    @pytest.mark.regression
    def test_lgpl_is_weak_copyleft_not_strong(self, script, lic):
        """`"GPL" in lic.lower()` is true of every LGPL string.

        psycopg2 is LGPL; reporting it as strong copyleft that "must not
        ship" sends the reader after a problem that does not exist.
        """
        flag = script.flags_for(lic)
        assert flag is not None
        assert "weak copyleft" in flag
        assert "strong copyleft" not in flag

    @pytest.mark.parametrize("lic", ["AGPL-3.0", "AGPL-3.0-only", "AGPL"])
    def test_agpl_gets_the_agpl_note(self, script, lic):
        assert script.flags_for(lic) == script.COPYLEFT_FLAGS[0][1]

    @pytest.mark.parametrize("lic", ["GPL-2.0", "GPL-3.0-or-later", "GPL"])
    def test_gpl_still_gets_the_gpl_note(self, script, lic):
        flag = script.flags_for(lic)
        assert flag is not None
        assert "strong copyleft" in flag
        assert "must not ship" in flag
        assert "network-use" not in flag  # that is the AGPL note

    @pytest.mark.parametrize(
        "lic", ["MIT", "Apache-2.0", "BSD-3-Clause", "ISC", "MPL-2.0", "PSF-2.0"]
    )
    def test_permissive_licences_are_not_flagged(self, script, lic):
        assert script.flags_for(lic) is None

    def test_an_expression_is_searched_not_compared(self, script):
        assert script.flags_for("Apache-2.0 OR MIT") is None
        assert script.flags_for("MIT OR GPL-2.0") is not None

    @pytest.mark.parametrize(
        "lic,fragment",
        [
            ("SSPL-1.0", "NOT an open-source licence"),
            ("BUSL-1.1", "source-available"),
            ("CDDL-1.0", "weak copyleft"),
            ("MS-PL", "separate artefact"),
            ("UNKNOWN", "resolved by hand"),
            ("UNLICENSED", "no licence declared"),
        ],
    )
    def test_the_other_flags_still_fire(self, script, lic, fragment):
        assert fragment in script.flags_for(lic)


class TestNormalise:
    def test_pypi_classifier_names_become_spdx_ids(self, script):
        assert script.normalise("MIT License") == "MIT"
        assert script.normalise("Apache Software License") == "Apache-2.0"
        assert (
            script.normalise("GNU Library or Lesser General Public License (LGPL)")
            == "LGPL"
        )

    def test_an_unknown_name_is_passed_through_trimmed(self, script):
        assert script.normalise("  Zlib  ") == "Zlib"


class TestDeclaredDependencies:
    @pytest.mark.regression
    def test_the_python_section_is_the_closure_of_the_pins_not_the_venv(self, script):
        """The committed report once listed black, flake8 and isort -- tools the
        venv still held after ruff replaced them -- under a header claiming
        the pinned set. A legal document that lists a dependency set must
        list *that* set."""
        declared = script.declared_python_dependencies()
        assert "fastapi" in declared
        assert "sqlalchemy" in declared
        # Transitive: pulled in by fastapi/starlette, not pinned directly.
        assert "starlette" in declared
        assert "anyio" in declared
        for leftover in ("black", "flake8", "isort", "pycodestyle", "pyflakes"):
            assert leftover not in declared, leftover

    def test_canonical_names(self, script):
        assert script._canonical("Typing_Extensions") == "typing-extensions"
        assert script._canonical("ruamel.yaml") == "ruamel-yaml"

    @pytest.mark.modules
    def test_the_modules_lock_pins_sqlglot(self, script):
        """The warehouse query builder's parser ships in the full image
        (modules/requirements.lock, absent from a core build). Read from the
        lock itself, so it holds in a job that installs only the core pins."""
        assert "sqlglot" in script._declared_pins()

    @pytest.mark.modules
    def test_the_shipped_modules_closure_includes_sqlglot(self, script):
        """The closure walks installed distributions only, so this half needs
        the modules' pins installed (the Release Gate installs the core set)."""
        pytest.importorskip("sqlglot")
        assert "sqlglot" in script.declared_python_dependencies()


class TestBlankLicence:
    """A package whose licence metadata comes back empty is refused, not
    written as a nameless row (sqlglot declares only License-Expression)."""

    DECLARED = {"sqlglot", "fastapi"}

    def test_an_expression_only_package_reads_as_its_licence(self, script):
        rows = script.python_rows_from(
            [
                {"Name": "sqlglot", "Version": "30.20.0", "License": "MIT"},
                {"Name": "fastapi", "Version": "1", "License": "MIT License"},
                {"Name": "not-shipped", "Version": "1", "License": ""},
            ],
            self.DECLARED,
        )
        assert rows == [("sqlglot", "30.20.0", "MIT"), ("fastapi", "1", "MIT")]

    @pytest.mark.parametrize("blank", ["", "  ", "UNKNOWN", None])
    def test_a_blank_licence_is_refused_and_named(self, script, blank):
        with pytest.raises(SystemExit) as refused:
            script.python_rows_from(
                [{"Name": "sqlglot", "Version": "30.20.0", "License": blank}],
                self.DECLARED,
            )
        assert "sqlglot 30.20.0" in str(refused.value)


# ---------------------------------------------------------------------------
# The gate itself (#170, #215 review)
#
# About 400 lines of guard, lock parsing and --check shipped with no test at
# all; the pull request offered its own first CI run as the tamper, which
# exercises staleness and nothing else. These cover the parts that decide
# whether a wrong document can be written or a right one reported stale.
# ---------------------------------------------------------------------------


class TestTheLockParser:
    """`_lock_pins` reads `uv pip compile --universal --generate-hashes`."""

    def test_it_reads_a_hashed_pin(self, script, tmp_path):
        lock = tmp_path / "x.lock"
        lock.write_text(
            "# a comment\n"
            "alembic==1.19.2 \\\n"
            "    --hash=sha256:aaa \\\n"
            "    --hash=sha256:bbb\n"
            "    # via -r backend/requirements/runtime.txt\n",
            encoding="utf-8",
        )
        assert script._lock_pins(lock) == {"alembic": "1.19.2"}

    def test_a_marker_that_does_not_hold_is_not_a_dependency(self, script, tmp_path):
        """A `--universal` lock resolves for every platform at once.

        Treating every pin as required reported the Windows-only ones as
        missing and refused to generate -- correctly, given what the guard had
        been told.
        """
        lock = tmp_path / "x.lock"
        lock.write_text(
            "colorama==0.4.6 ; sys_platform == 'win32' \\\n"
            "    --hash=sha256:aaa\n"
            "requests==2.32.3 \\\n"
            "    --hash=sha256:bbb\n",
            encoding="utf-8",
        )
        pins = script._lock_pins(lock)
        assert "requests" in pins
        assert "colorama" not in pins, (
            "a marker that does not hold is a dependency of a platform this "
            "image is not, and must not be required"
        )

    def test_a_marker_that_does_hold_is_required(self, script, tmp_path):
        lock = tmp_path / "x.lock"
        lock.write_text(
            'anyio==4.15.1 ; os_name == "posix" or os_name == "nt" \\\n'
            "    --hash=sha256:aaa\n",
            encoding="utf-8",
        )
        assert script._lock_pins(lock) == {"anyio": "4.15.1"}

    @pytest.mark.regression
    def test_an_unreadable_marker_refuses_instead_of_skipping(self, script, tmp_path):
        lock = tmp_path / "x.lock"
        lock.write_text("foo==1.0 ; not_a_marker ~~ 3 \\\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            script._lock_pins(lock)
        assert "marker" in str(exc.value)

    @pytest.mark.regression
    def test_an_unparseable_line_refuses_instead_of_skipping(self, script, tmp_path):
        """Silently skipping is what made the Python section empty once.

        The floor in `environment_problems` is 40 against 83 real pins, so a
        format change that broke the regex for half the closure would still
        have passed it.
        """
        lock = tmp_path / "x.lock"
        lock.write_text("foo @ https://example.invalid/foo.whl\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            script._lock_pins(lock)
        assert "cannot parse" in str(exc.value)


class TestTheEnvironmentGuard:
    @pytest.mark.regression
    def test_a_foreign_architecture_is_refused(self, script, monkeypatch):
        """Linux alone was checked, and sharp ships a binary per ARCH too.

        The LGPL-3.0-or-later rows in the dashboard section are
        `@img/sharp-libvips-linux-x64` and its musl sibling; on Linux/arm64
        the arm64 binaries would silently take their place.
        """
        monkeypatch.setattr(script.platform, "system", lambda: "Linux")
        monkeypatch.setattr(script.platform, "machine", lambda: "aarch64")
        problems = script.environment_problems()
        assert any("aarch64" in p for p in problems), problems

    def test_the_shipping_platform_is_accepted(self, script, monkeypatch):
        monkeypatch.setattr(script.platform, "system", lambda: "Linux")
        monkeypatch.setattr(script.platform, "machine", lambda: "x86_64")
        problems = script.environment_problems()
        assert not any("distribution runs on" in p for p in problems), problems

    @pytest.mark.regression
    def test_too_few_pins_is_reported_as_a_parsing_failure(self, script, monkeypatch):
        monkeypatch.setattr(script, "_declared_pins", lambda: {"one": "1.0"})
        problems = script.environment_problems(allow_foreign_platform=True)
        assert any("parsing failure" in p for p in problems), problems


class TestTheDebugFlagCannotWriteTheDocument:
    """It said so in three places and was true in none of them."""

    @pytest.mark.regression
    def test_allow_foreign_platform_refuses_to_write(self, script, monkeypatch, capsys):
        monkeypatch.setattr(script, "environment_problems", lambda *a, **k: [])
        wrote: list[str] = []
        monkeypatch.setattr(
            script, "render", lambda: (_ for _ in ()).throw(AssertionError("rendered"))
        )

        code = script.main(["--allow-foreign-platform"])

        assert code == 2, "it must refuse before rendering, let alone writing"
        assert "will not write" in capsys.readouterr().err
        assert not wrote

    def test_without_the_flag_a_clean_environment_writes(
        self, script, monkeypatch, tmp_path
    ):
        """The counter-example, so the refusal above cannot pass for free."""
        monkeypatch.setattr(script, "environment_problems", lambda *a, **k: [])
        monkeypatch.setattr(script, "render", lambda: "# generated\n")
        out = tmp_path / "OUT.md"
        monkeypatch.setattr(script, "OUT", out)
        monkeypatch.setattr(script, "ROOT", tmp_path)

        assert script.main([]) == 0
        assert out.read_text(encoding="utf-8") == "# generated\n"


class TestCheckMode:
    def test_only_the_generated_on_date_is_normalised(self, script):
        line = "Generated by `scripts/generate_third_party_licenses.py` on %s.\n"
        same = script._comparable(line % "2026-09-13" + "| `x` | 1.0 |")
        later = script._comparable(line % "2026-09-21" + "| `x` | 1.0 |")
        changed = script._comparable(line % "2026-09-21" + "| `x` | 2.0 |")
        assert same == later, "a regeneration on another day is not a diff"
        assert same != changed, "a version change must still be a diff"

    @pytest.mark.regression
    def test_check_reports_a_stale_document(self, script, monkeypatch, tmp_path):
        monkeypatch.setattr(script, "environment_problems", lambda *a, **k: [])
        monkeypatch.setattr(script, "render", lambda: "| `x` | 2.0 |\n")
        out = tmp_path / "OUT.md"
        out.write_text("| `x` | 1.0 |\n", encoding="utf-8")
        monkeypatch.setattr(script, "OUT", out)
        monkeypatch.setattr(script, "ROOT", tmp_path)

        assert script.main(["--check"]) == 1
        assert out.read_text(encoding="utf-8") == "| `x` | 1.0 |\n", (
            "--check must not write"
        )

    def test_check_passes_on_a_current_document(self, script, monkeypatch, tmp_path):
        monkeypatch.setattr(script, "environment_problems", lambda *a, **k: [])
        monkeypatch.setattr(script, "render", lambda: "| `x` | 1.0 |\n")
        out = tmp_path / "OUT.md"
        out.write_text("| `x` | 1.0 |\n", encoding="utf-8")
        monkeypatch.setattr(script, "OUT", out)
        monkeypatch.setattr(script, "ROOT", tmp_path)

        assert script.main(["--check"]) == 0


class TestFirstPartyPackagesAreNotThirdParties:
    @pytest.mark.regression
    def test_every_published_package_name_is_excluded(self, script):
        own = script._own_package_names()
        assert "@controlvar/js-sdk" in own, sorted(own)
        assert len(own) >= 5, (
            f"only {len(own)} first-party names found; the document listed "
            "five of its own SDKs as third-party software before this"
        )

    def test_the_node_package_list_is_derived_from_the_tree(self, script):
        """Hand-typed, it went stale silently; the sibling gate derives it."""
        import importlib.util

        sibling = _SCRIPT.parent / "check_node_licences.py"
        spec = importlib.util.spec_from_file_location("check_node_licences", sibling)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert [d for d, _ in script.NODE_PACKAGES] == list(module.NODE_PACKAGES), (
            "the two gates must cover the same packages, or one of them is "
            "silently not covering a published SDK"
        )


class TestComponentsCompiledIntoWheels:
    """pip-licenses reports a wheel's own licence, not what is built into it."""

    def test_the_re2_wheel_lists_abseil_and_pybind11(self, script):
        table = script.bundled_table([("google-re2", "1.1.20251105", "BSD")])
        assert "| `abseil-cpp` | `google-re2` 1.1.20251105 | Apache-2.0 |" in table
        assert "| `pybind11` | `google-re2` 1.1.20251105 | BSD-3-Clause |" in table

    def test_nothing_is_listed_for_a_package_that_is_not_shipped(self, script):
        assert script.bundled_table([("fastapi", "0.141.1", "MIT")]) == ""

    def test_the_name_is_compared_canonically(self, script):
        table = script.bundled_table([("Google_RE2", "1.1.20251105", "BSD")])
        assert "abseil-cpp" in table
