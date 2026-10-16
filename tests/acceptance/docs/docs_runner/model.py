"""The expectations file: one journey through one guide, written before the run.

Every model forbids unknown fields and coerces nothing (``extra="forbid"``,
``strict=True``), so a misspelt key or a quoted number is an error at load, not
a field silently ignored. ``loader`` adds the refusals that need more than one
file to decide (the guide's text and anchors, the inventory, the oracles).

A journey (this one walked the docs home page on docs-local, 2026-10-06; it
needs the inventory to classify ``README.md`` as the journey ``docs-site``,
without ``pending``)::

    guide: README.md                         # the nav path, as inventory.toml keys it (a [documents] key for a document)
    stack: docs-local                        # compose-dev | compose-empty | compose-sso | docs-local | docs-published | marketing-local | demo-local
    profile: core                            # core | full
    video: true
    written: 2026-10-06                      # the day the expectations were written
    steps:
      - id: home
        doc: start-here                      # an anchor of the guide
        do:
          goto: /
        expect:
          status: 200
          aria: |
            - heading "Controlvar Documentation" [level=1]
        fail: the home page does not answer, or its heading is not the source's first heading
      - id: open-quick-start
        doc: start-here
        do:
          click: {role: link, name: Quick Start}
        expect:
          url: /getting-started/quick-start/
          aria: |
            - heading "Quick Start" [level=1]
        fail: the Quick Start link does not lead to the Quick Start page
      - id: deploy-guide
        doc: self-hosting
        do:
          goto: /self-hosting/cdk/
        expect:
          status: 200
        snapshot: false                      # no ARIA snapshot: say why
        snapshot_reason: only that the page answers matters here
        fail: the AWS CDK deployment page does not answer
        not_run: needs-aws                   # a reason from registry.py; reported NOT RUN

``do`` is exactly one action. ``expect`` lists what must hold after it. A step
on a screen (``goto``, ``open``, ``click``, ``fill``, ``select``) expects an
ARIA snapshot of the page, ``aria``, written before the run, beside any
structural expectation (``url``, ``status``, ``visible``, ``text``,
``number``); only ``snapshot: false`` with a one-line ``snapshot_reason`` drops
it, and the step then needs a structural expectation and no ``aria``. A step
with ``snapshot: false`` keeps no screenshot and no ARIA snapshot, whether it
passes or fails. ``aria`` is Playwright's ARIA snapshot template: it must parse
as a YAML list. ``api``, ``ref``, ``crawl`` and ``keep`` steps take no
snapshot. ``fail`` says what failure looks like; a step without it is refused.
Names, labels, options, ``text``, ``goto``, ``open`` and ``search`` are one
line each.

Two actions are for the documentation site itself (stacks docs-published and
docs-local), whose source (``mkdocs.yml`` and ``docs/``) the stack names:

* ``search: <query>`` opens the site's home page and types the query into its
  search box; ``expect.found`` is the path of the page that must be among the
  first ``SEARCH_TOP`` results. A search step expects ``found`` and nothing
  else, and takes no ARIA snapshot (the results' excerpts change with every
  edit of the pages); its screen is kept as a screenshot.
* ``crawl: nav`` visits every page of the source's nav on the site: each must
  answer 2xx without leaving the site, and its rendered first-level heading
  must equal the source page's first ``# `` heading. ``crawl: links`` checks
  every link to the site found on those pages, and every absolute link to the
  site (its ``site_url``) written in the source pages: each must answer 2xx,
  following redirects only within the site. A crawl's check is the action
  itself, so a crawl step has no ``expect``.

Three are for following a guide on the compose stack (``compose-dev``, and for
``open`` also ``compose-sso``, the same stack brought up for SSO sign-in, and
``demo-local``, the one ``demo/setup-local.sh`` starts) as its reader does:

* ``open: <URL>`` opens an address the guide gives, such as
  ``http://localhost:3000``: the guide's text must contain the URL's origin
  (``http://localhost:<port>``), and the step fails unless the stack, started
  with ``docker-compose.yml``'s default host ports, serves something on that
  port. The stack runs on ports of its own, so the runner opens the same path
  on the port that stands for it (``stacks.ComposeDev``). On demo-local the
  ports are the script's own, and an address is served when the script's
  "Ready!" box lists it (``stacks.DemoLocal``). A screen step.
* ``passwords: [<name>, ...]`` (a journey field, not a step) makes up one
  fresh password per name when the journey starts, each meeting the account
  rules; ``fill: {label: ..., secret: <name>}`` types one instead of a
  ``value``.
* ``keep: {secret: <name>, role: <role>, within: {role: ..., name: ...}}``
  reads the text of the one element of that role inside the named element
  (the one-time password the dashboard shows once) and keeps it under that
  name, for a later ``fill``. Its check is the action itself: exactly one
  such element, its text at least ``redaction.MIN_LENGTH`` characters. It
  takes no screenshot and no ARIA snapshot.

What a run makes up or keeps is never written to the run directory
(``redaction.py``): a fill that types one takes ``snapshot: false``, a journey
that has one is not recorded whole (``video: false``), and every text the runner
writes has them taken out.

A journey may record launch walkthroughs (``recordings.toml``), each a segment
of its steps::

    recordings:
      - name: R8-onboarding        # a table of recordings.toml, from this guide
        start: demo-admin-log-out  # the first step recorded; by default the first
        end: viewer-new-accepted   # the last step recorded; by default the last

The run keeps ``recordings/<name>.webm`` for each: the journey's browser from
``start`` to ``end`` at 1280x720 and real speed, a title card first and a
caption bar naming each step's section, action and, for a step off the screen,
its result. Two segments of one journey may not overlap, and no name is given
twice. Unlike ``video: true`` (which they replace: a journey has one or the
other), they may be on a journey with secrets, as long as nothing in any
segment can draw one: the loader refuses, for each segment, one holding a
``keep`` step or the screen step right before one, one starting right after
one, and a ``fill`` of a secret into a field whose label does not say
"password" (only a password field draws dots); an api step's key never
reaches the browser. And the runner checks the page before each segment
starts and after each of its steps: a value the run keeps, in the page's text
or in any field but a password field, fails the step and drops that recording
(``redaction.on_screen``).

What an API answers can be kept for later steps and checked against an oracle
(``values.py``, ``traffic.py``, ``oracles.py``):

* ``save: {<name>: {path: <dotted path>, secret: false}}`` on an ``api`` step
  keeps the value at that path of its answer; a later api step's path, body
  or expected JSON, or a ``goto``, uses it as ``{{<name>}}``. A value saved with ``secret: true``
  (an API key) is a secret like a kept one: never written to the run directory,
  and used only as an api or traffic step's ``key``, sent as ``X-API-Key``.
  ``keep`` may also read the one element of a role whose text starts with
  ``prefix`` (an API key's documented ``eptk_``) instead of one ``within`` a
  named element.
* ``evaluations`` (compose-dev only) evaluates a flag for a numbered set of
  users through ``GET /api/v1/feature-flags/evaluate/{key}`` with an API key:
  every answer must give the ``reason`` the step names, and with ``reason:
  rollout`` a user gets the flag exactly when the documented rollout hash puts
  them below ``rollout`` percent. Its check is the action itself.
* ``traffic`` (compose-dev only) sends an experiment a population the journey
  chooses: per variant, how many users are assigned and how many of them send
  the metric's event, through the tracking API with an API key. Its check is
  the action itself: every user assigned the variant the documented hash
  gives, every event accepted.
* ``expect.computed`` on an api step: the number at each JSON path equals an
  oracle's within a relative tolerance ``rel`` (``rel: 0``, exactly).
  ``expect.cells`` on a screen step: the cell of a table's column, in the row
  that has a cell reading ``row``, shows an oracle's number at the precision
  it is shown. ``expect.number`` does the same for the one element of a role
  and name, reading its text again until it agrees or stops changing
  (``checks.settle``).
* ``expect.after`` on an api step: ``{<dotted path>: <saved name>}``, the time
  at each JSON path is later than the time an earlier step saved under that
  name (both ISO 8601, read on the stack's own clock; one written without a
  zone is UTC, as the API writes ``datetime.utcnow()``). Equal or earlier
  fails, and so does a value that is absent or is not a time. The step's log
  line says how many seconds later it was.
* ``poll: {up_to_seconds, every_seconds}`` on a ``GET`` api step (a step
  field, beside ``expect``): the request is sent again every
  ``every_seconds`` until the expectation holds or ``up_to_seconds`` have
  passed, for what a background job of the stack does on its own timer
  (``waiting.py``). A poll that never holds FAILs at the bound with the last
  answer.

An SDK page is followed as its reader follows it (``sdk.py``; compose-dev
only): ``sdk`` installs the package with the page's own install command (the
first shell block under the ``install`` anchor) from the public registry, in a
fresh project or virtual environment, and runs the page's code block (the first
block under the ``snippet`` anchor) with only its placeholders replaced: each
``replace`` key is a whole quoted string of the block, and each value one of
``{{api-url}}``, ``{{experiment}}`` (the key of the experiment saved as
``experiment``), ``{{flag}}`` and ``{{user}}``. The API key saved as ``key`` is
given as ``CONTROLVAR_API_KEY`` (and its former name). ``stubs`` are functions the block calls and
the page leaves to the reader; ``answers`` name the block's variables (or
``called.<stub>``) whose values must be the oracle's: the variant the
documented assignment hash gives ``user``, and the flag answer the documented
rollout hash gives at ``rollout`` percent. Its check is the action itself; an
install that fails is FAIL, never NOT RUN. With ``language: go`` the install
section's block is the page's ``go mod init`` and ``go get``, run in its order,
the block is a complete program that prints one ``key=value`` line,
``answers`` name the keys of that line (no stubs, no paths), and ``replace``
must also put ``{{api-url}}`` in place of the API address the program names.

A demo document's command is run as its reader runs it (``commands.py``;
demo-local only, the checkout ``demo/setup-local.sh`` set up): ``run:
{command, up_to_seconds}`` runs one shell block of the step's section (its
``doc``), word for word, from the checkout's root with its venv active, and
``expect.exit`` (the status it must end with) and ``expect.output`` (lines its
output must hold) are checked when it ends. With ``background: true`` the
block is started and the journey goes on, so later steps can watch what it
does through the API; a later ``finish: <that step's id>`` lets it end within
its bound and checks ``exit`` and ``output`` there. A block that runs past its
bound, or is still running when its journey ends, is stopped. What it printed
is written through the redactor, cut to its end.
"""

from __future__ import annotations

import datetime
import re
from typing import Annotated, Any, Dict, List, Literal, Optional, Sequence, Tuple, Union

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from docs_runner.redaction import credential_key, credential_path
from docs_runner.sdk import required_placeholders

STACKS = (
    "compose-dev",
    "compose-empty",
    "compose-sso",
    "docs-local",
    "docs-published",
    "marketing-local",
    "demo-local",
)
Stack = Literal[
    "compose-dev",
    "compose-empty",
    "compose-sso",
    "docs-local",
    "docs-published",
    "marketing-local",
    "demo-local",
]
#: The stacks with an API and a dashboard: ``api`` and ``open`` steps run on
#: these. compose-sso is compose-dev's stack brought up for SSO sign-in;
#: compose-empty is compose-dev's stack with no seed data (``SEED=``);
#: demo-local is the one ``demo/setup-local.sh`` starts (``stacks.py``).
COMPOSE_STACKS = ("compose-dev", "compose-empty", "compose-sso", "demo-local")
Profile = Literal["core", "full"]

#: Who an ``api`` step calls as: nobody, or one of the four role accounts the
#: stack provides (on compose-dev, compose-sso and demo-local, the ``demo``
#: seed's accounts).
Caller = Literal["anonymous", "admin", "developer", "analyst", "viewer"]

#: The ARIA roles Playwright's ``get_by_role`` accepts (playwright 1.63.0).
Role = Literal[
    "alert",
    "alertdialog",
    "application",
    "article",
    "banner",
    "blockquote",
    "button",
    "caption",
    "cell",
    "checkbox",
    "code",
    "columnheader",
    "combobox",
    "complementary",
    "contentinfo",
    "definition",
    "deletion",
    "dialog",
    "directory",
    "document",
    "emphasis",
    "feed",
    "figure",
    "form",
    "generic",
    "grid",
    "gridcell",
    "group",
    "heading",
    "img",
    "insertion",
    "link",
    "list",
    "listbox",
    "listitem",
    "log",
    "main",
    "marquee",
    "math",
    "menu",
    "menubar",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "meter",
    "navigation",
    "none",
    "note",
    "option",
    "paragraph",
    "presentation",
    "progressbar",
    "radio",
    "radiogroup",
    "region",
    "row",
    "rowgroup",
    "rowheader",
    "scrollbar",
    "search",
    "searchbox",
    "separator",
    "slider",
    "spinbutton",
    "status",
    "strong",
    "subscript",
    "superscript",
    "switch",
    "tab",
    "table",
    "tablist",
    "tabpanel",
    "term",
    "textbox",
    "time",
    "timer",
    "toolbar",
    "tooltip",
    "tree",
    "treegrid",
    "treeitem",
]

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
#: A guide's path: a Markdown file, or a dashboard component (``.tsx``).
GUIDE_PATTERN = r"^[^/\s][^\s]*\.(?:md|tsx)$"

#: The actions on one screen, which expect an ARIA snapshot.
SCREEN_ACTIONS = ("goto", "open", "click", "fill", "select")
#: The actions in the browser; each but ``keep`` keeps its screen as a screenshot.
BROWSER_ACTIONS = (*SCREEN_ACTIONS, "search", "keep")
#: What a step on a screen may expect, and what an ``api`` step may.
BROWSER_EXPECTS = ("url", "status", "visible", "text", "number", "cells", "aria")
#: A screen step's structural expectations: all but the ARIA snapshot.
STRUCTURAL_EXPECTS = tuple(name for name in BROWSER_EXPECTS if name != "aria")
#: What an api step may expect of its answer; ``after`` compares times in it.
API_ANSWER_EXPECTS = ("status", "json", "computed")
API_EXPECTS = (*API_ANSWER_EXPECTS, "after")
SEARCH_EXPECTS = ("found",)
#: What a ``run`` or ``finish`` step may expect: how its block ended.
RUN_EXPECTS = ("exit", "output")
#: The stacks that serve the documentation site and name its source.
SITE_STACKS = ("docs-local", "docs-published")
#: The stacks a ``run`` step's block runs on: a checkout demo/setup-local.sh
#: set up, whose venv the demo's documents tell their reader to activate.
RUN_STACKS = ("demo-local",)
#: What a crawl checks: every nav page's heading, or every link to the site.
CRAWLS = ("nav", "links")
#: How far down the search results a ``found`` page may be.
SEARCH_TOP = 3
#: An address a guide gives for the compose stack: ``open``'s argument.
LOCAL_URL = re.compile(
    r"^(?P<origin>http://localhost:(?P<port>[0-9]{1,5}))(?P<path>/\S*)?$"
)


def _one_line(value: str, what: str) -> str:
    if not value.strip() or "\n" in value.strip():
        raise ValueError(f"{what} must be one non-empty line")
    return value


def _single_line(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("must be one line")
    return value


#: A string a reader sees or types on one line: a name, a label, a path.
OneLine = Annotated[StrictStr, Field(min_length=1), AfterValidator(_single_line)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Quoted(_Strict):
    """A name the reader is told to look for, so the guide must say it.

    ``quote: false`` exempts it from the check that the guide's text contains
    it, and then needs a one-line ``reason``.
    """

    quote: StrictBool = True
    reason: Optional[StrictStr] = None

    @model_validator(mode="after")
    def _reason_iff_unquoted(self) -> "_Quoted":
        if self.quote and self.reason is not None:
            raise ValueError("reason is only for quote: false")
        if not self.quote:
            if self.reason is None:
                raise ValueError("quote: false needs a one-line reason")
            _one_line(self.reason, "reason")
        return self


class Named(_Quoted):
    """An element found by its ARIA role and accessible name."""

    role: Role
    name: OneLine


class Fill(_Quoted):
    """Type ``value``, or the password kept as ``secret``, into the field ``label``."""

    label: OneLine
    value: Optional[StrictStr] = None
    secret: Optional[StrictStr] = Field(default=None, pattern=SLUG_PATTERN)

    @model_validator(mode="after")
    def _value_or_secret(self) -> "Fill":
        if (self.value is None) == (self.secret is None):
            raise ValueError("a fill types exactly one of value or secret")
        return self


class Select(_Quoted):
    """Choose ``option`` in the select labelled ``label``."""

    label: OneLine
    option: OneLine


class Keep(_Strict):
    """Keep the text of one ``role`` element as ``secret``.

    The element is the one inside ``within``, or, where the page names no
    element around it, the one whose text starts with ``prefix`` (the shape the
    guide gives the value, such as an API key's ``eptk_``).
    """

    secret: StrictStr = Field(pattern=SLUG_PATTERN)
    role: Role
    within: Optional[Named] = None
    prefix: Optional[OneLine] = None

    @model_validator(mode="after")
    def _within_or_prefix(self) -> "Keep":
        if (self.within is None) == (self.prefix is None):
            raise ValueError(
                "keep finds its element by exactly one of within or prefix"
            )
        return self


#: A saved value's name, and a secret's: a slug, as a step's id is.
Name = Annotated[StrictStr, Field(pattern=SLUG_PATTERN)]


class Api(_Strict):
    """One HTTP request to the stack's API, as one of its accounts.

    ``key`` names a secret (an API key saved earlier) sent as ``X-API-Key``;
    it is given with ``as: anonymous``, so the request carries the key alone,
    as an SDK's does.
    """

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: StrictStr = Field(pattern=r"^/\S*$")
    as_: Caller = Field(alias="as")
    body: Optional[Union[Dict[str, Any], List[Any]]] = None
    key: Optional[Name] = None

    @model_validator(mode="after")
    def _key_alone(self) -> "Api":
        if self.key is not None and self.as_ != "anonymous":
            raise ValueError(
                "an api step with a key sends the key alone: give as: anonymous"
            )
        return self


class Population(_Strict):
    """How many users one variant gets, and how many of them convert."""

    assigned: StrictInt = Field(ge=1, le=10000)
    converted: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _converted_at_most_assigned(self) -> "Population":
        if self.converted > self.assigned:
            raise ValueError("converted cannot be more than assigned")
        return self


class Traffic(_Strict):
    """A chosen population sent through the tracking API (``traffic.py``)."""

    #: The saved value holding the experiment's id.
    experiment: Name
    #: The secret holding the API key the users are assigned and tracked with.
    key: Name
    #: Who reads the experiment's key and variants first.
    as_: Caller = Field(alias="as")
    #: The event a converting user sends: the primary metric's event name.
    event: OneLine
    #: The user ids are ``<users>-00001``, ``<users>-00002``, ...
    users: StrictStr = Field(pattern=SLUG_PATTERN)
    variants: Dict[OneLine, Population] = Field(min_length=1)


class Evaluations(_Strict):
    """Users evaluated for one flag through the SDK route (``traffic.py``).

    ``reason`` is what every answer must give: ``rollout`` (then each user gets
    the flag exactly when the documented rollout hash puts them below
    ``rollout`` percent), ``targeting_rule`` (every user gets it) or
    ``inactive`` (no user gets it).
    """

    flag: OneLine
    key: Name
    users: StrictStr = Field(pattern=SLUG_PATTERN)
    count: StrictInt = Field(ge=1, le=2000)
    context: Optional[
        Dict[StrictStr, Union[StrictBool, StrictInt, StrictFloat, StrictStr]]
    ] = None
    reason: Literal["rollout", "targeting_rule", "inactive"]
    rollout: Optional[StrictInt] = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _rollout_iff_reason_rollout(self) -> "Evaluations":
        if (self.reason == "rollout") != (self.rollout is not None):
            raise ValueError(
                "rollout is the percentage for reason: rollout, and only for it"
            )
        return self


#: A variable of an SDK page's block, or a path of attributes or keys under one
#: (``assignment.variantName``), or ``called.<stub>``.
Expression = Annotated[
    StrictStr,
    Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"),
]
#: A function the block calls and the page leaves to the reader.
Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
#: What a placeholder of an SDK page's block is replaced with (``sdk.py``).
SdkPlaceholder = Literal["{{api-url}}", "{{experiment}}", "{{flag}}", "{{user}}"]


class Answers(_Strict):
    """The block's values held to the oracle: each must be what it gives."""

    variant: List[Expression] = Field(min_length=1)
    enabled: List[Expression] = Field(min_length=1)
    reason: List[Expression] = Field(default_factory=list)

    def items(self):
        """(kind, expressions) for each kind of answer the step names."""
        named = {"variant": self.variant, "enabled": self.enabled}
        if self.reason:
            named["reason"] = self.reason
        return named.items()


class Sdk(_Strict):
    """Install an SDK as its page says and run its block (``sdk.py``)."""

    language: Literal["typescript", "python", "go"]
    #: The anchor of the section whose first shell block installs the package.
    install: StrictStr = Field(pattern=r"^[^#\s]+$")
    #: The anchor of the section whose first code block is run.
    snippet: StrictStr = Field(pattern=r"^[^#\s]+$")
    #: The secret holding the API key, given as ``CONTROLVAR_API_KEY`` (and its former name).
    key: Name
    #: The saved value holding the id of the experiment the block assigns.
    experiment: Name
    #: Who reads the experiment's key and variants.
    as_: Caller = Field(alias="as")
    flag: StrictStr = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    rollout: StrictInt = Field(ge=0, le=100)
    user: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
    replace: Dict[OneLine, SdkPlaceholder] = Field(min_length=1)
    stubs: List[Identifier] = Field(default_factory=list)
    answers: Answers

    @model_validator(mode="after")
    def _replaces_what_the_oracle_answers_for(self) -> "Sdk":
        used = set(self.replace.values())
        missing = [p for p in required_placeholders(self.language) if p not in used]
        if missing:
            raise ValueError(
                f"replace puts nothing in place of {', '.join(missing)}: the block"
                " must use the step's experiment, flag and user"
                + (
                    ", and a go block the stack's address, which its program names"
                    " itself"
                    if "{{api-url}}" in missing
                    else ""
                )
            )
        values = list(self.replace.values())
        twice = sorted({value for value in values if values.count(value) > 1})
        if twice:
            raise ValueError(f"replace puts {twice[0]} in place of two strings")
        return self

    @model_validator(mode="after")
    def _a_go_block_answers_with_its_line(self) -> "Sdk":
        if self.language != "go":
            return self
        if self.stubs:
            raise ValueError("a go block is a complete program; it takes no stubs")
        for _, expressions in self.answers.items():
            for expression in expressions:
                if "." in expression:
                    raise ValueError(
                        f"{expression}: a go block's answers are keys of the one"
                        " key=value line it prints, not paths"
                    )
        return self

    @model_validator(mode="after")
    def _called_names_a_stub(self) -> "Sdk":
        if len(set(self.stubs)) != len(self.stubs):
            raise ValueError("a stub is named twice")
        for _, expressions in self.answers.items():
            for expression in expressions:
                root, _, rest = expression.partition(".")
                if root != "called":
                    continue
                if not rest or "." in rest or rest not in self.stubs:
                    raise ValueError(
                        f"{expression}: called.<name> names one of the stubs"
                    )
        return self


class Run(_Strict):
    """One shell block of the step's section (its ``doc``), run as the page
    writes it from the checkout's root with the venv active (``commands.py``).

    ``command`` is the block's text, word for word; ``up_to_seconds`` bounds it
    from its start. ``background: true`` starts it and goes on: a later
    ``finish`` step names this step and checks how it ended.
    """

    command: StrictStr = Field(min_length=1, max_length=2000)
    up_to_seconds: StrictInt = Field(ge=5, le=3600)
    background: StrictBool = False


class Do(_Strict):
    """Exactly one action."""

    goto: Optional[OneLine] = None
    open: Optional[OneLine] = None
    click: Optional[Named] = None
    fill: Optional[Fill] = None
    select: Optional[Select] = None
    keep: Optional[Keep] = None
    api: Optional[Api] = None
    traffic: Optional[Traffic] = None
    evaluations: Optional[Evaluations] = None
    sdk: Optional[Sdk] = None
    ref: Optional[Literal["doc-examples"]] = None
    search: Optional[OneLine] = None
    crawl: Optional[Literal["nav", "links"]] = None
    run: Optional[Run] = None
    #: The id of an earlier ``run`` step with ``background: true``: this step
    #: lets that block end, within its bound, and checks how it ended.
    finish: Optional[StrictStr] = Field(default=None, pattern=SLUG_PATTERN)

    @model_validator(mode="after")
    def _exactly_one(self) -> "Do":
        given = [name for name in type(self).model_fields if getattr(self, name)]
        if len(given) != 1:
            raise ValueError(
                "do must be exactly one of goto, open, click, fill, select, keep,"
                f" api, traffic, evaluations, sdk, ref, search, crawl, run, finish;"
                f" got {given or 'none'}"
            )
        return self

    @field_validator("goto")
    @classmethod
    def _goto_is_a_path(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and "://" in value:
            raise ValueError(
                "goto is a path on the stack (its base URL is the stack's), not a"
                " URL; open is for an address the guide gives"
            )
        return value

    @field_validator("open")
    @classmethod
    def _open_is_a_local_url(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not LOCAL_URL.match(value):
            raise ValueError(
                "open is an address the guide gives on this machine,"
                " http://localhost:<port> and an optional path"
            )
        return value

    @property
    def kind(self) -> str:
        return next(name for name in type(self).model_fields if getattr(self, name))


Scalar = Union[StrictBool, StrictInt, StrictFloat, StrictStr]


class Oracle(_Strict):
    """A named function in ``docs_runner.oracles.ORACLES``, called with ``args``."""

    name: StrictStr = Field(min_length=1)
    args: Dict[str, Scalar] = Field(default_factory=dict)


class NumberExpect(_Strict):
    """The number shown in an element equals what an oracle computes."""

    locator: Named
    oracle: Oracle


class Computed(_Strict):
    """A number in an API answer equals an oracle's, within ``rel`` of it.

    ``rel: 0`` is equality: an oracle that gives a whole number (a sample
    size) is met by that number and by no neighbour.
    """

    oracle: Oracle
    rel: StrictFloat = Field(ge=0, le=0.01)


class Cell(_Quoted):
    """A table cell shows an oracle's number, at the precision it shows.

    The cell is the one under the column headed ``column``, in the one row
    that has a cell reading exactly ``row`` (a variant's name, say).
    """

    row: OneLine
    column: OneLine
    oracle: Oracle


class Poll(_Strict):
    """Ask again every ``every_seconds`` until the expectation holds, for at most
    ``up_to_seconds`` (``waiting.py``)."""

    up_to_seconds: StrictInt = Field(ge=5, le=900)
    every_seconds: StrictInt = Field(ge=5, le=60)

    @model_validator(mode="after")
    def _every_within_up_to(self) -> "Poll":
        if self.every_seconds > self.up_to_seconds:
            raise ValueError("every_seconds cannot be more than up_to_seconds")
        return self


#: A dotted path into an api step's JSON answer (``items.0.started_at``).
JsonPath = Annotated[StrictStr, Field(pattern=r"^[^.\s]+(?:\.[^.\s]+)*$")]


class Expect(_Strict):
    """What must hold after the step; at least one entry."""

    url: Optional[StrictStr] = Field(default=None, pattern=r"^/\S*$")
    status: Optional[StrictInt] = Field(default=None, ge=100, le=599)
    visible: Optional[List[Named]] = Field(default=None, min_length=1)
    text: Optional[OneLine] = None
    json_: Optional[Dict[str, Any]] = Field(default=None, alias="json", min_length=1)
    computed: Optional[Dict[StrictStr, Computed]] = Field(default=None, min_length=1)
    #: The time at each JSON path is later than the time saved under the name.
    after: Optional[Dict[JsonPath, Name]] = Field(default=None, min_length=1)
    number: Optional[NumberExpect] = None
    cells: Optional[List[Cell]] = Field(default=None, min_length=1)
    found: Optional[StrictStr] = Field(default=None, pattern=r"^/\S*$")
    aria: Optional[StrictStr] = Field(default=None, min_length=1)
    #: A ``run`` or ``finish`` step: the status its block must end with.
    exit: Optional[StrictInt] = Field(default=None, ge=0, le=255)
    #: And lines its output must hold, each word for word.
    output: Optional[List[OneLine]] = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _something(self) -> "Expect":
        if not self.given():
            raise ValueError("expect names nothing to check")
        return self

    @field_validator("aria")
    @classmethod
    def _aria_parses(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            nodes = yaml.safe_load(value)
        except yaml.YAMLError:
            nodes = None
        if not isinstance(nodes, list) or not nodes:
            raise ValueError(
                "aria must be an ARIA snapshot template: a non-empty YAML list of"
                ' nodes, such as - heading "Title" [level=1]'
            )
        return value

    def given(self) -> List[str]:
        """The entries set, by their names in the file."""
        names = []
        for name, field in type(self).model_fields.items():
            if getattr(self, name) is not None:
                names.append(field.alias or name)
        return names


class Save(_Strict):
    """Keep the value at ``path`` of an api step's answer; ``secret`` keeps it unwritten."""

    path: StrictStr = Field(pattern=r"^[^.\s]+(?:\.[^.\s]+)*$")
    secret: StrictBool = False


class Step(_Strict):
    id: StrictStr = Field(pattern=SLUG_PATTERN)
    doc: StrictStr = Field(pattern=r"^[^#\s]+$")
    do: Do
    expect: Optional[Expect] = None
    fail: StrictStr
    snapshot: Optional[StrictBool] = None
    snapshot_reason: Optional[StrictStr] = None
    not_run: Optional[StrictStr] = None
    save: Optional[Dict[Name, Save]] = Field(default=None, min_length=1)
    poll: Optional[Poll] = None

    @field_validator("fail")
    @classmethod
    def _fail_one_line(cls, value: str) -> str:
        return _one_line(value, "fail")

    @model_validator(mode="after")
    def _snapshot_reason_iff_no_snapshot(self) -> "Step":
        if self.snapshot is False:
            if self.snapshot_reason is None:
                raise ValueError("snapshot: false needs a one-line snapshot_reason")
            _one_line(self.snapshot_reason, "snapshot_reason")
        elif self.snapshot_reason is not None:
            raise ValueError("snapshot_reason is only for snapshot: false")
        return self

    @property
    def in_browser(self) -> bool:
        return self.do.kind in BROWSER_ACTIONS

    @property
    def takes_screenshot(self) -> bool:
        return self.in_browser and self.snapshot is not False and self.do.kind != "keep"

    @property
    def secret(self) -> Optional[str]:
        """The secret this step types (a fill's) or keeps, if any."""
        if self.do.fill is not None:
            return self.do.fill.secret
        if self.do.keep is not None:
            return self.do.keep.secret
        return None


#: A launch walkthrough's name: its number, then a slug (``recordings.toml``).
RECORDING_PATTERN = r"^R[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*$"


class Recording(_Strict):
    """The launch walkthrough this journey records, and which of its steps."""

    name: StrictStr = Field(pattern=RECORDING_PATTERN)
    start: Optional[StrictStr] = Field(default=None, pattern=SLUG_PATTERN)
    end: Optional[StrictStr] = Field(default=None, pattern=SLUG_PATTERN)


class Journey(_Strict):
    #: A Markdown page or document, or a dashboard component the inventory
    #: classes journey (``.tsx``: its anchors are its ``data-testid`` values).
    guide: StrictStr = Field(pattern=GUIDE_PATTERN)
    stack: Stack
    profile: Profile
    video: StrictBool
    written: datetime.date
    #: Passwords the runner makes up when the journey starts, by name.
    passwords: List[Annotated[StrictStr, Field(pattern=SLUG_PATTERN)]] = Field(
        default_factory=list
    )
    #: The launch walkthroughs this journey records, each a segment of its steps.
    recordings: List[Recording] = Field(default_factory=list)
    steps: List[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_step_ids(self) -> "Journey":
        seen = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"step id {step.id!r} is used twice")
            seen.add(step.id)
        return self

    @model_validator(mode="after")
    def _unique_secrets(self) -> "Journey":
        names = list(self.passwords)
        names += [step.do.keep.secret for step in self.steps if step.do.keep]
        twice = sorted({name for name in names if names.count(name) > 1})
        if twice:
            raise ValueError(
                f"secret {twice[0]!r} is made up or kept twice; give each its own name"
            )
        return self

    @model_validator(mode="after")
    def _recordings_apart(self) -> "Journey":
        segments_of(self.steps, self.recordings)
        return self

    @property
    def segments(self) -> List[Tuple[Recording, int, int]]:
        """Each recording with its first and last step index (from 0), in step order."""
        return segments_of(self.steps, self.recordings)

    @property
    def has_secrets(self) -> bool:
        """Made-up passwords, kept values, or an api step that reveals a credential."""
        return bool(self.passwords) or any(
            step.do.keep is not None or reveals_credential(step) for step in self.steps
        )


def segment_of(steps: Sequence[Step], recording: Recording) -> Tuple[int, int]:
    """The indices (from 0) of the recording's first and last step; ValueError
    when ``start`` or ``end`` names no step, or ``end`` comes before ``start``."""
    ids = [step.id for step in steps]
    bounds = []
    for what, name, default in (
        ("start", recording.start, 0),
        ("end", recording.end, len(ids) - 1),
    ):
        if name is None:
            bounds.append(default)
        elif name not in ids:
            raise ValueError(f"recording.{what} {name!r} is not a step of the journey")
        else:
            bounds.append(ids.index(name))
    if bounds[1] < bounds[0]:
        raise ValueError(
            f"recording.end {ids[bounds[1]]!r} comes before recording.start"
            f" {ids[bounds[0]]!r}"
        )
    return bounds[0], bounds[1]


def segments_of(
    steps: Sequence[Step], recordings: Sequence[Recording]
) -> List[Tuple[Recording, int, int]]:
    """Each recording with its step indices, in step order; ValueError when a
    bound names no step, a name is given twice, or two segments overlap."""
    names = [recording.name for recording in recordings]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise ValueError(f"recording {twice[0]!r} is named twice")
    found = sorted(
        ((recording, *segment_of(steps, recording)) for recording in recordings),
        key=lambda segment: segment[1],
    )
    for (before, _, last), (after, first, _) in zip(found, found[1:]):
        if first <= last:
            raise ValueError(
                f"recordings {before.name!r} and {after.name!r} overlap: a step is"
                " recorded by one walkthrough at most"
            )
    return found


#: Routes that answer with a credential: a sign-in's token, a new API key.
CREDENTIAL_ROUTES = ("/api/v1/auth/login", "/api/v1/auth/token", "/api/v1/api-keys")


def _names_a_credential(node: Any) -> bool:
    if isinstance(node, dict):
        return any(
            credential_key(str(k)) or _names_a_credential(v) for k, v in node.items()
        )
    if isinstance(node, list):
        return any(_names_a_credential(v) for v in node)
    return False


def reveals_credential(step: Step) -> bool:
    """True for an api step that asks for a credential, expects one in its answer,
    or saves one (``save`` with ``secret: true``).

    Its journey then counts as having a secret: it is not recorded.
    """
    call = step.do.api
    if call is None:
        return False
    if any(save.secret for save in (step.save or {}).values()):
        return True
    path = call.path.split("?", 1)[0].rstrip("/")
    if call.method == "POST" and path in CREDENTIAL_ROUTES:
        return True
    expected = (step.expect.json_ if step.expect is not None else None) or {}
    for json_path, wanted in expected.items():
        if credential_path(json_path) or _names_a_credential(wanted):
            return True
    return False
