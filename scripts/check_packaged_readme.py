#!/usr/bin/env python3
"""Refuse a package whose README would tell a registry reader something false.

A registry shows the README packed into a version as that version's page for
as long as the version exists, and nothing on main can change it afterwards.
So a README that a registry shows (here: NuGet's ``PackageReadmeFile``) is
written to be true before the publish and after it, for good (DECISIONS
T230, the note on T228's bullet 3). It has:

* no status sentence: neither "not yet published" nor "is on <registry>";
* no install command for one of this repository's packages from a registry,
  anywhere;
* no quote of its own version;
* no relative link (a registry renders the README away from this
  repository, where ``../x.md`` leads nowhere);
* exactly one Install section, which links the SDK's page on GitHub
  (``blob/main``, which says how to install it, today and after the
  publish) and states requirements, and names no install command of any
  kind and holds no fenced block. Instructions for building from a clone
  live under Development.

The check reads the README out of the built artefact, so it judges the bytes
the release would upload. It is a sweep, not a proof: a paraphrase passes,
and review catches the rest.

An SDK the gate has no rules for is refused rather than passed.

Usage::

    python scripts/check_packaged_readme.py --sdk dotnet Controlvar.SDK.0.1.0.nupkg

Exit 0 prints ``<artefact>: README checked``; exit 1 prints each problem.
Standard library only, so the runner's python3 is enough.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

#: The SDKs whose packed README this contract holds, each with the page its
#: Install section links. backend/tests/smoke/test_sdk_release_nuget.py pins
#: that every SDK whose build job runs this gate is named here.
I_README: dict[str, str] = {
    "dotnet": "docs/sdk/dotnet.md",
}
PAGE_URL = "https://github.com/Controlvar/controlvar/blob/main/"

#: A registry's name, as a status sentence spells it.
REGISTRY = r"(?:npm|pypi|nuget|maven central)"
#: Status sentences, matched on the folded text with markup and link targets
#: removed, case ignored. "Not yet published" is ``says_unpublished``.
STATUS = (
    re.compile(
        r"\b(?:is|are) (?:now )?(?:on|available on|published (?:on|to)) "
        + REGISTRY
        + r"\b"
    ),
    re.compile(
        r"\b(?:no release on|not (?:yet )?(?:on|published (?:on|to))) "
        + REGISTRY
        + r"\b"
    ),
)

#: Install commands that name one of this repository's packages on a
#: registry. Refused anywhere in the README. The .NET 10 SDK adds noun-first
#: commands (`dotnet package add`) and file-based apps (`#:package`).
REGISTRY_INSTALLS = (
    re.compile(
        r"\bdotnet[ \t]{1,10}add\b[^\n`]{0,200}?\bpackage[ \t]{1,10}Controlvar\.",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdotnet[ \t]{1,10}package[ \t]{1,10}add[ \t]{1,10}Controlvar\.",
        re.IGNORECASE,
    ),
    re.compile(r"#:package[ \t]{1,10}Controlvar\.", re.IGNORECASE),
    re.compile(r"\bInstall-Package[ \t]{1,10}Controlvar\.", re.IGNORECASE),
    re.compile(r"\bpaket[ \t]{1,10}add[ \t]{1,10}Controlvar\.", re.IGNORECASE),
    re.compile(
        r"<PackageReference\b[^>\n]{0,200}\bInclude=\"Controlvar\.", re.IGNORECASE
    ),
    re.compile(
        r"\b(?:npm[ \t]{1,10}(?:install|i|add)|yarn[ \t]{1,10}add|pnpm[ \t]{1,10}add|bun[ \t]{1,10}add)"
        r"\b[^\n`;&|]{0,300}?@controlvar/"
    ),
    re.compile(
        r"\bpip3?[ \t]{1,10}install\b[^\n`;&|]{0,300}?(?<![\w./-])controlvar(?:-openfeature)?(?![\w/-])"
    ),
)

#: Install commands of any kind, refused in the Install section: from a
#: registry, from a clone, from a tarball or from a path.
INSTALL_SECTION_REFUSED = (
    ("npm install", re.compile(r"\bnpm[ \t]{1,10}(?:install|i)\b")),
    ("npm ci", re.compile(r"\bnpm[ \t]{1,10}ci\b")),
    ("npm pack", re.compile(r"\bnpm[ \t]{1,10}pack\b")),
    ("yarn add", re.compile(r"\byarn[ \t]{1,10}add\b")),
    ("pip install", re.compile(r"\bpip3?[ \t]{1,10}install\b")),
    (
        "dotnet add package",
        re.compile(r"\bdotnet[ \t]{1,10}add\b[^\n]{0,200}?\bpackage\b"),
    ),
    (
        "dotnet add reference",
        re.compile(r"\bdotnet[ \t]{1,10}add\b[^\n]{0,200}?\breference\b"),
    ),
    (
        "dotnet package add",
        re.compile(r"\bdotnet[ \t]{1,10}package[ \t]{1,10}add\b"),
    ),
    (
        "dotnet reference add",
        re.compile(r"\bdotnet[ \t]{1,10}reference[ \t]{1,10}add\b"),
    ),
    ("#:package", re.compile(r"#:package\b")),
    ("#:project", re.compile(r"#:project\b")),
    ("Install-Package", re.compile(r"\bInstall-Package\b")),
    ("<PackageReference", re.compile(r"<PackageReference\b")),
    ("<ProjectReference", re.compile(r"<ProjectReference\b")),
    ("<dependency>", re.compile(r"<dependency>")),
    (".tgz", re.compile(r"\.tgz\b")),
    ("git clone", re.compile(r"\bgit[ \t]{1,10}clone\b")),
    ("--prefix", re.compile(r"--prefix\b")),
    ("/path/to", re.compile(r"/path/to\b")),
)

FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]")
INSTALL_HEADING = re.compile(r"^ {0,3}#{2,4}[ \t]{1,40}(?:Install(?:ation)?|Setup)\b")
FENCED_BLOCK = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1[`~]*[ \t]*$", re.S | re.M)
LINK_TARGETS = (
    re.compile(r"\]\(\s*<?([^()\s<>]+)"),  # [text](target) and ![alt](src)
    re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*<?([^\s<>]+)", re.M),  # [ref]: target
    re.compile(r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))"""),
)
ABSOLUTE = re.compile(r"(?:https?|mailto):", re.I)
MARKDOWN_LINK = re.compile(r"\[([^\]\n]{0,200})\]\([^()\n]{0,500}\)")


def fold(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def says_unpublished(text: str) -> bool:
    """ "Not yet published" in any markup or wrapping (as test_sdk_doc_versions)."""
    return "not yet published" in fold(re.sub(r"[*_`]", "", text)).lower()


def relative_links(text: str) -> list[str]:
    """Every link target that is not an absolute URL, outside fenced blocks."""
    text = FENCED_BLOCK.sub("", text)
    targets = []
    for pattern in LINK_TARGETS:
        for found in pattern.findall(text):
            target = found if isinstance(found, str) else next(p for p in found if p)
            if not ABSOLUTE.match(target):
                targets.append(target)
    return targets


def install_sections(text: str) -> list[str]:
    """The text of each Install (Installation, Setup) section: from its
    heading to the next heading of the same or a higher level, with headings
    inside fenced blocks ignored."""
    lines = text.split("\n")
    sections: list[str] = []
    current: list[str] | None = None
    level = 0
    fence = ""
    for line in lines:
        marker = FENCE_LINE.match(line)
        if fence:
            if (
                marker
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= len(fence)
            ):
                fence = ""
        elif marker:
            fence = marker.group(1)
        else:
            heading = HEADING.match(line)
            if heading and current is not None and len(heading.group(1)) <= level:
                sections.append("\n".join(current))
                current = None
            if heading and INSTALL_HEADING.match(line):
                current = []
                level = len(heading.group(1))
                continue
        if current is not None:
            current.append(line)
    if current is not None:
        sections.append("\n".join(current))
    return sections


def own_version_quotes(text: str, version: str) -> list[str]:
    """Where the README quotes ``version``, or its ``vX.Y`` short form."""
    parts = version.split(".")
    patterns = [r"(?<![\w.])v?" + re.escape(version) + r"(?![\w]|\.\d)"]
    if len(parts) >= 2:
        patterns.append(
            r"(?<![\w.])v" + re.escape(".".join(parts[:2])) + r"(?![\w]|\.\d)"
        )
    folded = fold(text)
    found = []
    for pattern in patterns:
        for match in re.finditer(pattern, folded):
            found.append(folded[max(0, match.start() - 40) : match.end() + 20])
    return found


def problems(sdk: str, text: str, version: str) -> list[str]:
    """Each way the README ``text`` of ``sdk`` at ``version`` breaks the contract."""
    if sdk not in I_README:
        return [
            f"sdk/{sdk} is not classified: this gate has no rules for its README "
            "(scripts/check_packaged_readme.py, I_README)"
        ]
    found = []
    plain = fold(MARKDOWN_LINK.sub(r"\1", re.sub(r"[*_`]", "", text))).lower()
    if says_unpublished(text):
        found.append("the README says the package is not yet published")
    for pattern in STATUS:
        for match in pattern.finditer(plain):
            found.append(
                f"the README states whether the package is on a registry: {match.group(0)!r}"
            )
    for pattern in REGISTRY_INSTALLS:
        for match in pattern.finditer(text):
            found.append(
                f"the README installs this repository's package from a registry: {match.group(0)!r}"
            )
    for quote in own_version_quotes(text, version):
        found.append(f"the README quotes its own version {version}: {quote!r}")
    links = relative_links(text)
    if links:
        found.append(
            f"the README links relative paths, which lead nowhere on a registry: {links}"
        )

    sections = install_sections(text)
    if len(sections) != 1:
        found.append(
            f"the README must have exactly one Install section; it has {len(sections)}"
        )
    for section in sections:
        if FENCED_BLOCK.search(section):
            found.append("the Install section holds a fenced block")
        for label, pattern in INSTALL_SECTION_REFUSED:
            match = pattern.search(section)
            if match:
                found.append(
                    f"the Install section names an install command ({label}): {match.group(0)!r}"
                )
        page = re.escape(PAGE_URL + I_README[sdk])
        if not re.search(r"\]\(\s*<?" + page + r"(?:#[\w-]{1,80})?>?\s*\)", section):
            found.append(
                f"the Install section does not link {PAGE_URL + I_README[sdk]}"
            )
    return found


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_nupkg(path: Path) -> tuple[str, str]:
    """Return ``(README text, version)`` from a .nupkg: the file its nuspec's
    ``<readme>`` names, and the nuspec's ``<version>``."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        nuspecs = [n for n in names if "/" not in n and n.endswith(".nuspec")]
        if len(nuspecs) != 1:
            raise ValueError(
                f"expected one .nuspec at the package root, found {nuspecs}"
            )
        metadata = {
            _local(element.tag): (element.text or "").strip()
            for element in ET.fromstring(archive.read(nuspecs[0])).iter()
        }
        readme = metadata.get("readme", "")
        if not readme:
            raise ValueError(
                "the nuspec names no <readme>, so the registry would show no README "
                "(PackageReadmeFile in the project)"
            )
        entry = PurePosixPath(readme.replace("\\", "/")).as_posix()
        if entry not in names:
            raise ValueError(
                f"the nuspec names the README {readme!r}, which the package does not hold"
            )
        return archive.read(entry).decode("utf-8"), metadata.get("version", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--sdk", required=True, help="Directory name under sdk/, e.g. dotnet."
    )
    parser.add_argument("artefact", type=Path, help="The built package (.nupkg).")
    args = parser.parse_args(argv)

    if args.sdk not in I_README:
        print(
            f"error: {args.artefact}: {problems(args.sdk, '', '')[0]}", file=sys.stderr
        )
        return 1
    if args.artefact.suffix != ".nupkg":
        print(f"error: {args.artefact}: this gate reads a .nupkg", file=sys.stderr)
        return 1
    try:
        text, version = read_nupkg(args.artefact)
    except (
        OSError,
        ValueError,
        zipfile.BadZipFile,
        ET.ParseError,
        UnicodeDecodeError,
    ) as error:
        print(f"error: {args.artefact}: {error}", file=sys.stderr)
        return 1
    if not version:
        print(f"error: {args.artefact}: the nuspec has no <version>", file=sys.stderr)
        return 1
    found = problems(args.sdk, text, version)
    for problem in found:
        print(f"error: {args.artefact}: {problem}", file=sys.stderr)
    if found:
        return 1
    print(f"{args.artefact}: README checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
