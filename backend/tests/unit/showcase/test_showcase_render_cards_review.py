"""The showcase render's cards, caption template and review page (#1066; gates 16 and 17).

Gates 16 and 17 are unit tests on the templates (plan v2, PE condition 12):
the cards name the product and its two addresses exactly as the repository
does, and a caption is drawn at least 36 px high, two lines of it fitting
the band. The review page carries what an approval needs and no needle.
"""

from __future__ import annotations

import re

import pytest
from showcase import contract
from showcase.render import review, templates

from backend.tests.unit.showcase.capture_dirs import REPO_ROOT, make_capture

pytestmark = [pytest.mark.unit]


def visible_text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


# --- Gate 16: the cards ------------------------------------------------------------------


def test_the_cards_addresses_are_the_repositorys_own():
    reuse = (REPO_ROOT / "REUSE.toml").read_text(encoding="utf-8")
    assert f'SPDX-PackageDownloadLocation = "https://{templates.REPOSITORY}"' in reuse
    assert f"<hello@{templates.SITE}>" in reuse
    assert templates.PRODUCT_NAME == "Controlvar"


def test_the_end_card_names_the_product_site_repository_and_version():
    text = visible_text(templates.end_card_html("v0.26.4"))
    for expected in (
        templates.PRODUCT_NAME,
        templates.END_LINE,
        templates.SITE,
        templates.REPOSITORY,
        "Recorded on Controlvar v0.26.4, a local stack with demo data",
    ):
        assert expected in text


def test_the_title_card_names_the_video():
    text = visible_text(templates.title_card_html("Get started"))
    assert (
        "Get started" in text
        and templates.PRODUCT_NAME in text
        and templates.TAGLINE in text
    )


def test_the_mark_is_the_dashboards_own():
    favicon = (REPO_ROOT / "frontend/public/favicon.svg").read_text(encoding="utf-8")
    path = re.search(r'<path d="([^"]+)"', favicon)[1]
    assert f'<path d="{path}"' in templates.MARK_SVG
    assert 'fill="#2563eb"' in favicon and 'fill="#2563eb"' in templates.MARK_SVG


def test_card_text_is_escaped():
    assert "<script>" not in templates.title_card_html("<script>x</script>")


# --- Gate 17: the caption band ---------------------------------------------------------------


def test_captions_are_big_enough_and_two_lines_fit_the_band():
    html = templates.caption_html(("One", "Two"))
    size = int(re.search(r"\.line\{[^}]*font-size:(\d+)px", html)[1])
    line = int(re.search(r"\.line\{[^}]*line-height:(\d+)px", html)[1])
    assert size >= templates.CAPTION_MIN_FONT_PX == 36
    assert (
        contract.MAX_CUE_LINES * line + 2 * templates.CAPTION_PADDING_PX
        <= contract.BAND_H
    )
    assert templates.CAPTION_MAX_WIDTH_PX <= contract.PAGE_W


def test_a_14_px_caption_would_fail_the_minimum(monkeypatch):
    from showcase.render import chromium

    metrics = {
        "lines": 1,
        "font_px": 14,
        "overflowing": 0,
        "widest_px": 400,
        "band_height_px": 90,
    }
    assert chromium.caption_problems(3, metrics, 1) == [
        "cue 3: caption font 14 px, minimum 36 px"
    ]
    metrics.update(font_px=40, overflowing=1, widest_px=1900, lines=3)
    problems = chromium.caption_problems(3, metrics, 2)
    assert "cue 3: drew 3 lines, expected 2" in problems
    assert "cue 3: a line is 1900 px wide; the band allows 1760 px" in problems


def test_caption_and_card_contrast_is_at_least_7_to_1():
    assert (
        templates.contrast_ratio(templates.CAPTION_COLOUR, templates.CAPTION_BACKGROUND)
        >= 7
    )
    assert (
        templates.contrast_ratio(templates.CARD_COLOUR, templates.CARD_BACKGROUND) >= 7
    )
    assert (
        templates.contrast_ratio(templates.CARD_MUTED, templates.CARD_BACKGROUND) >= 7
    )


# --- The review page -------------------------------------------------------------------------


def page(tmp_path):
    capture = contract.validate_capture_dir(make_capture(tmp_path / "capture"))
    names = contract.output_names(capture.sheet.slug, "c" * 64)
    contact = [
        review.ContactFrame(
            number=1,
            start_ms=3200,
            end_ms=7967,
            text="A <b>caption</b>",
            image="cue-01.jpg",
            focus=None,
        )
    ]
    return review.review_html(
        sheet=capture.sheet,
        manifest=capture.manifest,
        names=names,
        mp4_sha256="c" * 64,
        vtt_sha256="d" * 64,
        contact=contact,
        render_gates={"1": {"ok": True, "numbers": {"duration_s": 57.0}}},
    )


def test_the_review_page_carries_what_an_approval_needs(tmp_path):
    html = page(tmp_path)
    assert "c" * 64 in html and "d" * 64 in html
    assert 'src="../01-getting-started-cccccccccccc.mp4"' in html
    assert 'src="../01-getting-started-cccccccccccc.vtt"' in html
    assert html.count('type="checkbox"') == len(review.CHECKLIST) == 5
    assert "cue-01.jpg" in html and "A &lt;b&gt;caption&lt;/b&gt;" in html
    assert "duration_s" in html and "Capture gates" in html
    assert "<script" not in html


def test_holds_needle_finds_a_value_with_or_without_its_separators():
    needles = ["ZEBRA-VALUE-987-ALPHA", "QUOKKA-VALUE-123-BRAVO"]
    assert review.holds_needle("nothing here", needles) == []
    assert review.holds_needle("x ZEBRA-VALUE-987-ALPHA y", needles) == [1]
    assert review.holds_needle("quokka value 123 bravo", needles) == [2]


def test_the_review_page_holds_no_needle(tmp_path):
    from backend.tests.unit.showcase.capture_dirs import NEEDLES

    assert review.holds_needle(page(tmp_path), NEEDLES) == []
