# Targeting Rules: Operator Reference

This is the operator reference for targeting rules: what each operator matches, and the value
it takes. Flag targeting and experiment targeting answer every operator on this page the same
way.

## Two shapes

Rules are saved in one of two shapes:

- **Dashboard**, the shape the dashboard's rule builder writes:
  `{"logical_operator", "groups": [{"logical_operator", "conditions": [{"attribute", "operator", "value"}]}]}`.
  Its operators are the dashboard names in the table below. Segment rules take this shape
  only.
- **Native**: `{"rules": [{"id", "rule": {"operator", "conditions", "groups"}, "rollout_percentage", "priority"}], "default_rule"}`.
  A condition is `{"attribute", "operator", "value", "additional_value"}`, with the native
  names below. Groups can hold groups.

A list of rules, the legacy `[{"type": ...}]` shape, is refused when it is saved.
[Creating Feature Flags](feature-flags/create.md#add-targeting-rules) lists what else is
refused, and how the request's context is matched against the attributes.

Each level combines its conditions and groups with `AND` (all of them hold), `OR` (at least
one holds) or `NOT` (not all of them hold).

## Operators

A condition on an attribute the request's context does not carry does not hold, except
`is_null`; a segment condition reads no attribute.

| Dashboard | Native | The condition holds when |
|---|---|---|
| `equals` | `eq` | the attribute equals the value. Text and a number or boolean are compared as that type (`"17"` equals `17`, `"true"` equals `true`); two texts are compared as they are. |
| `not_equals` | `neq` | the attribute does not equal the value, compared as `equals` compares. |
| `greater_than`, `greater_than_or_equal`, `less_than`, `less_than_or_equal` | `gt`, `gte`, `lt`, `lte` | the attribute compares so with the value, both read as numbers: text that is a number (`"21"`) counts. |
| `in` | `in` | the attribute equals one of the values, compared as `equals` compares. |
| `not_in` | `not_in` | the attribute equals none of the values. |
| `contains`, `not_contains` | `contains`, `not_contains` | the attribute, as text, contains the value, or does not. |
| `starts_with`, `ends_with` | `starts_with`, `ends_with` | the attribute starts, or ends, with the value. |
| `regex` | `match_regex` | the attribute contains a match for the pattern ([below](#match_regex-match_regex)). |
| `is_null` | `is_null` | the attribute is missing or empty: `null`, blank text, or an empty list or object. |
| `is_not_null` | `is_not_null` | the attribute is there and is not empty in that sense. |
| `array_contains` | `contains_all` | the attribute is a list holding every value, or text containing every value. |
| `array_intersects` | `contains_any` | the attribute is a list holding at least one of the values, or text containing one of them. |
| `semver_eq`, `semver_gt`, `semver_lt`, `semver_gte`, `semver_lte` | `semantic_version`, with `additional_value` `eq`, `gt`, `lt`, `gte` or `lte` | the attribute is a version that compares so with the value ([below](#semantic_version-semantic_version)). |
| `geo_within_radius` | `geo_distance` | the attribute is a point within the radius ([below](#geo_distance-geo_distance)). |
| `time_window` | `time_window` | the attribute is a moment inside the window ([below](#time_window-time_window)). |
| `in_segment`, `not_in_segment` | `in_segment`, `not_in_segment` | the user is, or is not, a member of the segment ([below](#in_segment-in_segment-not_in_segment-not_in_segment)). |
| none | `between` | the attribute is a number, or a date, from `value` to `additional_value`, both included. |
| none | `before`, `after` | the attribute is a date before, or after, the value ([below](#before-before-after-after)). |
| none | `array_length` | the attribute is a list, text or object whose length compares with the value as `additional_value` says: `eq` (the default), `gt`, `gte`, `lt`, `lte`, or `between` a `{"min", "max"}` value, both included. A number or `null` has no length and does not match. |

The native shape also accepts `json_path` and `percentage_bucket`. Flag targeting and
experiment targeting do not answer them alike, so they are not documented here.

A native condition may also carry `attribute_type`. Neither flag targeting nor experiment
targeting reads it: the condition is answered from the attribute's value alone.

## Operator details

### MATCH_REGEX (`match_regex`)
Tests if the value contains a match for the pattern (a search, not an anchored
match: use `^` and `$` to anchor). Patterns use
[RE2 syntax](https://github.com/google/re2/wiki/Syntax), which differs from
Python's `re`:

- `\w`, `\d`, `\s` and `\b` are ASCII-only; use `\p{L}`, `\p{N}` or
  `[[:alpha:]]` for other scripts.
- `$` matches only at the very end of the value, not before a final newline.
- Lookahead, lookbehind, backreferences, `\Z`, `(?x)`, `(?u)`, `(?a)`,
  `\N{...}`, a counted repetition over 1000, and very large Unicode repetitions
  such as `[\p{L}\p{N}]{1,300}` are refused.
- Values longer than 256 characters are not evaluated.

When a pattern is refused or a value cannot be evaluated, the whole ruleset is
not applied for that evaluation: a flag evaluates disabled with reason
`error`, a user is not eligible for the experiment (control), and a user is
not a member of the segment. `python -m backend.scripts.check_regex_rules`
lists stored patterns that are refused or use the ASCII-only classes or `$`.
A pattern RE2 refuses answers 422 when experiment or feature-flag targeting
rules are saved; `python -m backend.scripts.check_targeting_rules` lists the
flags whose stored rules a save would now refuse, including such patterns.

### SEMANTIC_VERSION (`semantic_version`)

The attribute and the value are versions such as `1.2.3` or `1.2.3-beta.1`; a leading `v` on
the attribute is ignored. An attribute that is not a whole version, such as `"17.4"`, does not match. In the
dashboard shape a short value is filled in: `17` is `17.0.0` and `17.4` is `17.4.0`. A
pre-release comes before its release (`1.2.3-beta.1` is lower than `1.2.3`), and `eq` ignores
build metadata (`1.0.0+build.1` equals `1.0.0+build.2`). A native condition with no
`additional_value` compares with `eq`.

### GEO_DISTANCE (`geo_distance`)

The attribute is a point: `[latitude, longitude]`, or an object with `lat` and `lon` (or
`latitude` and `longitude`). The condition holds when the great-circle distance from the
rule's point is at most the radius, in kilometres unless the rule gives `miles` or `meters`.

- Dashboard: `{"lat": 37.7749, "lon": -122.4194, "radius": 15, "unit": "km"}`, the list
  `[37.7749, -122.4194, 15, "km"]` or the text `"37.7749,-122.4194,15,km"`. The unit is
  optional.
- Native: `value` is `[latitude, longitude]`, and `additional_value` is the radius, or
  `{"radius": 15, "unit": "km"}`.

### TIME_WINDOW (`time_window`)

Tests if a moment falls within a time window. The moment is the attribute's
value: an ISO 8601 string, a Unix timestamp or a `datetime`. An attribute sent
as `null` means the current time; an attribute that is not sent does not match.

```json
{
  "attribute": "current_time",
  "operator": "time_window",
  "value": {
    "start": "09:00",
    "end": "17:00",
    "start_time": "09:00",
    "end_time": "17:00",
    "days": [0, 1, 2, 3, 4],
    "start_date": "2024-01-01",
    "end_date": "2024-01-31"
  }
}
```

This window is open from 09:00 to 17:00, Monday to Friday, from 1 to 31 January
2024.

**Keys that say when the window is open** (every key given must hold):
- `start_time`, `end_time`: `HH:MM` or `HH:MM:SS`, both or neither. The end
  minute is included; a start after the end spans midnight (`22:00` to `02:00`).
- `days`: day numbers, 0 = Monday to 6 = Sunday.
- `start_date`, `end_date`: `YYYY-MM-DD`, both or neither; both days are included.

`timezone` (optional), a name such as `Europe/Paris`, reads the moment in that
timezone. Without it, the moment's own clock time is compared.

**`start` and `end`:** the `Condition` schema requires them, but the engine
does not read them. A rule saved from the dashboard stores `start` and `end` as
`HH:MM`, and those are read as `start_time` and `end_time`. In the native shape,
give `start_time` and `end_time` (or the other keys) as well, as the example
does. A window with none of those keys, or with one that cannot be read (`days`
not 0-6, a time that is not `HH:MM`, an unknown timezone, a date that is not
`YYYY-MM-DD`), is refused, and a warning is logged once. For a user whose
evaluation reaches the window, the evaluation is abandoned: the flag answers off
for that user, with reason `error`, the experiment does not enrol them, and a
segment that holds the window cannot be decided for them. A user decided by an
earlier rule never reaches it, and a window in `default_rule` is never read. So
a window with ISO datetimes in `start` and `end` is refused, in either shape
(accepting them is #1156). For a window between two moments, use `after` and
`before` (below) in an `AND` group.

Flag targeting and experiment targeting read a window the same way.

### BEFORE (`before`), AFTER (`after`)

`before` and `after` compare dates: an ISO 8601 string or a Unix timestamp, in the attribute
and in the value. The condition holds when the attribute is strictly before, or strictly
after, the value.

### IN_SEGMENT (`in_segment`), NOT_IN_SEGMENT (`not_in_segment`)

**`in_segment`, `not_in_segment`.** `{"attribute": "segment", "operator": "in_segment", "value": "<segment id>"}`.
One segment per condition; to match any of several, put one condition per segment in an `OR`
group. A ruleset can use at most 10 different segments. A user is a member of an ID-list segment
when the user the flag or experiment is evaluated for (the request's `user_id`) is in its list,
and of a rules segment when the attributes sent with the request match its rules; in a segment's
rules, `user_id` is that same user, whatever the context says. Membership is decided by the
server from its own records: nothing in the context you send makes a user a member. A condition
on the attribute `segment` with any other operator, such as `equals`, compares the context value
as before. Saving rules that name a segment that is unknown, inactive or archived, or whose rules
are not valid, answers 422, and a segment condition in a native `default_rule` (returned without
its conditions being evaluated) is refused. When the server cannot decide a user's membership of
a segment the rules name (a flag unarchived after its segment was archived, a segment rule whose
pattern cannot be evaluated for this context), it does not guess: the flag answers
`enabled: false` with `reason: "error"` and the experiment does not enrol the user
(`reason: "targeting"`), whichever of the two operators the condition uses. Flags that use a
segment are always evaluated by the server, never by an SDK's local evaluation.
A segment condition requires nothing from the context. A user who lacks an attribute is not
refused outright: on experiments, as on flags, they fail only the conditions on that attribute
(`is_null` passes), so another `OR` branch can still enrol them, and they match a `NOT` group on
that attribute (a user with no `country` matches `NOT (country equals US)`, but not
`country not_equals US`). Users already assigned to an experiment keep their assignment.

See [Segments](guides/segments.md#target-a-flag-or-an-experiment-at-a-segment).

## When a rule cannot be evaluated

Some conditions cannot be decided for a user: a `regex` pattern that is refused, or a value it
cannot evaluate; a `time_window` that cannot be read; a comparison with a number too large for
a floating-point number, or with a timestamp no date can hold; and a segment whose membership
the server cannot decide. The whole ruleset is abandoned for that user, so no later rule and
no `default_rule` applies, and a `NOT` around the condition does not turn it into a match:

- the flag answers `enabled: false` with `reason: "error"`;
- the user is not eligible for the experiment;
- a flag or an experiment that names a segment whose rules hold the condition answers as
  above, because the user's membership of that segment cannot be decided.

## Usage examples

These are dashboard-shape rules, as the builder saves them.

Users on iOS 15 or later, on an iPhone or an iPad:

```json
{
  "logical_operator": "AND",
  "groups": [
    {
      "logical_operator": "AND",
      "conditions": [
        {"attribute": "platform", "operator": "equals", "value": "iOS"},
        {"attribute": "os_version", "operator": "semver_gte", "value": "15"},
        {"attribute": "device_type", "operator": "in", "value": "iPhone, iPad"}
      ]
    }
  ]
}
```

A context of `{"platform": "iOS", "os_version": "16.2.0", "device_type": "iPhone"}` matches.

Users within 15 km of San Francisco:

```json
{
  "groups": [
    {
      "conditions": [
        {
          "attribute": "location",
          "operator": "geo_within_radius",
          "value": {"lat": 37.7749, "lon": -122.4194, "radius": 15}
        }
      ]
    }
  ]
}
```

A context with `"location": [37.8044, -122.2712]` (Oakland, 13.4 km away) matches; with a
radius of 10 it does not.

US users over 18, or verified Canadian users:

```json
{
  "logical_operator": "OR",
  "groups": [
    {
      "logical_operator": "AND",
      "conditions": [
        {"attribute": "country", "operator": "equals", "value": "US"},
        {"attribute": "age", "operator": "greater_than", "value": "18"}
      ]
    },
    {
      "logical_operator": "AND",
      "conditions": [
        {"attribute": "country", "operator": "equals", "value": "CA"},
        {"attribute": "verified", "operator": "equals", "value": "true"}
      ]
    }
  ]
}
```
