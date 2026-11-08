# The MCP Server and the AI Endpoints

The Controlvar MCP server lets an AI assistant such as Claude Code, Claude
Desktop or Cursor read your instance: list and read feature flags, evaluate a
flag for a user, list experiments and read their results. It runs on your
computer, started by the assistant, and calls your instance's API with your own
API key, so it sees what your role can see. It changes no flag or experiment;
evaluating a flag is recorded as one evaluation, as an SDK's is.

Nothing on your instance answers an MCP client; add the server by command, not
by URL.

The platform also serves REST endpoints under `/api/v1/ai/` that suggest an
experiment design, interpret a result, estimate a sample size and list
experiment templates. They come after the MCP server on this page.

The examples run against the [Quick Start](getting-started/quick-start.md)
stack, in one terminal, in order: later steps use the `$TOKEN` and `$KEY` an
earlier one sets.

---

## Before you start

You need Node.js 22 or later and the address of your instance's API:
`http://localhost:8000` on the Quick Start stack and `http://localhost:8080` on
the compose stack. Give the address only, with no path.

The server refuses a plain `http://` address for any host other than
`localhost`, `127.0.0.1` or `[::1]`, because your API key would cross the
network unencrypted. If your instance runs on a private network you trust and
has no HTTPS, add `CONTROLVAR_ALLOW_INSECURE_HTTP=1` to the server's `env`; the
server then prints a warning each time it starts.

## Create an API key

The server needs an API key that carries the `mcp:read` scope. It acts as the
key's owner, so it reads what that user can read.

Who can create one:

- An administrator can create one in the dashboard, under Admin and then API
  Keys, by ticking the `mcp:read` option.
- If your instance signs people in with a local password, as the Quick Start
  does, anyone who can sign in can create their own with the request below.

The request signs in as the Quick Start's administrator and creates the key,
which is shown once. The last request reads the key back from your list of keys
and prints its scopes:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

CREATED=$(curl -s -X POST localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"mcp","scopes":["mcp:read"]}')
KEY=$(echo "$CREATED" | jq -r .key)
ID=$(echo "$CREATED" | jq -r .id)

curl -s localhost:8000/api/v1/api-keys -H "Authorization: Bearer $TOKEN" \
  | jq -c --arg id "$ID" '.[] | select(.id == $id) | .scopes'
```
<!-- expect: ["mcp:read"] -->

## Add the server to your assistant

Each assistant starts the server itself, with two settings: `CONTROLVAR_API_URL`
is your instance's address and `CONTROLVAR_API_KEY` is the key.

### Claude Code

Run this in the project directory where you want the server, with the key in
`$KEY`:

```{.bash skip reason="toolchain: needs the Claude Code CLI and the key from the step above"}
claude mcp add controlvar \
  -e CONTROLVAR_API_URL=http://localhost:8000 \
  -e CONTROLVAR_API_KEY="$KEY" \
  -- npx -y @controlvar/mcp-server@0.1.0
```

The shell expands `$KEY`, so your shell history keeps `$KEY`, but Claude Code
stores the key itself in plain text.

### Claude Desktop

Add the server to Claude Desktop's configuration file, which is
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and
`%APPDATA%\Claude\claude_desktop_config.json` on Windows. Then quit Claude
Desktop fully and open it again.

```json
{
  "mcpServers": {
    "controlvar": {
      "command": "npx",
      "args": ["-y", "@controlvar/mcp-server@0.1.0"],
      "env": {
        "CONTROLVAR_API_URL": "http://localhost:8000",
        "CONTROLVAR_API_KEY": "paste the key here"
      }
    }
  }
}
```

### Cursor

Add the same block to `~/.cursor/mcp.json`.

## Try it

Ask your assistant a question that needs your instance, for example:

- Is the flag `beta_features` on for the user `user-123`, on the pro plan?
- How is the experiment `checkout_button_color` doing?

The first makes the assistant call `evaluate_feature_flag`. The second makes it
call `list_experiments` and then `get_experiment_results`, and a careful answer
checks `srm` and `sample_size_adequate`. A command such as `claude mcp list`
only shows that the process started, so a question is the check.

## The tools

The server offers six tools:

| Tool | What it answers | API route |
|---|---|---|
| `list_feature_flags` | The feature flags, a page at a time | `GET /api/v1/feature-flags/` |
| `get_feature_flag` | One feature flag, by its key | `GET /api/v1/feature-flags/` |
| `evaluate_feature_flag` | What a flag serves one user | `POST /api/v1/feature-flags/evaluate/{flag_key}` |
| `list_experiments` | The experiments, a page at a time | `GET /api/v1/experiments/` |
| `get_experiment` | One experiment, by its id | `GET /api/v1/experiments/{experiment_id}` |
| `get_experiment_results` | An experiment's statistical results | `GET /api/v1/experiments/{experiment_id}/results` |

None of them changes a flag, an experiment or a setting. A list tool returns at
most 100 items and the total, and the assistant asks for the next page with
`skip`.

## What leaves your instance

Whatever a tool returns is given to the AI model behind your assistant, and so
goes to that model's provider. That includes targeting rules, which can hold
user ids or email addresses, and experiment results. Text that people on your
instance wrote, such as the names, descriptions and hypotheses of flags and
experiments, is given to the model as it is. Check this against your own data
rules before you connect an assistant to an instance that holds personal data.

## Where the key is kept

Your assistant keeps the key in plain text in its own settings. The server
writes it nowhere else: it is in no tool result, error message or log line.
Do not use `--scope project` with Claude Code, and do not put the key in a
`.cursor/mcp.json` inside a repository, because both write the key into a file
in your repository, where it can be committed. Do not put this key in a web or
mobile app.

## Errors

A tool that fails answers with one message that says what failed and what to
do. A setting that can never work stops the server at start-up instead, with one
line on stderr.

| Message | Cause | Fix |
|---|---|---|
| `CONTROLVAR_API_URL is "localhost:8000", which is not an http:// or https:// address.` | The address has no scheme. | Set it to your instance's address, for example `http://localhost:8000`. |
| `CONTROLVAR_API_URL is "http://controlvar.internal:8080", a plain http:// address on another computer, so your API key would cross the network unencrypted.` | A plain `http://` address for a host that is not this computer. | Use the `https://` address, or set `CONTROLVAR_ALLOW_INSECURE_HTTP=1` as described above. |
| `CONTROLVAR_ALLOW_INSECURE_HTTP has a value this server does not accept.` | The setting has any value other than `1`. | Remove it, or set it to `1`. |
| `CONTROLVAR_API_KEY is not set.` | The assistant's settings have no key. | Set `CONTROLVAR_API_KEY` to the key you created. |
| `CONTROLVAR_API_KEY looks like a sign-in token, not an API key.` | A sign-in token was pasted where the key goes. | Create an API key and use that. |
| `Could not reach the Controlvar API at http://localhost:8000: connection refused.` | The instance is not running at that address. | Start the instance, or correct `CONTROLVAR_API_URL`. |
| `http://localhost:3100 answered, but not as the Controlvar API (it sent a web page).` | The address is the dashboard's. | Use the address that serves `/api/v1`: `http://localhost:8000` on the Quick Start stack. |
| `The Controlvar API at http://127.0.0.1:8080 refused that host name (400: Invalid host header).` | The instance does not accept that host name. | Use the host name your instance is set up for. |
| `The Controlvar API at http://localhost:8000 did not accept the key in CONTROLVAR_API_KEY (401: Invalid API Key).` | The key was deleted or has expired, or its owner's account is deactivated, or your instance is too old to accept an API key for these routes. | Create a new key, or upgrade the instance. |
| `Listing feature flags failed: This API key does not have the 'mcp:read' scope. Create a key with the 'mcp:read' scope to read flags, experiments and results with it. (403).` | The key was created without the `mcp:read` scope. | Create a key with the `mcp:read` scope. |
| `Listing feature flags failed: You don't have permission to list feature flags (403).` | The key's owner may not read this. | Use a key owned by a user who can. |
| `The Controlvar API is limiting requests from this address (429). Try again in 30 seconds.` | Too many requests. | Wait as long as the message says. |
| `The Controlvar API at http://localhost:8000 failed while listing feature flags (500). Request ID: req-from-the-api-42; your administrator can find it in the API's logs.` | The instance failed. | Give your administrator the request id in the message. |

## Revoking a key

To stop the server working, delete its key: in the dashboard under Admin and
then API Keys, or with `DELETE /api/v1/api-keys/{id}`. When a key is deleted or
expires, or its owner's account is deactivated, the next tool call fails with
the 401 message above. Removing a user from the identity provider does not
revoke their API keys: deactivate the Controlvar user or delete the keys.

To use a new key, create it, run `claude mcp remove controlvar`, add the server
again and start a new session.

## Versions

The server reports the version in `sdk/mcp/package.json` to your assistant when
it starts.

---

## The manifest

The manifest at `GET /api/v1/mcp/manifest` lists the server's tools, so you can
read what the server offers without installing it. It needs no sign-in:

```{.bash exec}
curl -s localhost:8000/api/v1/mcp/manifest | jq -r '.tools[].name'
```
<!-- expect: list_feature_flags -->
<!-- expect: get_feature_flag -->
<!-- expect: evaluate_feature_flag -->
<!-- expect: list_experiments -->
<!-- expect: get_experiment -->
<!-- expect: get_experiment_results -->

Each entry has a `name`, a `description` and its `parameters`:

```{.bash exec}
curl -s localhost:8000/api/v1/mcp/manifest \
  | jq '.tools[] | select(.name == "get_experiment")'
```
<!-- expect: "name": "get_experiment" -->
<!-- expect: "description": "Reads one experiment by its id (from list_experiments) -->
<!-- expect: "experiment_id" -->

```json
{
  "name": "get_experiment",
  "description": "Reads one experiment by its id (from list_experiments): hypothesis, status, variants and their traffic allocation, metrics (one is primary), targeting rules and dates.",
  "parameters": {
    "experiment_id": {
      "type": "string",
      "description": "The experiment's id, from list_experiments."
    }
  }
}
```

The manifest's `version` is the manifest's own, not the server's.

---

## The AI endpoints

The endpoints under `/api/v1/ai/` suggest an experiment design, interpret a
result, estimate a sample size and list experiment templates. Call them with any
HTTP client.

## Signing in

The endpoints under `/api/v1/ai/` need a signed-in user. Without a token they
answer `401`, as this one does:

```{.bash exec}
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/v1/ai/templates
```
<!-- expect: 401 -->

Sign in as the Quick Start's administrator. The second command prints
`"ADMIN"`; if it prints `null`, the sign-in failed and `$TOKEN` holds no token:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

---

## Experiment design: `POST /api/v1/ai/design`

Send a description of what you want to test and an experiment type
(`checkout`, `onboarding`, `pricing`, `email` or `landing_page`):

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/ai/design \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{
    "description": "We want to test whether simplifying the onboarding checklist increases activation",
    "experiment_type": "onboarding"
  }' | jq .
```
<!-- expect: "primary_metric": "activation_rate" -->
<!-- expect: "recommended_sample_size": 1000 -->
<!-- expect: "confidence": "template_based" -->

On the Quick Start stack the suggestion is the template for the experiment
type, and `confidence` says so (`"template_based"`):

```json
{
  "hypothesis": "Changing the onboarding flow will improve user outcomes",
  "primary_metric": "activation_rate",
  "guardrail_metrics": ["day7_retention", "time_to_first_action"],
  "recommended_sample_size": 1000,
  "recommended_duration_days": 14,
  "variant_descriptions": ["Control: current experience", "Variant A: proposed change"],
  "confidence": "template_based",
  "reasoning": "Template-based suggestion (AI not available)"
}
```

---

## Results interpretation: `POST /api/v1/ai/interpret/{experiment_id}`

Send one variant's result. The endpoint interprets the numbers in the request
body: it does not read the experiment's stored results or look the id up, so
the `checkout-v2` below, which does not exist on the stack, is answered like
any other:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/ai/interpret/checkout-v2 \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"experiment_id": "checkout-v2", "variant_name": "simplified", "p_value": 0.03, "relative_improvement_pct": 3.2}' \
  | jq .
```
<!-- expect: "summary": "simplified showed a 3.2% improvement (p=0.030). Recommend shipping." -->
<!-- expect: "recommendation": "ship" -->
<!-- expect: "confidence_statement": "Statistical confidence: 97.0%" -->
<!-- expect: "generated_by": "template" -->

```json
{
  "summary": "simplified showed a 3.2% improvement (p=0.030). Recommend shipping.",
  "recommendation": "ship",
  "confidence_statement": "Statistical confidence: 97.0%",
  "key_findings": ["simplified showed a 3.2% improvement (p=0.030). Recommend shipping."],
  "generated_by": "template"
}
```

On the Quick Start stack the interpretation comes from a template
(`"generated_by": "template"`), and `recommendation` is `ship` when `p_value`
is below 0.05 and the improvement is positive, `stop_futility` when `p_value`
is below 0.05 and the improvement is negative, and `continue_testing`
otherwise. These two print `stop_futility`, then `continue_testing`:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/ai/interpret/checkout-v2 \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"experiment_id": "checkout-v2", "variant_name": "simplified", "p_value": 0.03, "relative_improvement_pct": -2.5}' \
  | jq -r .recommendation

curl -s -X POST localhost:8000/api/v1/ai/interpret/checkout-v2 \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"experiment_id": "checkout-v2", "variant_name": "simplified", "p_value": 0.4, "relative_improvement_pct": 1.1}' \
  | jq -r .recommendation
```
<!-- expect: stop_futility -->
<!-- expect: continue_testing -->

All four body fields are required. An empty body answers `422`, naming each
missing field:

```{.bash exec}
curl -s -X POST localhost:8000/api/v1/ai/interpret/checkout-v2 \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{}' | jq -r '.detail[].loc[1]'
```
<!-- expect: experiment_id -->
<!-- expect: variant_name -->
<!-- expect: p_value -->
<!-- expect: relative_improvement_pct -->

---

## Sample size: `GET /api/v1/ai/sample-size`

Give a baseline rate (`baseline_rate`) and a minimum detectable effect
(`mde`); a confidence level (`confidence`) and power (`power`) are optional,
0.95 and 0.80 by default. `mde` is an absolute change in the rate: 0.004 on a
baseline of 0.08 plans for 8% against 8.4%, a 5% relative lift.

The estimate is for a control and one treatment. Add `daily_traffic`, the
users a day who enter the experiment (1 to 10^12), and `days_to_significance`
is the whole days until each variant has `required_per_variant` users. The
two variants split the day's users, so at 10,000 a day each gets 5,000, and
73,855 per variant takes 14.8 days: 15. Without `daily_traffic` it is `null`.

```{.bash exec}
curl -s "localhost:8000/api/v1/ai/sample-size?baseline_rate=0.08&mde=0.004&power=0.80&daily_traffic=10000" \
  -H "Authorization: Bearer $TOKEN" | jq .
```
<!-- expect: "required_per_variant": 73855 -->
<!-- expect: "total_required": 147710 -->
<!-- expect: "days_to_significance": 15 -->

```json
{
  "required_per_variant": 73855,
  "total_required": 147710,
  "days_to_significance": 15,
  "assumptions": {"baseline_rate": 0.08, "mde": 0.004, "confidence": 0.95, "power": 0.8}
}
```

---

## Experiment templates: `GET /api/v1/ai/templates`

Five templates, one for each experiment type. This prints each one's id and
type:

```{.bash exec}
curl -s localhost:8000/api/v1/ai/templates \
  -H "Authorization: Bearer $TOKEN" | jq -r '.[] | "\(.id) \(.experiment_type)"'
```
<!-- expect: checkout-cta checkout -->
<!-- expect: onboarding-flow onboarding -->
<!-- expect: pricing-display pricing -->
<!-- expect: email-subject email -->
<!-- expect: landing-page-hero landing_page -->

`type` keeps the templates of one experiment type:

```{.bash exec}
curl -s "localhost:8000/api/v1/ai/templates?type=pricing" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.[].id'
```
<!-- expect: pricing-display -->

One template, by its id:

```{.bash exec}
curl -s localhost:8000/api/v1/ai/templates/checkout-cta \
  -H "Authorization: Bearer $TOKEN" | jq '{name, primary_metric, guardrail_metrics}'
```
<!-- expect: "name": "Checkout CTA Button Test" -->
<!-- expect: "primary_metric": "conversion_rate" -->
