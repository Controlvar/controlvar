# Benjamini-Hochberg FDR Correction

The Benjamini-Hochberg (BH) procedure controls the **False Discovery Rate (FDR)** — the
expected proportion of false positives among all rejected hypotheses — when testing
multiple metrics simultaneously.

## The BH Procedure

Given $m$ raw p-values $p_1, p_2, \ldots, p_m$ and a desired FDR threshold $\alpha$:

1. **Sort** p-values ascending: $p_{(1)} \leq p_{(2)} \leq \cdots \leq p_{(m)}$.
2. **Compute** the BH threshold for rank $k$: $T_k = \frac{k}{m} \times \alpha$.
3. **Find** the largest rank $k^*$ such that $p_{(k^*)} \leq T_{k^*}$.
4. **Reject** all hypotheses with rank $\leq k^*$ (declare significant).

### Example

10 p-values with $\alpha = 0.10$:

| Rank | p-value | BH threshold $k/10 \times 0.10$ | Reject? |
|---|---|---|---|
| 1 | 0.001 | 0.010 | Yes |
| 2 | 0.008 | 0.020 | Yes |
| 3 | 0.039 | 0.030 | No |
| 4 | 0.041 | 0.040 | No |
| 5 | 0.042 | 0.050 | Yes |
| 6 | **0.060** | **0.060** | **Yes** ← largest k* |
| 7 | 0.074 | 0.070 | No |
| 8 | 0.205 | 0.080 | No |
| 9 | 0.212 | 0.090 | No |
| 10 | 0.391 | 0.100 | No |

Result: Reject 6 hypotheses (all with rank $\leq 6$), even though ranks 3–5 individually
don't satisfy their per-rank threshold — the step-up property means we look for the
largest k, so ranks 1, 2, 5, 6 passing brings along ranks 3 and 4.

At $\alpha = 0.05$ with the same p-values, only 2 are significant
(k=1: 0.001≤0.005 ✓, k=2: 0.008≤0.010 ✓, k=3: 0.039>0.015 ✗).

### Adjusted P-values

The BH-adjusted p-value for rank $k$ is:

$$p_{\text{adj}}(k) = \min\left(\frac{p_{(k)} \cdot m}{k},\ 1.0\right)$$

with monotonicity enforced from right to left:
$$p_{\text{adj,mono}}(k) = \min\left(p_{\text{adj}}(k),\ p_{\text{adj,mono}}(k+1)\right)$$

A metric is significant when its rank is at most $k^*$, that is, when its adjusted p-value is at most $\alpha$.

## REST API Reference

### POST `/api/v1/results/{experiment_id}/fdr-correction`

Apply Benjamini-Hochberg FDR correction to per-metric p-values. The p-values are the
ones you send: the experiment in the path must exist, but its own data isn't read. Any
logged-in user can call it.

**Request body:**

```json
{
  "p_values": {
    "revenue": 0.001,
    "click_through_rate": 0.032,
    "session_duration": 0.08,
    "churn_rate": 0.41,
    "engagement_score": 0.015
  },
  "fdr_threshold": 0.05
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `p_values` | `dict[str, float]` | Yes | — | Metric name → raw p-value. All values in [0,1]. Non-empty. |
| `fdr_threshold` | `float` | No | `0.05` | FDR control level α in [0,1]. |

**Response (200):**

```json
[
  {
    "metric_name": "revenue",
    "raw_p_value": 0.001,
    "adjusted_p_value": 0.005,
    "rank": 1,
    "is_significant": true
  },
  {
    "metric_name": "engagement_score",
    "raw_p_value": 0.015,
    "adjusted_p_value": 0.0375,
    "rank": 2,
    "is_significant": true
  },
  {
    "metric_name": "click_through_rate",
    "raw_p_value": 0.032,
    "adjusted_p_value": 0.0533,
    "rank": 3,
    "is_significant": false
  },
  {
    "metric_name": "session_duration",
    "raw_p_value": 0.08,
    "adjusted_p_value": 0.1,
    "rank": 4,
    "is_significant": false
  },
  {
    "metric_name": "churn_rate",
    "raw_p_value": 0.41,
    "adjusted_p_value": 0.41,
    "rank": 5,
    "is_significant": false
  }
]
```

The response is sorted by rank ascending (smallest raw p-value first).

Run the commands on this page in one terminal, in order, against the stack from the
[Quick Start](../getting-started/quick-start.md). Each uses the shell variables set by the
ones before it. Log in first:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`. This saves the id of the demo data's `checkout_button_color`
experiment in `$EXP_ID`, then corrects the five p-values above:

```{.bash exec}
EXP_ID=$(curl -s localhost:8000/api/v1/experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  | jq -r '.items[] | select(.key == "checkout_button_color") | .id')

curl -s -X POST localhost:8000/api/v1/results/$EXP_ID/fdr-correction \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "p_values": {
      "revenue": 0.001,
      "click_through_rate": 0.032,
      "session_duration": 0.08,
      "churn_rate": 0.41,
      "engagement_score": 0.015
    },
    "fdr_threshold": 0.05
  }' | jq -r '.[] | "\(.rank) \(.metric_name) \(.is_significant)"'
```
<!-- expect: 1 revenue true -->
<!-- expect: 2 engagement_score true -->
<!-- expect: 3 click_through_rate false -->
<!-- expect: 4 session_duration false -->
<!-- expect: 5 churn_rate false -->

It prints each metric's rank, name and whether it is significant: `revenue` and
`engagement_score` are, the other three are not, as in the response above.

**Error codes:**

| Status | Condition |
|---|---|
| 404 | Experiment not found |
| 422 | Empty `p_values`, any value outside [0,1], or `fdr_threshold` outside [0,1] |
| 500 | Internal computation error |

## Integration with the Existing Results API

Each experiment stores a `correction_method` (`none`, `bonferroni` or
`benjamini_hochberg`), Benjamini-Hochberg unless it was created with another, and
`GET /api/v1/results/{experiment_id}` applies it across the treatments of each metric:
each treatment then carries an `adjusted_p_value`, and `is_significant` is decided on
it. With one treatment the adjusted p-value equals the p-value and no decision changes.
The stored method can be changed only while the experiment is a draft.

A request can name another method for itself with the `correction_method` query
parameter; `none` shows the uncorrected numbers. The response's `correction_method`
says which one was applied:

```{.bash exec}
curl -s "localhost:8000/api/v1/results/$EXP_ID?correction_method=benjamini_hochberg" \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{correction_method, metric: .metrics[0].metric_name}'
```
<!-- expect: "correction_method": "benjamini_hochberg" -->
<!-- expect: "metric": "Checkout Completion" -->

It prints the correction applied and the experiment's metric, `Checkout Completion`.

The `POST /api/v1/results/{experiment_id}/fdr-correction` endpoint provides finer
control: you supply any set of p-values (from post-stratification, CUPED, or raw
analysis) and receive BH-corrected results immediately.
