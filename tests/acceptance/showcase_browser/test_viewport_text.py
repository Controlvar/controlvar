"""What ``director.VIEWPORT_TEXT_JS`` reads, run in headless Chromium (#1066, G2).

Gates U4 and G2 compare with this reader: the text drawn in the viewport, and
the field values. React draws ``{rolloutPercentage}%`` as two text nodes, "10"
and "%", with an empty comment between them when the page was rendered on the
server. Both flag pages draw their rollout this way (feature-flags/new.tsx and
feature-flags/[id].tsx at v0.36.0). The reader took one text node at a time, so
it read "10" and "%", and G2 refused video 3's "10%" (beat 5) although the page
showed 10%. Beat 8's "50%" is drawn the same way.

The reader now joins a parent's adjacent text nodes into one string, as they
are drawn: comments between them do not count, and an element between them
always ends the string.
"""

from __future__ import annotations

from typing import Any

import pytest
from showcase.capture import director, gates
from showcase.capture.storyboard import numbers_in

#: Video 3's two sentences with a number (03-flag-rollout.yaml, beats 5 and 8).
SAYS_10 = "The slider sets the share for everyone else: here, 10%."
SAYS_50 = "If it's going well, raise the rollout to 50%, and save."

#: How React fills ``<span>{value}%</span>`` in the browser: two text nodes.
CLIENT_SPLIT_JS = """([selector, value]) => {
  const span = document.querySelector(selector);
  span.replaceChildren(document.createTextNode(String(value)), document.createTextNode('%'));
  return [...span.childNodes].map((n) => n.nodeType);
}"""

#: The rollout section of both flag pages, as they draw it at v0.36.0 (the
#: value's span carries data-testid="rollout-value" on the detail page only;
#: its id="value" is this test's, to find it). ``{value}<!-- -->%`` is how
#: React renders ``{rolloutPercentage}%`` on the server.
ROLLOUT = """<body><section>
  <h2>Rollout</h2>
  <div>
    <div>
      <label for="rollout-percentage">Rollout percentage</label>
      <span id="value"{testid}>{value}<!-- -->%</span>
    </div>
    <input id="rollout-percentage" type="range" min="0" max="100" value="{value}">
    <div><span>0%</span><span>100%</span></div>
  </div>
</section></body>"""

#: DOM node types.
ELEMENT, TEXT, COMMENT = 1, 3, 8


def read(page: Any) -> str:
    return page.evaluate(director.VIEWPORT_TEXT_JS)


def node_types(page: Any, selector: str) -> list:
    return page.evaluate(
        "(s) => [...document.querySelector(s).childNodes].map((n) => n.nodeType)",
        selector,
    )


# --- Two text nodes are read as the one string the page draws ----------------------------------


@pytest.mark.regression
def test_two_text_nodes_react_made_in_the_browser_read_as_drawn(page):
    page.set_content('<body><span id="value"></span></body>')
    assert page.evaluate(CLIENT_SPLIT_JS, ["#value", 10]) == [TEXT, TEXT]
    assert page.inner_text("#value") == "10%"
    assert read(page) == "10%"


@pytest.mark.regression
def test_two_text_nodes_react_rendered_on_the_server_read_as_drawn(page):
    page.set_content('<body><span id="value">10<!-- -->%</span></body>')
    assert node_types(page, "#value") == [TEXT, COMMENT, TEXT]
    assert read(page) == "10%"


@pytest.mark.regression
@pytest.mark.parametrize("rendered", ["server", "browser"])
@pytest.mark.parametrize(
    ("testid", "value", "sentence"),
    [("", 10, SAYS_10), (' data-testid="rollout-value"', 50, SAYS_50)],
    ids=["create-page-beat-5", "detail-page-beat-8"],
)
def test_the_rollout_as_both_flag_pages_draw_it_passes_g2(
    page, testid, value, sentence, rendered
):
    page.set_content(ROLLOUT.format(testid=testid, value=value))
    if rendered == "browser":
        page.evaluate(CLIENT_SPLIT_JS, ["#value", value])
    reading = read(page)
    assert f"{value}%" in reading.split("\n")
    ok, numbers, why = gates.spoken_numbers(sentence, reading, reading)
    assert ok, why
    assert numbers["shown_at"] == {f"{value}%": "start and end"}


# --- What the page does not draw as one string stays apart, and G2 refuses it ---------------------


def test_text_in_two_elements_with_one_between_is_not_joined_and_g2_refuses(page):
    page.set_content("<body><span>10</span><b>x</b><span>%</span></body>")
    reading = read(page)
    assert reading == "10\nx\n%"
    assert "10%" not in numbers_in(reading)
    ok, numbers, why = gates.spoken_numbers(SAYS_10, reading, reading)
    assert not ok
    assert numbers["shown_at"] == {}
    assert why.startswith(
        "the narration says 10%; the page shows 10 at the beat's start"
    )


def test_an_element_between_two_text_nodes_of_one_parent_ends_the_string(page):
    page.set_content('<body><span id="value">10<b>x</b>%</span></body>')
    assert node_types(page, "#value") == [TEXT, ELEMENT, TEXT]
    reading = read(page)
    assert reading == "10\nx\n%"
    assert not gates.spoken_numbers(SAYS_10, reading, reading)[0]


def test_two_elements_side_by_side_are_two_strings(page):
    page.set_content("<body><span>10</span><span>%</span></body>")
    assert read(page) == "10\n%"


# --- What was never read still is not -----------------------------------------------------------


def test_text_with_no_box_in_the_viewport_is_not_read(page):
    page.set_content(
        '<body><span style="display:none">10<!-- -->%</span>'
        '<div style="position:absolute; top:5000px">50%</div><p>shown</p></body>'
    )
    assert read(page) == "shown"


def test_field_values_are_read_after_the_text(page):
    page.set_content(
        '<body><p>Name</p><input value="Express checkout" placeholder="Name it">'
        '<input type="password" value="hidden"></body>'
    )
    assert read(page) == "Name\nExpress checkout\nName it"
