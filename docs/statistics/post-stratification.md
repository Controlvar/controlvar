# Post-Stratification Variance Reduction

!!! warning "Not available through the API yet"
    `POST /api/v1/results/{experiment_id}/post-stratification` answers
    **501 Not Implemented**. The estimator exists as a
    Python service (`PostStratificationService`), but nothing yet builds its
    input -- one metric value and the stratum attributes per assigned user --
    from an experiment's recorded assignments and events.

## REST API Reference

### POST `/api/v1/results/{experiment_id}/post-stratification`

Not available yet: answers **501** for every existing experiment, with

```json
{"detail": "Post-stratification is not available yet: it is not computed from an experiment's recorded data."}
```

The request body is still validated, so a malformed request answers 422
first:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `stratum_cols` | `list[str]` | Yes | — | Column names defining strata. Non-empty. |
| `metric_col` | `str` | No | `"metric_value"` | Name of the outcome column. |
| `alpha` | `float` | No | `0.05` | Significance level for CI (exclusive: 0–1). |

**Status codes:**

| Status | Condition |
|---|---|
| 404 | Experiment not found |
| 422 | Missing/invalid `stratum_cols` or invalid `alpha` |
| 501 | Any existing experiment: post-stratification is not available yet |
