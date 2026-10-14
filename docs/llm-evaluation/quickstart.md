# LLM/AI Model Evaluation — Quick Start

This guide creates an experiment that compares two models on a customer support task:
`claude-3-5-sonnet-20241022` is the control and `gpt-4o` the treatment.

Run the commands in one terminal, in order, against the stack from the
[Quick Start](../getting-started/quick-start.md). Each uses the shell variables set by the
ones before it.

**Steps 3 to 5 need a provider key.** They call the models, so the API needs a key for
each provider your variants use. Steps 1, 2 and 6 need no key, and only they are commands
to run here.

---

## 1. Create an Experiment

Log in first. Creating and starting an experiment takes the DEVELOPER or ADMIN role:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`.

Create the experiment, with one variant per model. The collection URL ends with a slash,
`/api/v1/llm-experiments/`; without it the API answers `307`, which `curl` doesn't follow.
This saves the experiment's id in `$EXPERIMENT_ID`:

```{.bash exec}
EXPERIMENT=$(curl -s -X POST localhost:8000/api/v1/llm-experiments/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "name": "Customer Support: Claude vs GPT-4o",
    "description": "Compare Claude Sonnet and GPT-4o on customer support quality",
    "task_type": "chat_completion",
    "evaluation_metric": "business_metric",
    "variants": [
      {
        "name": "control_claude",
        "is_control": true,
        "traffic_split": 0.5,
        "provider": "anthropic",
        "model_name": "claude-3-5-sonnet-20241022",
        "system_prompt": "You are a helpful customer support agent for Acme Corp.",
        "prompt_template": "Customer question: {{customer_message}}\n\nProvide a clear, empathetic response.",
        "temperature": 0.7,
        "max_tokens": 500
      },
      {
        "name": "treatment_gpt4o",
        "is_control": false,
        "traffic_split": 0.5,
        "provider": "openai",
        "model_name": "gpt-4o",
        "system_prompt": "You are a helpful customer support agent for Acme Corp.",
        "prompt_template": "Customer question: {{customer_message}}\n\nProvide a clear, empathetic response.",
        "temperature": 0.7,
        "max_tokens": 500
      }
    ]
  }')
EXPERIMENT_ID=$(jq -r .id <<<"$EXPERIMENT")

jq '{name, status, variants: [.variants[].name]}' <<<"$EXPERIMENT"
```
<!-- expect: "name": "Customer Support: Claude vs GPT-4o" -->
<!-- expect: "status": "DRAFT" -->

The API answers `201 Created`:

```json
{
  "name": "Customer Support: Claude vs GPT-4o",
  "status": "DRAFT",
  "variants": [
    "control_claude",
    "treatment_gpt4o"
  ]
}
```

The response also carries the experiment's `id`, `description`, `task_type`,
`evaluation_metric`, and each variant's full settings and `id`.

---

## 2. Start the Experiment

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/llm-experiments/$EXPERIMENT_ID/start \
  -H "Authorization: Bearer $TOKEN" | jq .status
```
<!-- expect: "ACTIVE" -->

It prints `"ACTIVE"`.

---

## 3. Route User Requests Through the Experiment

For each incoming customer message, call the `/complete` endpoint. The platform assigns the
user to a variant deterministically (the same user always gets the same model), renders the
variant's prompt template with `input_variables`, and calls the model.

Send `POST /api/v1/llm-experiments/{id}/complete` with the user's `user_id` and the
template's `input_variables`. The answer names the variant and the model, and carries the
model's reply (`response`), `latency_ms`, `cost_usd`, the token counts and an
`evaluation_id`, which step 4 uses.

A `google` variant calls the Gemini REST API with the key in
`GEMINI_API_KEY`; `GEMINI_BASE_URL` (default `https://generativelanguage.googleapis.com`)
points it at a proxy or gateway instead. With no `GEMINI_API_KEY`, a Gemini variant's
completion answers `502 {"detail":"LLM provider error: GEMINI_API_KEY is not set"}`.

---

## 4. Record Business Outcomes

When the customer resolves their issue (converts), submit the business metric for that
evaluation with `POST /api/v1/llm-experiments/{id}/evaluate`: the `evaluation_id` and a
`business_metric_value`, such as `1.0`. A `human_rating` from your QA team, from 1 to 5,
goes in the same request.

---

## 5. Run Automated Quality Scoring (Optional)

`POST /api/v1/llm-experiments/{id}/judge` scores the responses on a quality dimension with
LLM-as-judge. It takes a `criteria` (default `helpfulness`) and a
`judge_model` (default `LLM_DEFAULT_JUDGE_MODEL`). The judge is called through the
Anthropic SDK, so `judge_model` names a Claude model. A judge call that fails, or a reply
without a usable score, leaves that response unscored: `score` is `null` in the judge's
response, and the results count it in the variant's `n_auto_eval_unscored`.

---

## 6. Read Results and Declare a Winner

```{.bash exec}
curl -s localhost:8000/api/v1/llm-experiments/$EXPERIMENT_ID/results \
  -H "Authorization: Bearer $TOKEN" \
  | jq '{status, total_evaluations, variants: [.variant_stats[] | {variant_name, n_evaluations}]}'
```
<!-- expect: "status": "ACTIVE" -->
<!-- expect: "total_evaluations": 0 -->

With no completions yet, every count is `0`:

```json
{
  "status": "ACTIVE",
  "total_evaluations": 0,
  "variants": [
    {
      "variant_name": "control_claude",
      "n_evaluations": 0
    },
    {
      "variant_name": "treatment_gpt4o",
      "n_evaluations": 0
    }
  ]
}
```

With traffic, the full response looks like this:

```json
{
  "experiment_id": "550e8400-e29b-41d4-a716-446655440000",
  "experiment_name": "Customer Support: Claude vs GPT-4o",
  "status": "ACTIVE",
  "evaluation_metric": "business_metric",
  "total_evaluations": 1247,
  "winner_variant_name": "treatment_gpt4o",
  "variant_stats": [
    {
      "variant_name": "control_claude",
      "is_control": true,
      "n_evaluations": 624,
      "mean_latency_ms": 1850.3,
      "mean_cost_usd": 0.000923,
      "mean_business_metric": 0.71,
      "business_metric_ci_lower": 0.674,
      "business_metric_ci_upper": 0.746,
      "p_value": null,
      "effect_size": null
    },
    {
      "variant_name": "treatment_gpt4o",
      "is_control": false,
      "n_evaluations": 623,
      "mean_latency_ms": 1120.6,
      "mean_cost_usd": 0.001245,
      "mean_business_metric": 0.79,
      "business_metric_ci_lower": 0.756,
      "business_metric_ci_upper": 0.824,
      "p_value": 0.003,
      "effect_size": 0.42
    }
  ]
}
```

---

## Prompt Template Variables

Use `{{double_braces}}` in your prompt templates:

```text
Template: "Summarize this article for a {{audience}} audience: {{article}}"

Input variables: {
  "audience": "5-year-old",
  "article": "Quantum computing leverages..."
}

Rendered: "Summarize this article for a 5-year-old audience: Quantum computing leverages..."
```

Missing variables will cause a `400 Bad Request` error.

---

## Pausing and Resuming

Pause the experiment:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/llm-experiments/$EXPERIMENT_ID/pause \
  -H "Authorization: Bearer $TOKEN" | jq .status
```
<!-- expect: "PAUSED" -->

It prints `"PAUSED"`. While paused, `/complete` answers `400`. Starting it again resumes it:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/llm-experiments/$EXPERIMENT_ID/start \
  -H "Authorization: Bearer $TOKEN" | jq .status
```
<!-- expect: "ACTIVE" -->

It prints `"ACTIVE"`.

---

## Permissions

| Action | Required Role |
|--------|--------------|
| Create / Start / Pause experiment | DEVELOPER or ADMIN |
| Add / update variants | DEVELOPER or ADMIN |
| Get completion | Any authenticated user (READ) |
| Submit evaluation | DEVELOPER or ADMIN |
| Get results | Any authenticated user (READ) |
| Run LLM-as-judge | DEVELOPER or ADMIN |
