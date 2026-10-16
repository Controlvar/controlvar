Run the full autonomous multi-agent QA pipeline for the Controlvar platform.

Usage: /autotest [scenario_name or "all"]

If no scenario is provided, defaults to "ab_test_lifecycle".

## What This Does

This skill orchestrates 8 specialized agents in a phased pipeline — with parallel
execution where possible, conditional logic, and a consolidated quality report.

```
Pre-flight checks
  ↓
Phase 1 (parallel): data-generator + tester (unit tests)
  ↓
Phase 2 (sequential): scenario-runner (E2E browser tests, depends on seeded data)
  ↓
Phase 3 (parallel): validator (statistics) + reviewer (code review) + contract-tester + a11y-auditor
  ↓
Phase 4 (conditional): auto-fixer (only if failures detected)
  ↓
Final Report
```

## Available Agents

| Agent | Role |
|-------|------|
| `data-generator` | Seed statistically realistic experiment data |
| `tester` | Run unit/integration tests, check coverage |
| `scenario-runner` | E2E browser tests via Playwright + visual regression |
| `validator` | Validate statistical correctness of results |
| `reviewer` | Code review for correctness, security, patterns |
| `contract-tester` | Cross-SDK golden vector tests |
| `a11y-auditor` | WCAG 2.1 AA accessibility scans |
| `auto-fixer` | Diagnose and fix failures |

## Execution Plan

### Pre-flight Checks

Before launching any agents, verify the environment:
```bash
# Backend health
curl -sf http://localhost:8000/health || echo "BACKEND DOWN"
# Frontend running
curl -sf http://localhost:3100 -o /dev/null -w "%{http_code}" || echo "FRONTEND DOWN"
# Database connected (health endpoint returns DB status)
curl -sf http://localhost:8000/health | python -m json.tool 2>/dev/null || true
```

If backend is down, STOP and report — most agents need it.
If frontend is down, mark E2E/visual/a11y phases as SKIPPED but continue with backend-only tests.

### Phase 1: Data Generation + Unit Tests (PARALLEL)

Launch these two agents simultaneously:

**Agent 1: data-generator**
- Run scenario: "$ARGUMENTS" (default: "ab_test_lifecycle")
- First do a dry run and print the summary
- If the platform is running, seed the data
- Return: experiment_id, user count, event count, expected significance, z-score

**Agent 2: tester**
- Run unit tests for recently changed files
- Command: `source venv/bin/activate && python -m pytest backend/tests/unit/ -p no:cov -q --tb=short`
- Check test pass rate
- Return: passed count, failed count, coverage gaps

### Phase 2: E2E Browser Tests (SEQUENTIAL — needs Phase 1 data)

**Agent: scenario-runner**
- Check if frontend is running at http://localhost:3100
- If running: execute the full scenario UI walkthrough
  - "ab_test_lifecycle" → Scenario 1: A/B Experiment Lifecycle
  - "feature_flag_rollout" → Scenario 2: Feature Flag Gradual Rollout
  - Default → Scenarios 1 + 3 (lifecycle + RBAC check)
- Run Playwright E2E tests: `cd frontend && npx playwright test --reporter=list`
- Also run Scenario 3 (RBAC) regardless of which scenario was requested
- Capture screenshots for visual regression comparison
- Check for any 500 errors in network requests during scenario execution
- If frontend not running: skip and note in report
- Return: steps passed/failed, E2E test results, visual diff count, network errors

### Phase 3: Validation + Review + Contracts + A11y (PARALLEL)

Launch these four agents simultaneously:

**Agent 1: validator**
- Use experiment_id from Phase 1 (if available)
- Validate: basic significance, CUPED, sequential testing status
- If no experiment_id: validate data generator's statistical properties
- Return: validation results per check (pass/fail with values)

**Agent 2: reviewer**
- Run `git diff HEAD~1..HEAD --name-only` to find changed files
- Review changed files for correctness, security, pattern adherence
- Pay attention to: import paths, async/sync patterns, RBAC checks
- Return: APPROVED / APPROVED WITH SUGGESTIONS / CHANGES REQUESTED

**Agent 3: contract-tester**
- Run the SDK contract tests: `source venv/bin/activate && make test-sdk` (each SDK's own hash function against the golden vectors)
- Verify cross-SDK hash parity
- Return: per-SDK pass/fail, any divergences

**Agent 4: a11y-auditor**
- Run accessibility tests: `cd frontend && npx playwright test accessibility --reporter=list`
- If @axe-core/playwright not installed, install it first
- Categorize violations by severity
- Return: violation counts by severity, affected pages

### Phase 4: Auto-Fix (CONDITIONAL — only if failures exist)

If Phases 1-3 produced any failures, launch the auto-fixer agent:

**Agent: auto-fixer**
- Collect all failures from all phases
- For each failure: diagnose root cause, write minimal fix, verify it passes
- If auto-fix not possible, produce diagnosis report
- Return: fixed count, blocked count (needing human review)

If all phases passed, skip Phase 4.

## Final Report

After all agents complete, produce a consolidated report:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  AUTOTEST REPORT — Controlvar Platform (Multi-Agent QA)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Scenario:      <name>
Run at:        <timestamp>

Phase 1A — Data Generation
  Status:      PASS / FAIL
  Users:       <n>
  Events:      <n>
  Z-score:     <value> (<significant/not significant>)

Phase 1B — Unit Tests
  Status:      PASS / FAIL
  Tests:       <n> passed, <n> failed

Phase 2 — E2E Browser Tests
  Status:      PASS / FAIL / SKIPPED (frontend not running)
  Playwright:  <n>/<total> specs passed
  UI Steps:    <n>/<total> passed
  Visual Diff: <n> screenshots with changes
  Network:     <n> errors detected

Phase 3A — Statistical Validation
  Status:      PASS / FAIL / SKIPPED
  Checks:      <n>/<total> passed

Phase 3B — Code Review
  Status:      APPROVED / CHANGES REQUESTED / NO CHANGES

Phase 3C — SDK Contract Tests
  Status:      PASS / FAIL
  Python:      <n>/<n> vectors passed
  JavaScript:  <n>/<n> vectors passed

Phase 3D — Accessibility Audit
  Status:      PASS / CONDITIONAL PASS / FAIL
  Critical:    <n> violations
  Serious:     <n> violations
  Pages:       <n>/<total> clean

Phase 4 — Auto-Fix
  Status:      N/A / FIXED <n> issues / BLOCKED
  Fixed:       <list or "none">
  Needs human: <list or "none">

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OVERALL: ✓ PASS  |  ✗ FAIL  |  ⚠ PASS WITH WARNINGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Quick Run

/autotest                          — defaults to ab_test_lifecycle
/autotest ab_test_lifecycle        — full A/B test lifecycle
/autotest feature_flag_rollout     — feature flag gradual rollout
/autotest statistical_edge_cases   — edge cases (zero events, Simpson's paradox)
/autotest concurrent_experiments   — mutual exclusion + interactions
/autotest all                      — all scenarios
