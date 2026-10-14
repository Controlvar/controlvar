# LLM/AI Model Evaluation — Overview

## Supported Providers and Models

| Provider | Models | Notes |
|----------|--------|-------|
| **Anthropic** | claude-opus-5, claude-sonnet-5, claude-haiku-4-5, claude-fable-5-1, and the 4.5/4.6 and Claude 3 generations | Recommended judge model. The priced set is `COST_PER_1K_TOKENS` in `backend/app/core/llm_pricing.py`; a model missing from it records $0.00 and logs a warning. |
| **OpenAI** | gpt-4o, gpt-4o-mini, gpt-3.5-turbo | Priced in the same table |
| **Google** | gemini-1.5-pro, gemini-1.5-flash | Priced in the same table |
| **Local** | A model served by Ollama at `http://localhost:11434` on the API's own host | The address is fixed; token counts are word counts, and cost is $0.00 |

- `ANTHROPIC_API_KEY` enables Anthropic variants in `POST /llm-experiments/{id}/complete`,
  the LLM-as-judge (`POST /llm-experiments/{id}/judge`), and the platform's own AI
  features (`POST /ai/design` and `POST /ai/interpret/{id}`).
- `OPENAI_API_KEY`, which the OpenAI SDK reads, enables OpenAI variants in `/complete`.
- With neither set, no provider is called: `/complete` answers 502 for that variant,
  the judge records its fallback score of 0.5, and the AI features use their templates.

`ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` point the SDKs at a proxy or gateway.

The Google provider needs no extra package: a `google` variant calls the Gemini REST
API with the key in `GEMINI_API_KEY`, and `GEMINI_BASE_URL` (default
`https://generativelanguage.googleapis.com`) points it at a proxy or gateway. With no
`GEMINI_API_KEY`, that variant's `/complete` answers
`502 {"detail":"LLM provider error: GEMINI_API_KEY is not set"}`.

---

## Task types and evaluation metrics

`task_type` is one of `chat_completion`, `text_generation`, `classification`,
`summarization`, `code_generation` or `embedding`.

`evaluation_metric` is one of `business_metric`, `human_rating`, `latency`, `cost`,
`accuracy`, `relevance` or `fluency`. It does not choose the winner: the winner is always
the variant with the highest mean business metric, or, with no business metric, the highest
mean auto-eval score.

---

## Variant Assignment

Variants are assigned on the server using **consistent MD5 hashing**. The same `user_id` always receives the same variant, ensuring reproducibility and preventing confounding from re-assignment.

The hash key is `"{experiment_id}:{user_id}"`, mapped to a bucket in [0, 1) and distributed across variants according to their `traffic_split` values.

---

## LLM-as-Judge Scoring

The platform supports automated quality evaluation using a judge LLM. The default is `settings.LLM_DEFAULT_JUDGE_MODEL` (`claude-sonnet-5`), overridable per request and by the `LLM_DEFAULT_JUDGE_MODEL` environment variable. The judge is prompted:

```text
You are an impartial evaluator.  Rate the following AI response on '{criteria}'
using a score from 0 (terrible) to 1 (excellent).

Original prompt:
{prompt}

Response:
{response}

Reply with ONLY a JSON object: {"score": <float 0-1>, "reasoning": "<one sentence>"}
```

Judge scores are stored as `auto_eval_score` on each evaluation record and included in the results analytics.

---

## Cost Estimation

The platform automatically estimates the cost of each LLM call using a built-in cost table:

```text
claude-opus-5:   $0.005/1k input + $0.025/1k output
claude-sonnet-5: $0.002/1k input + $0.010/1k output
claude-haiku-4-5:$0.001/1k input + $0.005/1k output
gpt-4o:          $0.0025/1k input + $0.010/1k output
gemini-1.5-flash:$0.000075/1k input + $0.0003/1k output
```

Per-variant cost is in `GET /api/v1/llm-experiments/{id}/results` (`mean_cost_usd`); the
dashboard has no LLM screen.

---

## Statistical Analysis

Results include:
- **Mean and 95% confidence interval** for latency, cost, auto-eval score, and business metric;
  the interval uses the exact t critical value at n - 1 degrees of freedom
- **A p-value** (treatment vs control) on the business metric, or on the auto-eval score
  when either variant has no business metric, from Welch's t-test: the exact two-tailed
  value from the t distribution at the Welch-Satterthwaite degrees of freedom, at every
  sample size
- **Cohen's d effect size** for business metric and auto-eval score
- **Winner determination** based on the highest mean business metric (falls back to auto-eval score)
