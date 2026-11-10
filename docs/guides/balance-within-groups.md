# Balance the split within groups (beta)

An experiment that balances its split within groups gives each group of users the same split as
the whole experiment, as they are assigned. With a 50/50 split balanced within `tier`, the users of
each tier are split 50/50 among themselves, so a lucky mix of tiers cannot tip the result. You set
`balance_within` on the experiment, and send the attribute in each assign request's `context`.

## How it works

Within each group, each new user takes the next place in a block, a shuffled list of the
experiment's split. Every user's chance of each variant is still its share. For the users balanced
in the blocks, a variant's count in a group stays within a few users of its share at every moment,
and is exactly its share each time the group completes a block: at 50/50, the two variants of a
group never differ by more than one user.

The global holdout, the exclusion group and the targeting rules come first: only users who take
part are balanced. A user keeps their variant and their group if their attribute changes later. An
experiment that does not set `balance_within` assigns its users exactly as before.

## Turn it on

Send `balance_within` when you create the experiment, or with `PUT /api/v1/experiments/{id}` while
it is a draft:

```json
{"balance_within": {"attribute": "tier", "values": ["high_value", "churn_risk", "new"]}}
```

The response echoes `balance_within`. Check that it does: a server version without this feature
ignores the field and still answers `200`.

- `attribute` is a top-level key of the assign `context`, such as `tier` or `country`.
- `values` lists the values to keep apart, 1 to 20 of them, each matched exactly: `Gold` and
  `gold` are different values.
- A user whose value is not listed is balanced with the others in `other`, and a user without the
  key in `unknown`.
- `null` turns balancing off on a draft. Once the experiment has started, `balance_within` cannot
  be changed.
- A clone balances within the same groups, and its groups start from their first place.

## Assign users

Put the attribute at the top level of `context` in every assign request:

```json
{"experiment_key": "pricing-test", "user_id": "u-123", "context": {"tier": "high_value"}}
```

A nested key (`{"user": {"tier": "high_value"}}`) or a dotted one (`user.tier`) is not read, so that
user is balanced in `unknown`. `POST /api/v1/tracking/assign` and
`POST /api/v1/tracking/assign/batch` take their places from the same groups, so users assigned one
at a time and users of an uploaded list are balanced together (see
[Assign a customer list](assign-customer-list.md#balance-each-segment-of-the-list)). The same list
sent in another order can split differently; each user keeps the variant they got.

## Balance within saved segments

Instead of an attribute, `balance_within` can list 1 to 10 [saved segments](segments.md) by their
id:

```json
{"balance_within": {"segments": ["8d4c6f0e-3a1b-4c2d-9e8f-7a6b5c4d3e2f", "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b"]}}
```

- A new user is balanced in the group of the first listed segment they belong to, so for a user
  in two of them the order of the list decides. A user in none of them is balanced in `other`.
- Membership is decided as it is for targeting rules: an ID list by the assign request's
  `user_id`, a rules segment by its `context`.
- When the membership of a segment listed before the first one the user belongs to cannot be
  decided, the user is balanced in `unknown`.
- A user keeps their variant and their group if they leave the segment later.
- Every segment must exist and be active, and a rules segment's rules must be valid.
- A segment that a draft, active or paused experiment balances within cannot be archived or made
  inactive: the request answers `409`, as for a segment that targeting rules use. A clone answers
  `409` when a segment it would balance within cannot be used.

## What is refused

The request answers `422` with the reason when:

- the attribute has a dot, has a character other than letters, digits, `_` and `-`, or is
  `user_id` or `segment`;
- `values` is missing (`web_device`, `web_visitor` and `web_browser` need none: their values are a
  fixed set), has more than 20 values, lists `other` or `unknown`, or lists a value twice;
- a share of the split is not a multiple of 5, such as 34/33/33 (50/50, 20/80, 35/35/30 and 40/30/30
  are allowed);
- the experiment is a bandit or a split URL experiment;
- both `attribute` and `segments` are sent, or neither is;
- `segments` has more than 10 ids, lists one twice, or names a segment that does not exist, is
  archived or inactive, or whose rules are not valid.

Changing `balance_within` after the experiment has started answers `400`.

## Users assigned without balancing

When the server is busy balancing other users at that moment, a new user is assigned by the
experiment's ordinary hash instead. They keep their group, their chance of each
variant is still its share, and they are counted in the `experiment_balance_fallback_total` metric
by reason (`timeout` or `busy`). An older server version, during a rolling deploy or a rollback,
assigns every new user by the hash and stores no group. [Limitations](../getting-started/limitations.md#experiments-and-results)
lists what balancing does not do.
