"""Gate 7 and the three public values (#1066; the gate-7 matcher plan, rows 1-8, 15 and 17-19).

docker-compose.yml carries three values every Quick Start stack runs with: the two demo
app keys and the SECRET_KEY default (``contract.PUBLIC_VALUES``). Their readable parts are
the apps' own names and words, so gate 7 looks for those three only through their random
part (``ocr.needle_pieces``), and matches every other needle exactly as before.

Every value, cut, look-alike and plant here is built at run time from
``contract.PUBLIC_VALUES`` and ``config.STATIC_NEEDLES``, or made up from a seeded
generator. The page text of rows 1-3 is what OCR read on real frames (copied from the
plan's QA draft). Assertions compare finding texts, counts, offsets and booleans, never a
needle, so a failure prints none.

Rows 1-8 go through ``ocr.findings`` alone and read the values from
``config.STATIC_NEEDLES``, so they also run against the matcher before this change
(9032803e): rows 1, 3 and 5 fail there, and the rest hold.
"""

from __future__ import annotations

import random
import re
import string
from pathlib import Path

import pytest
from showcase import contract
from showcase.capture import config
from showcase.render import encode, ocr, probe, synthetic
from showcase.render.workdir import WorkDir

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
N = contract.normalise
#: The four values every stack carries, in config's order: the demo password, then the
#: three public values.
PUBLIC = tuple(config.STATIC_NEEDLES)
HEX = string.hexdigits[:16]
# No - or _, so raw and normalised positions line up.
ALNUM = string.ascii_letters + string.digits
PASSWORD = "ABCDEFGHJKLMNPQRSTUVWXYZ" + "abcdefghijkmnopqrstuvwxyz" + string.digits[2:]
JWT_HEAD = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"


def hex_key(rng):
    return "eptk_" + "".join(rng.choice(HEX) for _ in range(32))


def jwt(rng):
    body = "".join(rng.choice(ALNUM) for _ in range(110))
    sig = "".join(rng.choice(ALNUM) for _ in range(43))
    return f"{JWT_HEAD}.{body}.{sig}"


def password(rng):
    return "".join(rng.choice(PASSWORD) for _ in range(16))


def gate7(text, needles):
    return [what for gate, what in ocr.findings(text, needles) if gate == "7"]


def needles_with(*kept):
    """needles.txt as the capture writes it: the static values and the kept ones, sorted."""
    return sorted(set(PUBLIC) | set(kept))


def own(needles, value, similarity="1.00"):
    return f"needle {needles.index(value) + 1} of {len(needles)} at {similarity}"


def credited_to_a_static_value(found, needles):
    """The findings that name one of the four static values' needles."""
    return [
        what
        for what in found
        if what.startswith("needle ") and needles[int(what.split()[1]) - 1] in PUBLIC
    ]


def readable_words(readable):
    """A readable part's words without the ``eptk`` key prefix, as a page shows them."""
    return [word for word in re.split(r"[_-]", readable) if word and word != "eptk"]


def public_parts(which):
    """(readable part, random part) of public value ``which`` (0-2), from STATIC_NEEDLES.

    Split where its lowercase hex tail starts, so that rows 1-8 need nothing the
    matcher before this change lacks; row 17 pins that this equals PUBLIC_VALUES.
    """
    match = re.fullmatch(r"(.*[_-])([0-9a-f]+)", PUBLIC[which + 1])
    assert match is not None, f"static needle {which + 2} has no hex tail"
    return match.group(1), match.group(2)


# --- What the matcher refused on real frames, as the OCR read them ---------------------------

#: tesseract's read of V6 take 1, frame 616, verbatim (window-B hand-back, section 3).
FRAME_616 = "\n".join(
    "ShopLab | Home | Products | Search | Cart C1) | Checkout | » | 2 | = | 3 | Order summary | C7) Address | "
    "Shipping | Payment | Ember Camp Stove x 1 | $59.00 | Delivery address | Full name | Subtotal | $59.00 | "
    "Shipping | Free | Total | $59.00 | Email | Street address | City | Postal code | "
    "In ShopLab, set up off camera, this | shopper gets 3 steps.".split(" | ")
)
SEED_FLAG_KEYS = (
    "streampulse_recs_v2",
    "streampulse_player_v2",
    "streampulse_ai_search",
    "streampulse_offline_mode",
    "streampulse_push_frequency",
    "streampulse_wrapped",
    "streampulse_profile_badges",
    "streampulse_onboarding_steps",
    "streampulse_upsell_modal",
)
WORD_FRAMES = [
    FRAME_616,
    "Variant\nvideo_hero\nshoplab_hero_banner\nEvents",
    "StreamPulse\nHome Library Search",
    "StreamPulse is a demo. No audio, no images, no real users",
    "Postal code\nIn ShopLab, a demo storefront, this shopper gets 3 steps.",
    "Analysts see every experiment and flag, and change meither.",
    "Analysts see every experiment and flag, and only change neither.",
    "Settings\nChange me\nSave",
    *[f"Feature Flags\n{key}\nActive 100%" for key in SEED_FLAG_KEYS],
]

PAGE_IDS = [f"page-{i:02d}" for i in range(len(WORD_FRAMES))]


@pytest.mark.regression
@pytest.mark.parametrize("text", WORD_FRAMES, ids=PAGE_IDS)
def test_a_demo_app_s_own_words_are_not_its_key(text):
    """Rows 1 and 2. Refused before at 0.75-1.00 (V6 window B; the v0.36.0 app probe).

    The same page with 12 piece-aligned characters of a kept key added is refused by that
    key's needle alone, so this cannot pass on a matcher that finds nothing.
    """
    rng = random.Random(text)
    key = hex_key(rng)
    needles = needles_with(jwt(rng), jwt(rng), key)
    found = gate7(text, needles)
    assert found == []
    found = gate7(f"{text}\n{key[17:29]}", needles)
    expected = [own(needles, key)]
    assert found == expected


# --- A real secret beside those words is still refused, by its own needle ---------------------

PAGES = [
    "StreamPulse\nDevices",
    "video_hero\nshoplab_hero_banner",
    "Settings\nChange me",
    FRAME_616,
]


@pytest.mark.regression
@pytest.mark.parametrize("page", PAGES, ids=[f"page-{i}" for i in range(len(PAGES))])
@pytest.mark.parametrize("kind", ["api key", "jwt", "password", "server key"])
def test_a_real_secret_beside_the_demo_words_is_refused_by_its_own_needle(page, kind):
    """Row 3: the plant is refused by its own needle, and no static value gets the credit."""
    rng = random.Random(f"{page}{kind}")
    if kind in ("api key", "server key"):
        value = hex_key(rng)
        shown = [value[17:29], value]  # 12 characters (a piece), then whole
    elif kind == "jwt":
        value = jwt(rng)
        sig_at = len(value) - 43
        start = sig_at + (-sig_at) % 4  # a piece boundary inside the signature
        shown = [value[start : start + 12]]
    else:
        value = password(rng)
        shown = [value, value[:12]]
    needles = needles_with(value, jwt(rng), hex_key(rng))
    expected = own(needles, value)
    for part in shown:
        found = gate7(f"{page}\n{part}", needles)
        credited = credited_to_a_static_value(found, needles)
        assert expected in found, (kind, len(part))
        assert credited == [], (kind, len(part))


# --- A needle is public only by its exact value ------------------------------------------------


@pytest.mark.regression
def test_a_real_key_named_like_a_demo_key_keeps_the_full_match():
    """Row 5: a kept value is never matched the public way because it looks like one.

    It is the second demo app key's readable part with another random part, on a page
    that names the app: matched as today, its own needle finds the words at 1.00.
    """
    rng = random.Random(99)
    readable = public_parts(1)[0]
    look_alike = readable + "".join(rng.choice(HEX) for _ in range(28))
    is_static = look_alike in PUBLIC
    assert not is_static
    needles = needles_with(look_alike, jwt(rng), jwt(rng))
    found = gate7("Signed in to the " + "\n".join(readable_words(readable)), needles)
    expected = [own(needles, look_alike)]
    assert found == expected


def test_the_order_of_the_needles_changes_nothing():
    """Row 6: the same findings, needle for needle, in any order of needles.txt."""
    rng = random.Random(5)
    kept = [hex_key(rng), jwt(rng), password(rng)]
    texts = [f"{page}\n{kept[0][17:29]}" for page in PAGES] + WORD_FRAMES
    base = needles_with(*kept)

    def by_needle(found, order):
        """Each finding as (the needle's place in ``base``, its score), or (the finding, '')."""
        return sorted(
            (base.index(order[int(w.split()[1]) - 1]), w.rsplit(" ", 1)[1])
            if w.startswith("needle ")
            else (w, "")
            for w in found
        )

    for _ in range(20):
        shuffled = base[:]
        rng.shuffle(shuffled)
        for number, text in enumerate(texts):
            got = by_needle(gate7(text, shuffled), shuffled)
            expected = by_needle(gate7(text, base), base)
            assert got == expected, number


# --- The public values are still refused when drawn as a key --------------------------------


@pytest.mark.parametrize("which", [1, 2, 3])
def test_a_public_constant_drawn_whole_is_refused_by_its_own_needle(which):
    """Row 7: each public value drawn whole is refused by its own needle."""
    needles = needles_with(jwt(random.Random(which)), hex_key(random.Random(which)))
    expected = own(needles, PUBLIC[which])
    found = gate7(f"Settings\nKey {PUBLIC[which]}\nSave", needles)
    assert expected in found


# --- Row 8: what is still refused for a public value, one test per guarantee ------------------

#: Three made-up kept keys, so the public values' needle numbers are not 1 of 1.
KEPT = [hex_key(random.Random(f"kept-{i}")) for i in range(3)]


def static_and_kept():
    return sorted(set(PUBLIC) | set(KEPT))


def own_findings(text, value, needles):
    number = needles.index(value) + 1
    return [w for w in gate7(text, needles) if w.startswith(f"needle {number} of ")]


def needle_findings(text, needles):
    return [w for w in gate7(text, needles) if w.startswith("needle ")]


@pytest.mark.parametrize("which", [0, 1, 2])
def test_a_public_value_shown_whole_is_refused(which):
    readable, random_part = public_parts(which)
    value = readable + random_part
    found = own_findings(f"Key {value} saved", value, static_and_kept())
    assert found != [], "the whole value is not refused by its own needle"


@pytest.mark.parametrize("which", [0, 1, 2])
def test_any_12_of_a_public_value_s_random_characters_in_a_row_are_refused(which):
    readable, random_part = public_parts(which)
    value, needles = readable + random_part, static_and_kept()
    missed = [
        start
        for start in range(len(random_part) - 11)
        if not own_findings(
            f"Settings\n{random_part[start : start + 12]}\nSave", value, needles
        )
    ]
    assert missed == [], "starts in the random part that were not refused"


#: Literal, hand-derived: where each kept 12-character piece starts inside the random part
#: (the normalised starts 16, 20, 24, 28, 32, 35 less 15, and 20 ... 35 less 19).
KEPT_12 = {0: [1, 5, 9, 13, 17, 20], 1: [1, 5, 9, 13, 16], 2: [1, 5, 9, 13, 17, 20]}
#: Three misreads in 12, at either end and in the middle, and spread out.
MISREADS = [(0, 5, 11), (1, 2, 3), (9, 10, 11), (0, 4, 8), (3, 7, 11)]


@pytest.mark.parametrize("which", [0, 1, 2])
def test_a_kept_12_character_piece_with_3_misreads_is_refused(which):
    readable, random_part = public_parts(which)
    value, needles = readable + random_part, static_and_kept()
    missed = []
    for start in KEPT_12[which]:
        for at in MISREADS:
            run = list(random_part[start : start + 12])
            for i in at:
                run[i] = "#"
            if not own_findings(f"Code {''.join(run)} done", value, needles):
                missed.append((start, at))
    assert missed == [], "kept pieces with 3 misreads that were not refused"


@pytest.mark.parametrize("which", [0, 1])
def test_a_demo_app_key_s_readable_part_is_still_key_shaped(which):
    readable, _random_part = public_parts(which)
    found = gate7(f"Key {readable}", static_and_kept())
    assert "key-shaped text (eptk and 5 or more characters)" in found


#: How many of its random characters must follow a public value's readable part (without
#: the ``eptk`` prefix) for the value's own needle to refuse the frame (measured: PE P11).
BOUNDARY = {0: 9, 1: 8, 2: 10}


@pytest.mark.parametrize("which", [0, 1, 2])
def test_the_readable_part_and_a_few_random_characters_are_refused_only_from_the_boundary(
    which,
):
    """Refused from ``BOUNDARY`` random characters on, and by no needle below it."""
    readable, random_part = public_parts(which)
    value, needles = readable + random_part, static_and_kept()
    words = readable[len("eptk_") :] if readable.startswith("eptk_") else readable
    k = BOUNDARY[which]
    below = [
        shown
        for shown in range(k)
        if needle_findings(f"Label {words}{random_part[:shown]} more", needles)
    ]
    from_it = [
        shown
        for shown in range(k, len(random_part) + 1)
        if not own_findings(f"Label {words}{random_part[:shown]} more", value, needles)
    ]
    assert below == [], "refused with fewer random characters than the boundary"
    assert from_it == [], "not refused at or past the boundary"


# --- Row 4: every other needle is matched exactly as before (a frozen copy) -------------------
# The matcher as of blob 9032803e (render/ocr.py, unchanged since #1077), verbatim but
# renamed. It is vendored, not read with git: core-build runs these tests with no .git.


def _old_best_distance(pattern, text):
    m = len(pattern)
    if m == 0:
        return 0
    peq = {}
    for i, char in enumerate(pattern):
        peq[char] = peq.get(char, 0) | (1 << i)
    mask = (1 << m) - 1
    high = 1 << (m - 1)
    pv, mv, score, best = mask, 0, m, m
    for char in text:
        eq = peq.get(char, 0)
        xv = eq | mv
        xh = ((((eq & pv) + pv) & mask) ^ pv) | eq
        ph = (mv | ~(xh | pv)) & mask
        mh = pv & xh
        if ph & high:
            score += 1
        elif mh & high:
            score -= 1
        ph = (ph << 1) & mask
        mh = (mh << 1) & mask
        pv = (mh | ~(xv | ph)) & mask
        mv = ph & xv
        if score < best:
            best = score
    return best


def _old_windows(needle, size, step):
    starts = list(range(0, len(needle) - size + 1, step))
    if starts[-1] != len(needle) - size:
        starts.append(len(needle) - size)
    return [needle[s : s + size] for s in starts]


def _old_chunks(n):
    pieces = [n] if len(n) <= 24 else _old_windows(n, 24, 8)
    if len(n) > 12:
        pieces += _old_windows(n, 12, 4)
    return list(dict.fromkeys(pieces))


#: Frozen with the copy: the oracle reads nothing from the code under test.
_OLD_THRESHOLD = 0.7


def _old_similarity(n, t):
    return max(1 - _old_best_distance(p, t) / len(p) for p in _old_chunks(n))


def _old_findings(text, needle):
    """What the frozen matcher reports for one needle alone: [] or its one finding."""
    old = _old_similarity(N(needle), N(text))
    return [f"needle 1 of 1 at {old:.2f}"] if old >= _OLD_THRESHOLD else []


WORDS = "Experiments Flags Active Draft variants users Started Settings Key Created Devices Home".split()


def _case(rng):
    """One seeded case. A look-alike is a demo app key's readable part, another random part."""
    kind = rng.choice(["api key", "jwt", "password", "random", "look-alike"])
    needle = {
        "api key": lambda: hex_key(rng),
        "jwt": lambda: jwt(rng),
        "password": lambda: password(rng),
        "random": lambda: "".join(
            rng.choice(ALNUM + "-_") for _ in range(rng.randint(8, 80))
        ),
        "look-alike": lambda: (
            rng.choice([public_parts(0)[0], public_parts(1)[0]])
            + "".join(rng.choice(HEX) for _ in range(28))
        ),
    }[kind]()
    text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 12)))
    if rng.random() < 0.6:
        start = rng.randint(0, max(0, len(needle) - 8))
        part = list(needle[start : start + rng.randint(8, 40)])
        for _ in range(rng.randint(0, 4)):
            part[rng.randrange(len(part))] = "#"
        text += " " + "".join(part) + " " + rng.choice(WORDS)
    return kind, needle, text


#: How many of the 2,000 seeded cases the old matcher refuses (pinned from a run on 9032803e).
OLD_REFUSED = 940


def test_every_needle_but_the_public_constants_is_matched_exactly_as_before():
    rng = random.Random(1306)
    refused, mismatches = 0, []
    for _ in range(2000):
        kind, needle, text = _case(rng)
        is_static = needle in PUBLIC
        assert not is_static, kind
        expected = _old_findings(text, needle)
        got = needle_findings(text, [needle])
        refused += bool(expected)
        if got != expected:
            mismatches.append((kind, len(needle), expected, got))
    assert mismatches == [], mismatches[:5]
    assert refused == OLD_REFUSED


# --- Row 15: the scan's numbers count the public needles exactly -----------------------------


class FakeEngine(ocr.Engine):
    def __init__(self, name, answer):
        super().__init__(name=name, binary="fake")
        self.answer = answer

    def read(self, paths):
        return self.answer(paths)


def fake_video(monkeypatch, sums):
    """A video whose decoded checksums are ``sums``; frames extract as files named for their index."""
    monkeypatch.setattr(probe, "framemd5", lambda video: list(sums))

    def extract(video, indices, out_dir, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in sorted(set(indices)):
            path = out_dir / f"frame-{index:06d}.png"
            path.write_bytes(b"png")
            paths.append(path)
        return paths

    monkeypatch.setattr(encode, "extract_frames", extract)


def reader_by_frame(texts):
    """An engine that reads each extracted frame's text from ``texts`` by frame index."""
    return FakeEngine(
        "vision",
        lambda paths: {p: texts.get(int(p.stem.split("-")[1]), "") for p in paths},
    )


def scan_numbers(tmp_path, monkeypatch, needles):
    fake_video(monkeypatch, ["a" * 32, "b" * 32])
    engine = reader_by_frame({0: "Experiments", 1: "Feature Flags"})
    numbers, _u4 = ocr.scan(
        tmp_path / "v.mp4",
        2,
        needles,
        [engine],
        WorkDir(tmp_path, needles=tmp_path / "capture" / "needles.txt"),
        lambda: None,
    )
    return numbers


def test_the_scan_counts_seven_needles_and_three_public(tmp_path, monkeypatch):
    numbers = scan_numbers(tmp_path, monkeypatch, static_and_kept())
    assert (numbers["needles"], numbers["public_needles"]) == (7, 3)


def test_a_capture_without_the_public_values_counts_none(tmp_path, monkeypatch):
    numbers = scan_numbers(tmp_path, monkeypatch, sorted(KEPT))
    assert (numbers["needles"], numbers["public_needles"]) == (3, 0)


def test_a_look_alike_is_not_counted_as_public(tmp_path, monkeypatch):
    readable, random_part = public_parts(1)
    rng = random.Random(7)
    look_alike = readable + "".join(rng.choice(HEX) for _ in random_part)
    is_static = look_alike in PUBLIC
    assert not is_static
    numbers = scan_numbers(
        tmp_path, monkeypatch, sorted({*static_and_kept(), look_alike})
    )
    assert (numbers["needles"], numbers["public_needles"]) == (8, 3)


# --- Row 17: the contract's pins -------------------------------------------------------------


def compose_default(text, name):
    """The default docker-compose.yml gives ``name`` (``NAME: ${NAME:-default}``), or None."""
    pattern = rf"^\s*{re.escape(name)}: \$\{{{re.escape(name)}:-([^}}\n]*)\}}\s*$"
    found = re.findall(pattern, text, re.M)
    return found[0] if len(found) == 1 else None


def compose_text():
    return (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_there_are_exactly_three_public_values_each_compose_s_by_name():
    """Each app's demo key, then the SECRET_KEY default: compose's own defaults, by name."""
    names = [f"{app.upper()}_API_KEY" for app in config.APPS] + ["SECRET_KEY"]
    text = compose_text()
    entries = len(
        contract.PUBLIC_VALUES
    )  # counted here: inside an assert it prints them
    assert entries == 3
    same = [
        compose_default(text, name) == readable + random_part
        for name, (readable, random_part) in zip(names, contract.PUBLIC_VALUES)
    ]
    assert same == [True, True, True]


def test_each_public_value_is_a_readable_part_then_24_or_more_hex():
    shapes = [
        (
            bool(re.fullmatch(r".*[_-]", readable)),
            bool(re.fullmatch(r"[0-9a-f]{24,}", random_part)),
        )
        for readable, random_part in contract.PUBLIC_VALUES
    ]
    assert shapes == [(True, True)] * 3


def test_the_public_values_split_where_the_plan_measured():
    """(normalised readable length, random length): what row 18's pieces are derived from."""
    lengths = [(len(N(r)), len(x)) for r, x in contract.PUBLIC_VALUES]
    assert lengths == [(15, 32), (19, 28), (15, 32)]
    assert all(N(r + x) == N(r) + N(x) for r, x in contract.PUBLIC_VALUES)


def test_every_public_value_is_in_docker_compose():
    text = compose_text()
    assert [(r + x) in text for r, x in contract.PUBLIC_VALUES] == [True] * 3


def test_the_static_needles_keep_their_values_and_order():
    """The demo password first, then the three public values in ``PUBLIC_VALUES``' order.

    That is compose's password, then compose's three by name, so a reordering fails
    here even when ``PUBLIC_VALUES`` is reordered with it.
    """
    text = compose_text()
    names = ["FIRST_SUPERUSER_PASSWORD"]
    names += [f"{app.upper()}_API_KEY" for app in config.APPS] + ["SECRET_KEY"]
    count = len(config.STATIC_NEEDLES)
    assert count == 4
    in_order = [
        compose_default(text, name) == value
        for name, value in zip(names, config.STATIC_NEEDLES)
    ]
    assert in_order == [True] * 4
    built = config.STATIC_NEEDLES == (config.DEMO_ADMIN[1],) + tuple(
        r + x for r, x in contract.PUBLIC_VALUES
    )
    assert built, "STATIC_NEEDLES is not the password, then PUBLIC_VALUES in order"
    apps = config.APP_STATIC_KEY == dict(zip(config.APPS, config.STATIC_NEEDLES[1:3]))
    assert apps, "APP_STATIC_KEY no longer names each app's own demo key"
    split = [public_parts(i) == contract.PUBLIC_VALUES[i] for i in range(3)]
    assert split == [True] * 3, "rows 1-8's split differs from PUBLIC_VALUES"


# --- Row 18: the pieces ----------------------------------------------------------------------

#: (size, start in the normalised value) of every kept piece, derived by hand from today's
#: windows of a 47-character needle (24 apart 8 from 0, the last at 23; 12 apart 4 from 0,
#: the last at 35) and the readable parts' normalised lengths 15, 19 and 15.
LITERAL = {
    0: {(24, 16), (24, 23), (12, 16), (12, 20), (12, 24), (12, 28), (12, 32), (12, 35)},
    1: {(24, 23), (12, 20), (12, 24), (12, 28), (12, 32), (12, 35)},
    2: {(24, 16), (24, 23), (12, 16), (12, 20), (12, 24), (12, 28), (12, 32), (12, 35)},
}
#: Distinct pieces: ShopLab's repeating random part makes its 12-character pieces at 16
#: and 32 equal, so it has 7 from 8 starts.
COUNTS = {0: 7, 1: 6, 2: 8}


def test_every_needle_s_pieces_are_a_subset_of_today_s():
    rng = random.Random(18)
    variants = [
        v
        for r, x in contract.PUBLIC_VALUES
        for v in ((r + x).upper(), r + x + " ", r, x, r + x[:-1])
    ]
    fixtures = (
        list(PUBLIC)
        + KEPT
        + variants
        + [
            "".join(rng.choice(HEX + "_-") for _ in range(rng.randint(8, 90)))
            for _ in range(300)
        ]
    )
    bad = [
        i
        for i, v in enumerate(fixtures)
        if N(v) and not set(ocr.needle_pieces(v)) <= set(ocr.chunks(N(v)))
    ]
    assert bad == [], "fixtures whose pieces are not a subset of today's"


@pytest.mark.parametrize("which", [0, 1, 2])
def test_a_public_value_keeps_exactly_the_pieces_inside_its_random_part(which):
    readable, random_part = contract.PUBLIC_VALUES[which]
    n = N(readable + random_part)
    expected = {n[start : start + size] for size, start in LITERAL[which]}
    got = ocr.needle_pieces(readable + random_part)
    length, count, distinct = len(n), len(got), len(set(got))
    assert (length, count, distinct) == (47, COUNTS[which], COUNTS[which])
    same = set(got) == expected
    assert same, "the kept pieces are not the literal oracle's"


def test_a_public_value_that_would_keep_no_piece_keeps_today_s(monkeypatch):
    """The fail-closed path: an entry whose random part leaves no window (read at call time)."""
    rng = random.Random(1801)
    made_up = ("made-up-readable-", "".join(rng.choice(HEX) for _ in range(10)))
    monkeypatch.setattr(contract, "PUBLIC_VALUES", contract.PUBLIC_VALUES + (made_up,))
    needle = made_up[0] + made_up[1]
    assert ocr.needle_pieces(needle) == ocr.chunks(N(needle))
    text = f"Label {made_up[0]} more"
    expected = _old_findings(text, needle)
    assert expected != []
    assert needle_findings(text, [needle]) == expected


# --- Row 19: a public value is recognised by raw equality only --------------------------------


def _variants(value):
    separator = next(c for c in value if c in "_-")
    return {
        "upper case": value.upper(),
        "a trailing space": value + " ",
        "a trailing newline": value + "\n",
        "one separator changed": value.replace(
            separator, "-" if separator == "_" else "_", 1
        ),
    }


@pytest.mark.parametrize("which", [0, 1, 2])
@pytest.mark.parametrize(
    "variant",
    ["upper case", "a trailing space", "a trailing newline", "one separator changed"],
)
def test_a_public_value_in_another_form_is_matched_as_before(which, variant):
    readable, random_part = contract.PUBLIC_VALUES[which]
    needle = _variants(readable + random_part)[variant]
    is_static = needle in PUBLIC
    assert not is_static
    as_today = ocr.needle_pieces(needle) == ocr.chunks(N(needle))
    assert as_today, "a variant of a public value was matched the public way"
    text = "Label " + " ".join(readable_words(readable)) + " more"
    expected = _old_findings(text, needle)
    found = needle_findings(text, [needle])
    assert expected != []
    assert found == expected


# --- Row 13's options in render/synthetic.py (the render itself runs on a Mac) ----------------


def test_the_synthetic_words_page_holds_the_public_values_words_and_no_key():
    lines = synthetic.product_words()
    words = [
        N(line) == N("".join(readable_words(r)))
        for line, (r, _x) in zip(lines, contract.PUBLIC_VALUES)
    ]
    shape = (
        len(lines),
        words,
    )  # computed here: inside an assert, len() prints its list
    assert shape == (3, [True] * 3)
    needles = synthetic.capture_needles(public_needles=True)
    counts = (
        len(needles),
        tuple(needles[2:]) == PUBLIC,
        len(synthetic.capture_needles()),
    )
    assert counts == (6, True, 2)
    page = synthetic.page_html(0, words=lines)
    assert all(f"<div class='words'>{line}</div>" in page for line in lines)
    found = gate7("\n".join(lines), needles)
    assert found == []
