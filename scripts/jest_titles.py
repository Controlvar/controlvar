"""The test titles a jest file declares, read from its source, without node.

``scripts/doc_claims.py`` uses this to refuse, on a pull request, a ``test:``
target that is not the whole title of a test. ``scripts/rc_evidence.py`` judges
the same target against jest's own report at a release; the rule for a name is
one function, its ``jest_title_pattern`` (#1819).

WHAT IT READS

A call of ``it``, ``test`` or ``describe`` (also ``fit``, ``xit``, ``xtest``,
``fdescribe``, ``xdescribe``, and with ``.only``, ``.skip``, ``.todo``,
``.failing``, ``.concurrent`` and ``.each``) that is code: not in a comment, a
string, a template or JSX. Its title is its first argument, when that is

* a string literal, or a template literal with no ``${}``;
* such literals joined with ``+``;
* a conditional between two of these (``on ? 'a' : 'b'``): both are titles.

An ``.each`` title counts as it is written (``%s: saves``) and with a row
filled in (``guided: saves``). The rows are read when the table is a list of
lists or of literals, written in the call or as a ``const`` of the file, and a
row when each value its title takes is a string (``%s``, or ``%p`` with no
quote, backslash or line break), a whole number (``%s``, ``%d``, ``%i``,
``%p``) or ``true``, ``false``, ``null``, ``undefined`` (``%s``, ``%p``). A
decimal or negative number, ``%j``, ``%o``, ``%O`` and ``%f`` are not filled
in: name such a test as written.

WHAT IT CANNOT READ

A title that is a name, a call or a template with ``${}``, so also a test
declared by a helper (``row('saves', ...)``); the rows of any other ``.each``
table; and an ``.each`` title with a ``$name``, which jest fills in from an
object's key. A name that only such a test has is refused, and the problem
lists the lines that were not read: give the test a title that can be read, or
name another. A file that does not scan (a string not closed, a bracket with no
pair) is refused whole. It takes a ``/`` after ``)`` or ``}`` for a division,
and does not see ``it['only']``, ``it`` under another name, or a function of
the file's own called ``it``.

It reads source, so it does not know what runs or what a value prints as. Both
arms of a conditional, a test declared under an ``if`` and one in a function
count, and so does an ``.each`` title as written, whatever its values (the
release takes 1-500 characters on one line for each). The release finds those.

Standard library only. It runs nothing and reads no file itself.
"""

from __future__ import annotations

import bisect
import dataclasses
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

TESTS = {"it", "test", "fit", "xit", "xtest"}
SUITES = {"describe", "fdescribe", "xdescribe"}
MODIFIERS = {"only", "skip", "todo", "failing", "concurrent"}
SKIPS = {"xit", "xtest", "xdescribe", "skip", "todo"}
ONLYS = {"fit", "fdescribe", "only"}


class Unreadable(ValueError):
    """The source does not scan, so nothing in it is taken for a title."""


@dataclasses.dataclass(frozen=True)
class Token:
    kind: str  # word, number, string, template, regex, jsx, punct
    value: Any  # a string's or a plain template's text; None for one with ${}
    at: int


@dataclasses.dataclass(frozen=True)
class Title:
    """One title: a test's own, or a describe block's. Unreadable when both
    tuples are empty."""

    line: int
    #: As jest reports it: a literal, or an ``.each`` title's row filled in.
    exact: Tuple[str, ...] = ()
    #: An ``.each`` title as written.
    written: Tuple[str, ...] = ()

    @property
    def readable(self) -> bool:
        return bool(self.exact or self.written)


@dataclasses.dataclass(frozen=True)
class Test:
    title: Title
    suites: Tuple[Title, ...]  # its describe blocks, outermost first
    skipped: bool


@dataclasses.dataclass(frozen=True)
class Source:
    """What one file declares."""

    tests: Tuple[Test, ...]
    suites: Tuple[Title, ...]
    only: Optional[int]  # the line of the first ``.only``


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------
_SPACE = re.compile(r"[\s\ufeff]*")
_WORD = re.compile(r"[A-Za-z_$#@\u0080-\uffff][\w$\u0080-\uffff]*")
_NUMBER = re.compile(r"[0-9][\w.]*|\.[0-9]\w*")
_STRING = re.compile(r"'(?:[^'\\\n]|\\[\s\S])*'" r'|"(?:[^"\\\n]|\\[\s\S])*"')
_REGEX = re.compile(r"/(?:[^/\\\[\n]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/[a-z]*")
_CHUNK = re.compile(r"(?:[^`\\$]|\\[\s\S]|\$(?!\{))*")
_JSX = re.compile(r"<(?:>|(?P<tag>[A-Za-z_$][\w$.:-]*)(?=[\s/>]))")
_GENERIC = re.compile(r"\s+extends\s")
_CLOSE = re.compile(r"</\s*(?:[A-Za-z_$][\w$.:-]*)?\s*>")
_ATTRIBUTE = re.compile(r"[A-Za-z_$][\w$:-]*")
_CHILD = re.compile(r"[<{]")
_ESCAPE = re.compile(
    r"\\(?:u\{(?P<point>[0-9A-Fa-f]{1,6})\}|u(?P<unit>[0-9A-Fa-f]{4})"
    r"|x(?P<byte>[0-9A-Fa-f]{2})|(?P<break>\r\n|[\n\r\u2028\u2029])|(?P<one>[\s\S]))"
)
_NAMED = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_PAIRS = {")": "(", "]": "[", "}": "{"}
#: Words an expression follows: a ``/`` or ``<`` after one starts a regular
#: expression or an element, not a division or a comparison.
_BEFORE_EXPRESSION = {
    "await",
    "case",
    "default",
    "delete",
    "do",
    "else",
    "in",
    "instanceof",
    "new",
    "of",
    "return",
    "throw",
    "typeof",
    "void",
    "yield",
}


def _unescaped(match: "re.Match[str]") -> str:
    code = match["point"] or match["unit"] or match["byte"]
    if code:
        return chr(int(code, 16)) if int(code, 16) <= 0x10FFFF else match.group()
    return "" if match["break"] else _NAMED.get(match["one"], match["one"])


def unescape(body: str) -> str:
    """The text of a JavaScript literal's *body*."""
    text = _ESCAPE.sub(_unescaped, body)
    # Two \u escapes may be one character.
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


class _Scanner:
    def __init__(self, text: str, jsx: bool) -> None:
        self.text = text
        self.jsx = jsx
        self._breaks = [m.start() for m in re.finditer("\n", text)]

    def line(self, at: int) -> int:
        return bisect.bisect_left(self._breaks, at) + 1

    def refuse(self, what: str, at: int) -> Unreadable:
        return Unreadable(f"{what} at line {self.line(at)}")

    def _after_space(self, at: int) -> int:
        """The end of the white space and comments at *at*."""
        text = self.text
        while True:
            at = _SPACE.match(text, at).end()  # type: ignore[union-attr]
            if text.startswith("//", at):
                end = text.find("\n", at)
                at = len(text) if end < 0 else end
            elif text.startswith("/*", at):
                end = text.find("*/", at + 2)
                if end < 0:
                    raise self.refuse("a comment is not closed", at)
                at = end + 2
            else:
                return at

    @staticmethod
    def _expects_expression(last: Optional[Token]) -> bool:
        if last is None:
            return True
        if last.kind == "word":
            return last.value in _BEFORE_EXPRESSION
        return last.kind == "punct" and last.value not in (")", "]", "}")

    def code(self, at: int, into: List[Token], closer: str = "") -> int:
        """Append the tokens from *at* to the end, or to the *closer* of a
        ``{`` already read; the position after it."""
        text, start = self.text, at
        opened: List[Token] = []
        while True:
            at = self._after_space(at)
            if at >= len(text):
                if closer or opened:
                    raise self.refuse(
                        "a bracket is not closed", opened[-1].at if opened else start
                    )
                return at
            char = text[at]
            last = into[-1] if into else None
            if char in "'\"":
                found = _STRING.match(text, at)
                if not found:
                    raise self.refuse("a string is not closed", at)
                into.append(Token("string", unescape(found.group()[1:-1]), at))
                at = found.end()
            elif char == "`":
                end, value = self.template(at)
                into.append(Token("template", value, at))
                at = end
            elif (found := _WORD.match(text, at)) is not None:
                into.append(Token("word", found.group(), at))
                at = found.end()
            elif (found := _NUMBER.match(text, at)) is not None:
                into.append(Token("number", found.group(), at))
                at = found.end()
            elif char == "/" and self._expects_expression(last):
                found = _REGEX.match(text, at)
                if not found:
                    raise self.refuse("a regular expression is not closed", at)
                into.append(Token("regex", None, at))
                at = found.end()
            elif char == "<" and self._expects_expression(last) and self._element(at):
                into.append(Token("jsx", None, at))
                at = self.element(at)
            elif text.startswith("=>", at):
                into.append(Token("punct", "=>", at))
                at += 2
            else:
                token = Token("punct", char, at)
                if char in "([{":
                    opened.append(token)
                elif char in _PAIRS:
                    if not opened and char == closer:
                        return at + 1
                    if not opened or opened.pop().value != _PAIRS[char]:
                        raise self.refuse(f"a {char!r} closes nothing", at)
                into.append(token)
                at += 1

    def template(self, at: int) -> Tuple[int, Optional[str]]:
        """The end of the template at *at*, and its text (None with ``${}``)."""
        text, start, parts, plain = self.text, at, [], True
        at += 1
        while True:
            chunk = _CHUNK.match(text, at)
            at = chunk.end()  # type: ignore[union-attr]
            parts.append(chunk.group())  # type: ignore[union-attr]
            if at >= len(text):
                raise self.refuse("a template is not closed", start)
            if text[at] == "`":
                return at + 1, unescape("".join(parts)) if plain else None
            plain = False
            at = self.code(at + 2, [], "}")

    def _element(self, at: int) -> bool:
        found = _JSX.match(self.text, at) if self.jsx else None
        return found is not None and not (
            found["tag"] and _GENERIC.match(self.text, found.end())
        )

    def element(self, at: int) -> int:
        """The end of the JSX element at *at*."""
        text, start = self.text, at
        at = _JSX.match(text, at).end()  # type: ignore[union-attr]
        if not text.startswith("<>", start):
            while True:
                at = self._after_space(at)
                if text.startswith("/>", at):
                    return at + 2
                char = text[at : at + 1]
                if char == ">":
                    at += 1
                    break
                if char == "{":
                    at = self.code(at + 1, [], "}")
                elif char and char in "'\"":
                    end = text.find(char, at + 1)
                    if end < 0:
                        raise self.refuse("an attribute is not closed", at)
                    at = end + 1
                elif char == "=":
                    at += 1
                elif char == "<" and self._element(at):
                    at = self.element(at)
                elif (found := _ATTRIBUTE.match(text, at)) is not None:
                    at = found.end()
                else:
                    raise self.refuse("an element is not closed", start)
        while True:
            child = _CHILD.search(text, at)
            if child is None:
                raise self.refuse("an element is not closed", start)
            at = child.start()
            if child.group() == "{":
                at = self.code(at + 1, [], "}")
            elif (close := _CLOSE.match(text, at)) is not None:
                return close.end()
            elif self._element(at):
                at = self.element(at)
            else:
                raise self.refuse("an element is not closed", start)


# ---------------------------------------------------------------------------
# The calls
# ---------------------------------------------------------------------------
#: A placeholder jest fills in an ``.each`` title, and ``%%``, its ``%``.
PLACEHOLDER = re.compile(r"%[psdifjoO#$%]")
#: A ``$name`` (or ``$#``) jest fills in from a row that is an object.
VARIABLE = re.compile(r"\$[\w#]")
_CONSTANTS = ("true", "false", "null", "undefined")
_INTEGER = re.compile(r"0|[1-9][0-9]{0,14}")
_PLAIN = re.compile(r"[^\"\\\n]*")
#: Tokens from a number up to, not including, another.
Range = Tuple[int, int]


def _is(token: Optional[Token], *values: str) -> bool:
    return token is not None and token.kind == "punct" and token.value in values


class _Calls:
    def __init__(self, text: str, jsx: bool) -> None:
        self.scanner = _Scanner(text, jsx)
        self.tokens: List[Token] = []
        self.scanner.code(0, self.tokens)
        self.close: Dict[int, int] = {}
        opened: List[int] = []
        for number, token in enumerate(self.tokens):
            if _is(token, "(", "[", "{"):
                opened.append(number)
            elif _is(token, ")", "]", "}"):
                self.close[opened.pop()] = number

    def token(self, number: int) -> Optional[Token]:
        return self.tokens[number] if 0 <= number < len(self.tokens) else None

    def split(self, start: int, end: int, *at: str) -> List[Range]:
        """The ranges between the punctuation *at* in tokens start..end, not
        counting any inside brackets."""
        out, first, number = [], start, start
        while number < end:
            if number in self.close:
                number = self.close[number]
            elif _is(self.tokens[number], *at):
                out.append((first, number))
                first = number + 1
            number += 1
        out.append((first, end))
        return out

    def literals(self, start: int, end: int) -> Tuple[str, ...]:
        """The titles the expression in tokens start..end can be; () when it
        is not one this can read."""
        while start < end and self.close.get(start) == end - 1:
            if not _is(self.tokens[start], "("):
                return ()
            start, end = start + 1, end - 1
        choice = self.split(start, end, "?")
        if len(choice) == 2:
            arms = self.split(choice[1][0], choice[1][1], ":")
            found = [self.literals(*arm) for arm in arms]
            return found[0] + found[1] if len(found) == 2 and all(found) else ()
        joined = []
        for first, last in self.split(start, end, "+"):
            token = self.tokens[first] if last - first == 1 else None
            if token is None or token.kind not in ("string", "template"):
                return ()
            if token.value is None:  # a template with ${}
                return ()
            joined.append(token.value)
        return ("".join(joined),)

    def _value(self, start: int, end: int, placeholder: str) -> Optional[str]:
        """What jest puts in a title for the value in tokens start..end; None
        when it is not a literal whose form is certain."""
        texts = self.literals(start, end)
        if len(texts) == 1:
            if placeholder == "%p":
                return f'"{texts[0]}"' if _PLAIN.fullmatch(texts[0]) else None
            return texts[0] if placeholder == "%s" else None
        if end - start != 1:
            return None
        token = self.tokens[start]
        if token.kind == "number" and _INTEGER.fullmatch(token.value):
            return token.value if placeholder in ("%s", "%d", "%i", "%p") else None
        if token.kind == "word" and token.value in _CONSTANTS:
            return token.value if placeholder in ("%s", "%p") else None
        return None

    def _row(self, title: str, row: Sequence[Range], index: int) -> Optional[str]:
        """*title* with row number *index* filled in, as jest does it; None
        when a value it takes is not known."""
        values = iter(row)
        out, end = [], 0
        if title.count("%#") > 1 or title.count("%$") > 1:
            return None  # jest fills in the first only
        for found in PLACEHOLDER.finditer(title):
            out.append(title[end : found.start()])
            end = found.end()
            if found.group() == "%%":
                out.append("%")
            elif found.group() in ("%#", "%$"):
                out.append(str(index + (found.group() == "%$")))
            else:
                at = next(values, None)
                if at is None:
                    out.append(found.group())
                    continue
                value = self._value(*at, found.group())
                if value is None:
                    return None
                out.append(value)
        return "".join(out) + title[end:]

    def _items(self, start: int, end: int) -> Optional[List[Range]]:
        """The items of the list at token *start* (a ``[``) of start..end; None
        when it is not a list alone (``as const`` may follow)."""
        if start >= end or not _is(self.tokens[start], "["):
            return None
        last = self.close[start]
        after = self.tokens[last + 1] if last + 1 < end else None
        if after is not None and after.value not in ("as", "satisfies"):
            return None
        items = self.split(start + 1, last, ",")
        if items[-1][0] == items[-1][1]:
            items.pop()  # empty, or after a trailing comma
        return items

    def _declared(self, name: str, at: int) -> Optional[Range]:
        """The value of the one ``const`` *name* declared at the top of the
        file or in a block around token *at*. None when there is none, or more
        than one."""
        found: List[Range] = []
        around: List[int] = []
        for number in range(len(self.tokens) - 2):
            while around and around[-1] <= number:
                around.pop()
            if number in self.close and _is(self.tokens[number], "{"):
                around.append(self.close[number])
            keyword, declared = self.tokens[number], self.tokens[number + 1]
            if (
                (keyword.kind, keyword.value) != ("word", "const")
                or declared.value != name
                or any(closing < at for closing in around)
            ):
                continue
            end = number + 2
            while end < len(self.tokens) and not _is(self.tokens[end], ";", "}"):
                end = self.close.get(end, end) + 1
            parts = self.split(number + 2, end, "=")
            found.append(parts[1] if len(parts) == 2 else (end, end))
        return found[0] if len(found) == 1 else None

    def _rows(self, start: int, end: int) -> Optional[List[List[Range]]]:
        """The rows of the ``.each`` table in tokens start..end, each a list of
        values: a list of lists or of literals, or a ``const`` of this file
        that is one. None for any other, such as rows that are objects."""
        if end - start == 1 and self.tokens[start].kind == "word":
            declared = self._declared(self.tokens[start].value, start)
            if declared is None:
                return None
            start, end = declared
        items = self._items(start, end)
        if not items:
            return None
        lists = [self._items(a, b) for a, b in items]
        if all(row is not None for row in lists):
            return [row for row in lists if row is not None]
        for first, last in items:
            token = self.tokens[first]
            if token.kind not in ("string", "template", "number") and not (
                last - first == 1 and token.value in _CONSTANTS
            ):
                return None
        return [[item] for item in items]

    def title(self, line: int, texts: Tuple[str, ...], table: Optional[Range]) -> Title:
        """The title of a call that is one of *texts*; *table* is the argument
        of its ``.each`` (an empty range for a tagged template), None without
        one."""
        if table is None:
            return Title(line, exact=texts)
        tagged = table[0] == table[1]
        rows = self._rows(*table)
        exact: List[str] = []
        written: List[str] = []
        for text in texts:
            takes = any(p != "%%" for p in PLACEHOLDER.findall(text))
            # jest fills in a $name in a tagged template's titles, and in a
            # list of objects' when the title has no % placeholder.
            if VARIABLE.search(text) and (tagged or (rows is None and not takes)):
                continue
            written.append(text)
            filled = [self._row(text, row, n) for n, row in enumerate(rows or [])]
            exact += [title for title in filled if title is not None]
        return Title(line, tuple(dict.fromkeys(exact)), tuple(written))

    def _generics(self, number: int) -> Optional[int]:
        """The token after the ``<...>`` at *number*; *number* when there is
        none, None when it does not close."""
        if not _is(self.token(number), "<"):
            return number
        depth = 0
        while (token := self.token(number)) is not None:
            if number in self.close:
                number = self.close[number]
            elif _is(token, "<"):
                depth += 1
            elif _is(token, ">"):
                depth -= 1
                if depth == 0:
                    return number + 1
            number += 1
        return None

    def call(self, number: int) -> Optional[Tuple[Set[str], Optional[Range], int]]:
        """The call of a test or describe function at token *number*: its name
        and modifiers, its ``.each`` table, and the token that opens its
        arguments. None when it is not such a call."""
        token, before = self.tokens[number], self.token(number - 1)
        if token.kind != "word" or token.value not in TESTS | SUITES:
            return None
        if _is(before, ".") or (
            before is not None and before.kind == "word" and before.value == "function"
        ):
            return None
        names: Set[str] = {token.value}
        table: Optional[Range] = None
        number += 1
        while _is(self.token(number), ".") and table is None:
            word = self.token(number + 1)
            number += 2
            if word is None or word.kind != "word":
                return None
            if word.value in MODIFIERS:
                names.add(word.value)
                continue
            opening = self._generics(number) if word.value == "each" else None
            if opening is None or opening >= len(self.tokens):
                return None
            if _is(self.tokens[opening], "("):
                number = self.close[opening] + 1
                table = self.split(opening + 1, self.close[opening], ",")[0]
            elif self.tokens[opening].kind == "template":
                number = opening + 1
                table = (opening, opening)
            else:
                return None
        return (names, table, number) if _is(self.token(number), "(") else None

    def source(self) -> Source:
        tests: List[Test] = []
        suites: List[Title] = []
        only: Optional[int] = None
        # The describe blocks around a token: (the token that closes it, its
        # title, whether it is skipped).
        around: List[Tuple[int, Title, bool]] = []
        for number, token in enumerate(self.tokens):
            while around and around[-1][0] <= number:
                around.pop()
            found = self.call(number)
            if found is None:
                continue
            names, table, opening = found
            line = self.scanner.line(token.at)
            first = self.split(opening + 1, self.close[opening], ",")[0]
            title = self.title(line, self.literals(*first), table)
            skipped = bool(names & SKIPS) or any(s for _, _, s in around)
            if only is None and names & ONLYS:
                only = line
            if names & TESTS:
                tests.append(Test(title, tuple(t for _, t, _ in around), skipped))
            else:
                suites.append(title)
                around.append((self.close[opening], title, skipped))
        return Source(tuple(tests), tuple(suites), only)


def read(text: str, *, jsx: bool) -> Source:
    """The tests *text* declares. Raises ``Unreadable`` when it does not scan."""
    try:
        return _Calls(text, jsx).source()
    except RecursionError:
        raise Unreadable("it nests too deeply") from None


# ---------------------------------------------------------------------------
# A target's name
# ---------------------------------------------------------------------------
#: ``rc_evidence.jest_title_pattern``: a target's name as the pattern the
#: release matches jest's reported titles with.
Pattern = Callable[[str], "re.Pattern[str]"]
SHOWN = 3


def _filled(text: str) -> str:
    """*text*, an ``.each`` title, with a value in each placeholder."""
    return PLACEHOLDER.sub(lambda m: "%" if m.group() == "%%" else "value", text)


class Names:
    """The names a target may give the tests of *source*: a test's own title,
    or its full name (its describe titles and its own, joined by spaces).

    Compared as the release's *pattern* of them, so a name counts only when
    the release would match it with the title jest reports, and the letter of a
    placeholder does not matter. Narrower than the release in one thing: an
    ``.each`` title is named as written or with a whole row filled in, so a
    placeholder stands only where the title has one (``%s`` is not the name of
    every test in a file)."""

    def __init__(self, source: Source, pattern: Pattern) -> None:
        self.source = source
        self._pattern = pattern
        self._known: Dict[Title, Set[str]] = {}
        self._own: Dict[str, List[Test]] = {}
        for test in source.tests:
            for spelling in self._spellings(test.title):
                self._own.setdefault(spelling, []).append(test)

    def _spellings(self, title: Title) -> Set[str]:
        """The patterns of the names that are *title*."""
        if title not in self._known:
            found = set()
            for text in title.written:
                pattern = self._pattern(text)
                if pattern.fullmatch(_filled(text)):
                    found.add(pattern.pattern)
            for text in title.exact:
                found.add(re.escape(text))
                pattern = self._pattern(text)
                if pattern.fullmatch(text):
                    found.add(pattern.pattern)
            self._known[title] = found
        return self._known[title]

    def _is_full_name(self, wanted: str, test: Test) -> bool:
        space = re.escape(" ")
        rest = {wanted}
        for suite in test.suites:
            rest = {
                left[len(text) + len(space) :]
                for left in rest
                for text in self._spellings(suite)
                if left.startswith(text + space)
            }
        return bool(rest & self._spellings(test.title))

    def problem(self, name: str) -> Optional[str]:
        """Why *name* is not a test of the file that runs; None when it is."""
        if self.source.only is not None:
            return f"line {self.source.only} is .only: jest skips the other tests"
        wanted = self._pattern(name).pattern
        named = self._own.get(wanted, []) + [
            test
            for test in self.source.tests
            if test.suites and self._is_full_name(wanted, test)
        ]
        if named:
            skipped = [test.title.line for test in named if test.skipped]
            return f"the test at line {skipped[0]} is skipped" if skipped else None
        said = f"no test is titled {name!r}"
        if any(wanted in self._spellings(suite) for suite in self.source.suites):
            said += " (a describe block is: name a test in it)"
        near = [
            text
            for test in self.source.tests
            for text in test.title.written or test.title.exact
            if name in text or text in name or self._pattern(text).fullmatch(name)
        ]
        if near:
            shown = ", ".join(repr(text) for text in list(dict.fromkeys(near))[:SHOWN])
            said += f"; one is titled {shown}"
        unread = [t.title.line for t in self.source.tests if not t.title.readable]
        if len(unread) == 1:
            said += f"; the title at line {unread[0]} cannot be read"
        elif unread:
            lines = ", ".join(str(line) for line in unread[:SHOWN])
            more = f" and {len(unread) - SHOWN} more" if len(unread) > SHOWN else ""
            said += f"; the titles at lines {lines}{more} cannot be read"
        return said
