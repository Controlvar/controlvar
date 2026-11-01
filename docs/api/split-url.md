# Server-Side Split URL Testing API

!!! info "Part of the `split_url` module"
    Split URL testing is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

!!! warning "The edge router does not split traffic in this release"
    The Lambda@Edge router reads its configuration only from an `X-Split-URL-Config` request
    header, and nothing sets that header: no stack creates the CloudFront distribution or adds
    the header, and the platform does not send an experiment's `split_url_config` to the edge.
    Without it the router passes every request through unchanged, so deploying the construct
    does not split traffic. What works today is storing a split URL experiment and its
    configuration, and the preview endpoint. The preview does not predict the router either:
    it hashes the `user_id` you pass with the experiment's id, while the router hashes the
    viewer's IP address and User-Agent with the `experiment_key` in its configuration
    ([#393](https://github.com/Controlvar/controlvar/issues/393)).

This document describes the server-side split URL testing feature. Split URL experiments are meant to redirect different users to distinct URLs (e.g., `/checkout` vs `/checkout-v2`) at the CloudFront layer, with a Lambda@Edge router and a cookie that keeps each viewer on one variant.

---

## Overview

Split URL testing differs from classic A/B tests in that the **entire page or URL path** varies between variants rather than a component within a single page. Given a configuration, the router (`modules/lambda/split_url_router/handler.py`):

1. Reads the viewer's assignment cookie (`split_url_{experiment_key}` unless the configuration names another).
2. If the cookie holds one of the variant URLs, passes the request through.
3. Otherwise hashes the viewer's IP address and User-Agent to pick a variant.
4. Answers `302` to that variant's URL, with a cookie that lasts 30 days unless the configuration sets `cookie_ttl_days`.

---

## Experiment Type

Set `experiment_type` to `split_url` when creating an experiment.

### `SplitUrlConfig` Schema

`split_url_config` holds the URL variants:

| Field | Type | Required | Description |
|---|---|---|---|
| `variants` | `array` | Yes | The URL variants: at least 2 |
| `variants[].name` | `string` | Yes | The name of the experiment variant this URL is for |
| `variants[].url` | `string` | Yes | The URL for this variant: an absolute `http://` or `https://` URL (see [Validation Rules](#validation-rules)), stored as given |
| `variants[].traffic_allocation` | `number` | Yes | Percentage of traffic: each from 0 to 100, they must add up to 100, and each equals its experiment variant's |
| `cookie_name` | `string` | No | The router's cookie name; `null` means `split_url_{experiment_key}` |
| `cookie_ttl_days` | `int` | No | How long the router's cookie lasts, in days (default `30`) |
| `canonical_url` | `string` | No | Stored with the configuration; nothing reads it yet |

The first URL variant is the router's control, which a holdout sends its viewers to.

---

## Create a Split URL Experiment

```
POST /api/v1/experiments/
```

**Example Request**

```bash
curl -X POST "https://your-platform.example.com/api/v1/experiments/" \
  -H "Authorization: Bearer your_access_token" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Checkout Flow Split URL Test",
    "hypothesis": "The new checkout URL reduces drop-off by 10%",
    "experiment_type": "split_url",
    "variants": [
      { "name": "Control",   "is_control": true,  "traffic_allocation": 50 },
      { "name": "Treatment", "is_control": false, "traffic_allocation": 50 }
    ],
    "metrics": [
      { "name": "Purchase", "event_name": "purchase", "metric_type": "conversion", "is_primary": true }
    ],
    "split_url_config": {
      "variants": [
        { "name": "Control",   "url": "https://example.com/checkout",    "traffic_allocation": 50 },
        { "name": "Treatment", "url": "https://example.com/checkout-v2", "traffic_allocation": 50 }
      ]
    }
  }'
```

**Response: 201 Created** (abridged: the response is the whole experiment)

```json
{
  "id": "acf727d0-fd25-41d6-9813-7335d9ad189f",
  "name": "Checkout Flow Split URL Test",
  "experiment_type": "split_url",
  "status": "draft",
  "split_url_config": {
    "variants": [
      { "url": "https://example.com/checkout",    "name": "Control",   "traffic_allocation": 50.0 },
      { "url": "https://example.com/checkout-v2", "name": "Treatment", "traffic_allocation": 50.0 }
    ],
    "cookie_name": null,
    "canonical_url": null,
    "cookie_ttl_days": 30
  }
}
```

---

## Split URL Preview Endpoint

```
GET /api/v1/experiments/{experiment_id}/split-url/preview
```

Returns the URL variant the preview's own hash gives a `user_id`, without redirecting. It hashes
`{user_id}:{experiment_id}`, so it does not tell you what the router would do for a viewer (see
the note at the top of this page). It is for ADMIN and DEVELOPER users (and superusers), and in
a core deployment it answers `501`.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `experiment_id` | `string` (UUID) | The experiment to preview |

**Query Parameters**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `user_id` | `string` | Yes | User identifier to hash |

**Example Request**

```bash
curl -X GET "https://your-platform.example.com/api/v1/experiments/acf727d0-fd25-41d6-9813-7335d9ad189f/split-url/preview?user_id=alice" \
  -H "Authorization: Bearer your_access_token"
```

**Response: 200 OK**

```json
{
  "experiment_id": "acf727d0-fd25-41d6-9813-7335d9ad189f",
  "user_id": "alice",
  "variant_name": "Treatment",
  "url": "https://example.com/checkout-v2",
  "traffic_allocation": 50.0
}
```

---

## CloudFront CDK Construct

`modules/infrastructure/constructs/split_url_distribution.py` provisions a
CloudFront distribution wired to a Lambda@Edge viewer-request function.

---

## Validation Rules

On create and update, `split_url_config` is checked for:

- at least 2 URL variants, with unique names;
- `traffic_allocation` values that are each from 0 to 100 and add up to 100 (within 0.01), none of them NaN or infinite;
- a `url` for each that is an absolute `http://` or `https://` URL of at most 2048 characters with an ASCII host name without a `%` sign (an internationalised name in its `xn--` form), without a user name or password, a `#` fragment, a backslash, whitespace or control characters, and whose path has no single-dot or double-dot segment (such as `/./` or `/../`, also written with `%2e`) and no character a browser percent-encodes in a path: a non-ASCII character, `"`, `<`, `>`, `^`, a backquote, `{`, `|` or `}`, which you write percent-encoded, as the browser sends it (`é` as `%C3%A9`).

A URL is stored exactly as sent.

In a `split_url` experiment the configuration must also match the experiment's variants:

- each URL variant names one of the experiment's variants, and each of those has exactly one URL variant;
- each URL variant's `traffic_allocation` equals its experiment variant's, within 0.01: the experiment's variants decide the split;
- no URL variant but the control's has the control URL's host and path.

The control URL is the URL variant named like the experiment's control variant. Hosts are
compared without case or port, and paths without one trailing `/`; the query string is not
compared. A variant whose URL has the control URL's path on another host is accepted.

A refusal answers `422` at the rule's place, such as
`["body", "split_url_config", "variants", 1, "name"]`, with a fixed message that names the
variant and never repeats a URL. An update is checked on its result: a `PUT` that sends
`variants`, `split_url_config` or `experiment_type` is checked with whatever it leaves out
taken from what is stored, so `variants` sent alone are checked against the stored
configuration, and the refusal is then at `["body", "variants"]` (or
`["body", "experiment_type"]`).

A configuration stored before a rule is kept as it was: a `PUT` that sends none of those
three fields works, and one that sends the configuration back must correct it. An experiment
of another type can carry a configuration, which is held to the first list only, and a
`split_url` experiment can be created without one (its preview then answers `400`).

### Starting a split URL experiment

`POST /api/v1/experiments/{id}/start` answers `400`, with a `detail` that says what to change
and that it is changed with `PUT /api/v1/experiments/{id}`, when the split URL experiment:

1. has no `split_url_config`;
2. has one that breaks a rule above;
3. has the same control URL host and path as an active split URL experiment;
4. would chain with an active split URL experiment: its control URL has the host and path of that experiment's variant URL, or one of its variant URLs has the host and path of that experiment's control URL.

Refusing every such link refuses chains and loops of any length. The same path on another
host is neither the same control URL nor a chain. An active split URL experiment whose
stored configuration breaks a rule is left out of the last two checks. When the scheduler
starts a draft at its `start_date`, it makes the first two checks and leaves a draft that
fails them as a draft.

---

## Error Responses

| Status | Meaning |
|---|---|
| `400 Bad Request` | Preview: the experiment is not a `split_url` experiment, or has no `split_url_config`. Start: see [Starting a split URL experiment](#starting-a-split-url-experiment) |
| `401 Unauthorized` | Missing or invalid Bearer token |
| `403 Forbidden` | Preview: the user is not ADMIN, DEVELOPER or a superuser |
| `404 Not Found` | Preview: no experiment has this id |
| `409 Conflict` | Preview: the stored `split_url_config` is not valid, such as an allocation outside 0 to 100 or a URL saved before it was checked; update it with `PUT /api/v1/experiments/{id}` |
| `422 Unprocessable Entity` | Create or update: the body fails validation, such as allocations that do not sum to 100 or URL variants that do not match the experiment's variants |
| `501 Not Implemented` | Preview, or creating a `split_url` experiment, in a core deployment |

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "split_url_config"],
      "msg": "Value error, Traffic allocations must sum to 100, got 95.0",
      "ctx": { "error": {} }
    }
  ]
}
```
