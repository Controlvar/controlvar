"""The HTML the render draws with Chromium: the caption band, the cards, the OCR canary.

Each template is a pure function of its text, so the unit tests can pin what a
viewer reads (gate 16: the cards name the product and its addresses exactly;
gate 17: captions are big enough and two lines fit the band) without a
browser. ``chromium.py`` turns them into PNGs and measures the result again.
"""

from __future__ import annotations

import html
from typing import Sequence

from showcase import contract

PRODUCT_NAME = "Controlvar"
TAGLINE = "A/B testing and feature flags you run yourself."
END_LINE = "Open source, Apache-2.0, runs on your own infrastructure"
SITE = "controlvar.com"
REPOSITORY = "github.com/Controlvar/controlvar"
RECORDED_LINE = "Recorded on Controlvar {version}, a local stack with demo data"

#: The caption band: white system-UI semibold on #111827 (17.7:1 contrast).
CAPTION_BACKGROUND = "#111827"
CAPTION_COLOUR = "#FFFFFF"
CAPTION_FONT_PX = 40
#: The UX minimum, in output pixels.
CAPTION_MIN_FONT_PX = 36
CAPTION_LINE_PX = 50
CAPTION_PADDING_PX = 20
#: The widest a caption line may draw; wider is refused, never cut.
CAPTION_MAX_WIDTH_PX = 1760

CARD_BACKGROUND = "#0F172A"
CARD_COLOUR = "#FFFFFF"
CARD_MUTED = "#CBD5E1"

#: The OCR canary's faint line: smaller and fainter than any text the
#: dashboard draws at the capture's zoom of 4/3. Measured in frontend/src: its
#: faintest text is slate-400 (#94A3B8) or gray-400 (#9CA3AF), 2.56 and 2.54 to
#: 1 on white, at 12 px (``text-xs``) at the smallest, so 16 px in the frame;
#: its smallest text is 11 px, 14.7 px in the frame.
FAINT_CANARY_PX = 13
FAINT_CANARY_COLOUR = "#94A3B8"

FONT_STACK = '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif'
MONO_STACK = 'ui-monospace, Menlo, "SF Mono", Consolas, monospace'

# The dashboard's own mark (frontend/public/favicon.svg).
MARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="{size}" height="{size}" '
    'role="img" aria-label="Controlvar"><rect width="64" height="64" rx="14" fill="#2563eb"/>'
    '<path d="M46 16H26a6 6 0 0 0-6 6v20a6 6 0 0 0 6 6h20v-8H29V24h17z" fill="#ffffff"/></svg>'
)


def _page(
    width: int, height: int, background: str, body: str, extra_css: str = ""
) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        f"html,body{{margin:0;padding:0;width:{width}px;height:{height}px;overflow:hidden;"
        f"background:{background};font-family:{FONT_STACK};-webkit-font-smoothing:antialiased}}"
        f"{extra_css}</style></head><body>{body}</body></html>"
    )


def caption_html(lines: Sequence[str]) -> str:
    """One caption band, ``contract.PAGE_W`` x ``contract.BAND_H``, lines as given."""
    css = (
        f".band{{box-sizing:border-box;width:{contract.PAGE_W}px;height:{contract.BAND_H}px;"
        f"padding:{CAPTION_PADDING_PX}px 0;display:flex;flex-direction:column;align-items:center;"
        "justify-content:center}"
        f".line{{color:{CAPTION_COLOUR};font-weight:600;font-size:{CAPTION_FONT_PX}px;"
        f"line-height:{CAPTION_LINE_PX}px;white-space:pre;overflow:hidden;"
        f"max-width:{CAPTION_MAX_WIDTH_PX}px}}"
    )
    body = "".join(f'<div class="line">{html.escape(line)}</div>' for line in lines)
    return _page(
        contract.PAGE_W,
        contract.BAND_H,
        CAPTION_BACKGROUND,
        f'<div class="band" id="band">{body}</div>',
        css,
    )


def title_card_html(title: str) -> str:
    css = (
        ".card{width:1920px;height:1080px;display:flex;flex-direction:column;align-items:center;"
        f"justify-content:center;color:{CARD_COLOUR};text-align:center}}"
        ".brand{display:flex;align-items:center;gap:32px;font-size:88px;font-weight:700}"
        ".title{margin-top:56px;font-size:72px;font-weight:600}"
        f".tagline{{margin-top:40px;font-size:40px;color:{CARD_MUTED}}}"
    )
    body = (
        '<div class="card">'
        f'<div class="brand">{MARK_SVG.format(size=112)}<span>{html.escape(PRODUCT_NAME)}</span></div>'
        f'<div class="title" id="title">{html.escape(title)}</div>'
        f'<div class="tagline">{html.escape(TAGLINE)}</div>'
        "</div>"
    )
    return _page(contract.OUT_W, contract.OUT_H, CARD_BACKGROUND, body, css)


def end_card_html(version: str) -> str:
    css = (
        ".card{width:1920px;height:1080px;display:flex;flex-direction:column;align-items:center;"
        f"justify-content:center;color:{CARD_COLOUR};text-align:center}}"
        ".brand{display:flex;align-items:center;gap:32px;font-size:88px;font-weight:700}"
        f".line{{margin-top:40px;font-size:40px;color:{CARD_MUTED}}}"
        ".site{margin-top:64px;font-size:64px;font-weight:600}"
        f".repo{{margin-top:24px;font-size:40px;color:{CARD_MUTED}}}"
        f".recorded{{position:absolute;bottom:56px;width:1920px;font-size:28px;color:{CARD_MUTED}}}"
    )
    body = (
        '<div class="card">'
        f'<div class="brand">{MARK_SVG.format(size=112)}<span>{html.escape(PRODUCT_NAME)}</span></div>'
        f'<div class="line">{html.escape(END_LINE)}</div>'
        f'<div class="site" id="site">{html.escape(SITE)}</div>'
        f'<div class="repo" id="repo">{html.escape(REPOSITORY)}</div>'
        f'<div class="recorded">{html.escape(RECORDED_LINE.format(version=version))}</div>'
        "</div>"
    )
    return _page(contract.OUT_W, contract.OUT_H, CARD_BACKGROUND, body, css)


def card_texts(title: str, version: str) -> Sequence[str]:
    """Every string the two cards draw."""
    return (
        PRODUCT_NAME,
        title,
        TAGLINE,
        END_LINE,
        SITE,
        REPOSITORY,
        RECORDED_LINE.format(version=version),
    )


def canary_html(needle: str, key_like: str, faint: str) -> str:
    """A page that draws an OCR canary in the sizes a dashboard uses.

    The canary values are made up for each run. The self-test passes only if
    every OCR engine reads the dark value and key back through the same
    encode the video gets, and at least one reads the faint value
    (``FAINT_CANARY_PX`` px ``FAINT_CANARY_COLOUR``).
    """
    css = (
        ".wrap{padding:96px 120px;color:#0F172A}"
        "h1{font-size:30px;margin:0 0 32px}"
        f".mono{{font-family:{MONO_STACK};font-size:14px;margin:16px 0}}"
        ".small{font-size:16px;margin:16px 0}"
        f".faint{{font-size:{FAINT_CANARY_PX}px;color:{FAINT_CANARY_COLOUR};margin:16px 0}}"
        "table{border-collapse:collapse;font-size:15px;margin-top:40px}"
        "td{border-bottom:1px solid #E2E8F0;padding:10px 24px 10px 0}"
    )
    body = (
        '<div class="wrap"><h1>Settings</h1>'
        f'<div class="mono">X-API-Key: {html.escape(key_like)}</div>'
        f'<div class="small">One-time password: {html.escape(needle)}</div>'
        "<table><tr><td>Homepage hero copy test</td><td>Active</td><td>2 variants</td></tr>"
        "<tr><td>Checkout button colour</td><td>Draft</td><td>3 variants</td></tr></table>"
        f'<div class="faint">Last edited by {html.escape(faint)}</div>'
        "</div>"
    )
    return _page(contract.OUT_W, contract.OUT_H, "#FFFFFF", body, css)


def relative_luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (relative_luminance(foreground), relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)
