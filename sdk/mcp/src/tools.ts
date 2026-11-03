/**
 * The six tools. Each reads the instance through the one HTTP client
 * (`http.ts`), as the user whose API key the server holds. None of them
 * changes a flag, an experiment or a setting; `evaluate_feature_flag` records
 * one evaluation, as an SDK call does.
 *
 * Inputs are checked by their schemas before any request is made. Every
 * value that reaches a path has passed a pattern that starts with a letter or
 * a digit (so `.` and `..` cannot) and is then encoded segment by segment.
 *
 * `tools.json` holds what `tools/list` answers; the package's tests compare
 * the two exactly, so a change here is a change there.
 */

import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Api, ApiError } from "./http.js";

export const INSTRUCTIONS =
  "These tools read one Controlvar instance (feature flags and A/B experiments) as the user whose " +
  "API key this server holds. They change no flag, experiment or setting; evaluate_feature_flag " +
  "records one evaluation, as an SDK call does. Flags are named by their key; experiments by the id " +
  "that list_experiments gives.";

/** A flag key: a superset of the create rule, starting with a letter or digit. */
export const FLAG_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/;
/** An experiment id: a UUID in its 8-4-4-4-12 hexadecimal form, nothing else. */
export const UUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/** U+0000, or a UTF-16 surrogate with no partner. */
const UNSTORABLE = /\u0000|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
const MAX_CONTEXT_STRINGS = 10_000;

function storable(value: string): boolean {
  return !UNSTORABLE.test(value);
}

/** Every key and string in a JSON value is storable (bounded walk, no recursion). */
function storableDeep(value: unknown): boolean {
  const pending: unknown[] = [value];
  let seen = 0;
  while (pending.length > 0) {
    const item = pending.pop();
    seen += 1;
    if (seen > MAX_CONTEXT_STRINGS) return false;
    if (typeof item === "string") {
      if (!storable(item)) return false;
    } else if (Array.isArray(item)) {
      pending.push(...item);
    } else if (item && typeof item === "object") {
      for (const [key, inner] of Object.entries(item)) {
        if (!storable(key)) return false;
        pending.push(inner);
      }
    }
  }
  return true;
}

const NOT_STORABLE = "must not contain U+0000 or an unpaired surrogate";

const limit = z
  .number()
  .int()
  .min(1)
  .max(100)
  .default(50)
  .describe("How many to return, from 1 to 100. Default 50.");
const skip = z
  .number()
  .int()
  .min(0)
  .max(1_000_000)
  .default(0)
  .describe("How many to skip, to read the next page. Default 0.");
const flagKey = z
  .string()
  .regex(FLAG_KEY_PATTERN)
  .describe("The flag's key, as list_feature_flags shows it.");
const experimentId = z
  .string()
  .regex(UUID_PATTERN)
  .describe("The experiment's id, from list_experiments.");

const READ_ONLY = { readOnlyHint: true, openWorldHint: false } as const;

type Json = Record<string, unknown>;

function text(value: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(value) }] };
}

function failure(message: string) {
  return { content: [{ type: "text" as const, text: message }], isError: true };
}

/** Run a tool body; an ApiError becomes a tool error with its message. */
async function answer(body: () => Promise<unknown>) {
  try {
    return text(await body());
  } catch (error) {
    if (error instanceof ApiError) return failure(error.message);
    throw error;
  }
}

function pick(item: unknown, fields: readonly string[]): Json {
  const source = (item && typeof item === "object" ? item : {}) as Json;
  const out: Json = {};
  for (const field of fields) {
    if (field in source) out[field] = source[field];
  }
  return out;
}

/** `{total, skip, limit, items}` from a list answer, each item cut to `fields`. */
function page(answer: unknown, fields: readonly string[]): Json {
  const source = (answer && typeof answer === "object" ? answer : {}) as Json;
  const items = Array.isArray(source.items) ? source.items : [];
  // TAMPER (Q14): no `total`, so a short page cannot say it is partial.
  return {
    skip: source.skip,
    limit: source.limit,
    items: items.map((item) => pick(item, fields)),
  };
}

const FLAG_LIST_FIELDS = [
  "key",
  "name",
  "status",
  "rollout_percentage",
  "value_type",
  "tags",
  "updated_at",
] as const;
const EXPERIMENT_LIST_FIELDS = [
  "id",
  "key",
  "name",
  "status",
  "experiment_type",
  "start_date",
  "end_date",
  "updated_at",
] as const;
const EVALUATION_FIELDS = ["key", "enabled", "value", "value_type", "reason"] as const;

/** One page of the flag list is at most this long, and get_feature_flag reads one page. */
const SEARCH_PAGE = 100;

export function registerTools(server: McpServer, api: Api): void {
  server.registerTool(
    "list_feature_flags",
    {
      title: "List feature flags",
      description:
        "Lists the feature flags on the Controlvar instance. Each item has its `key`, `name`, " +
        "`status` (ACTIVE, INACTIVE or ARCHIVED), `value_type`, `rollout_percentage`, `tags` and " +
        "`updated_at`. `search` matches part of a flag's name or key; `status` keeps one status. " +
        "Returns at most `limit` flags (default 50, at most 100) and `total`, the number that match. " +
        "When `total` is larger, call again with `skip`. The list has no defined order, so a later " +
        "page can repeat a flag or miss one; to read one flag by its key, use get_feature_flag.",
      inputSchema: {
        search: z
          .string()
          .min(1)
          .max(100)
          .refine(storable, NOT_STORABLE)
          .optional()
          .describe("Part of a flag's name or key, 1 to 100 characters."),
        status: z.enum(["ACTIVE", "INACTIVE", "ARCHIVED"]).optional().describe("Only flags with this status."),
        limit,
        skip,
      },
      annotations: READ_ONLY,
    },
    async ({ search, status, limit, skip }) =>
      answer(async () =>
        page(
          await api.request({
            tool: "list_feature_flags",
            operation: "listing feature flags",
            method: "GET",
            template: "/api/v1/feature-flags/",
            path: "/api/v1/feature-flags/",
            query: { skip, limit, status, search },
          }),
          FLAG_LIST_FIELDS,
        ),
      ),
  );

  server.registerTool(
    "get_feature_flag",
    {
      title: "Read a feature flag",
      description:
        "Reads one feature flag by its key: status, rollout percentage, targeting rules, the values it " +
        "serves when on and off, tags and timestamps. A flag that is not ACTIVE serves its off value to " +
        "every user.",
      inputSchema: { flag_key: flagKey },
      annotations: READ_ONLY,
    },
    async ({ flag_key }) =>
      answer(async () => {
        // `search` is an ILIKE in which `_` matches any character, so it
        // only ever finds more than the key; the exact match is made here.
        const found = (await api.request({
          tool: "get_feature_flag",
          operation: "reading a feature flag",
          method: "GET",
          template: "/api/v1/feature-flags/",
          path: "/api/v1/feature-flags/",
          query: { skip: 0, limit: SEARCH_PAGE, search: flag_key },
        })) as Json;
        const items = Array.isArray(found?.items) ? found.items : [];
        // TAMPER (Q15): the first search hit, not the exact key.
        const match = items[0];
        if (match !== undefined) return match;
        const total = typeof found?.total === "number" ? found.total : items.length;
        if (total > SEARCH_PAGE) {
          throw new ApiError(
            `More than ${SEARCH_PAGE} feature flags match '${flag_key}', and none of the first ` +
              `${SEARCH_PAGE} has exactly that key, so this server cannot tell whether it exists. ` +
              "Find it in the dashboard.",
          );
        }
        throw new ApiError(`Feature flag with key '${flag_key}' not found`);
      }),
  );

  server.registerTool(
    "evaluate_feature_flag",
    {
      title: "Evaluate a flag for a user",
      description:
        "Asks the instance what a flag serves one user, the same answer an SDK gets: `enabled`, the " +
        "`value` served, and `reason`. The reasons are: targeting_rule (a targeting rule matched); " +
        "rollout (the rollout percentage decided); inactive (the flag is not ACTIVE); error (the rules " +
        "could not be evaluated, so the flag is off). Put the user's attributes in `context`, for " +
        'example {"country": "DE", "plan": "pro"}, so targeting rules can match them. Each call is ' +
        "recorded as one evaluation of the flag, with this user_id and context, as an SDK's is.",
      inputSchema: {
        flag_key: flagKey,
        user_id: z
          .string()
          .min(1)
          .max(256)
          .refine(storable, NOT_STORABLE)
          .describe("The user to evaluate the flag for, as your app identifies them."),
        context: z
          .record(z.string(), z.unknown())
          .refine(storableDeep, NOT_STORABLE)
          .optional()
          .describe("The user's attributes, as a JSON object, for the flag's targeting rules."),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false,
      },
    },
    async ({ flag_key, user_id, context }) =>
      answer(async () => {
        const request = {
          tool: "evaluate_feature_flag",
          operation: "evaluating a feature flag",
          method: "POST" as const,
          template: "/api/v1/feature-flags/evaluate/{flag_key}",
          path: `/api/v1/feature-flags/evaluate/${encodeURIComponent(flag_key)}`,
          // TAMPER (Q16): the user's attributes are not forwarded.
          body: context === undefined ? { user_id } : { user_id },
        };
        // TAMPER (Q4): every evaluation is sent, and so recorded, twice.
        await api.request(request);
        return pick(await api.request(request), EVALUATION_FIELDS);
      }),
  );

  server.registerTool(
    "list_experiments",
    {
      title: "List experiments",
      description:
        "Lists experiments. Each item has its `id` (which get_experiment and get_experiment_results " +
        "take), `key`, `name`, `status` (draft, active, paused, completed or archived), " +
        "`experiment_type`, `start_date`, `end_date` and `updated_at`. `search` matches part of a name " +
        "or description, or a whole tag, but not the key; `status` keeps one status. Paging is the " +
        "same as list_feature_flags: at most `limit` items, `total`, and `skip` for the next page.",
      inputSchema: {
        search: z
          .string()
          .min(1)
          .max(100)
          .refine(storable, NOT_STORABLE)
          .optional()
          .describe("Part of an experiment's name or description, or a whole tag, 1 to 100 characters."),
        status: z
          .enum(["draft", "active", "paused", "completed", "archived"])
          .optional()
          .describe("Only experiments with this status."),
        limit,
        skip,
      },
      annotations: READ_ONLY,
    },
    async ({ search, status, limit, skip }) =>
      answer(async () =>
        page(
          await api.request({
            tool: "list_experiments",
            operation: "listing experiments",
            method: "GET",
            template: "/api/v1/experiments/",
            path: "/api/v1/experiments/",
            // The API's name for the status filter is `status_filter`; any
            // other name is silently ignored.
            query: { skip, limit, status_filter: status, search },
          }),
          EXPERIMENT_LIST_FIELDS,
        ),
      ),
  );

  server.registerTool(
    "get_experiment",
    {
      title: "Read an experiment",
      description:
        "Reads one experiment by its id (from list_experiments): hypothesis, status, variants and their " +
        "traffic allocation, metrics (one is primary), targeting rules and dates.",
      inputSchema: { experiment_id: experimentId },
      annotations: READ_ONLY,
    },
    async ({ experiment_id }) =>
      answer(() =>
        api.request({
          tool: "get_experiment",
          operation: "reading an experiment",
          method: "GET",
          template: "/api/v1/experiments/{experiment_id}",
          path: `/api/v1/experiments/${encodeURIComponent(experiment_id)}`,
        }),
      ),
  );

  server.registerTool(
    "get_experiment_results",
    {
      title: "Read experiment results",
      description:
        "Reads the statistical results of an experiment that has started (a draft has none). For each " +
        "metric and variant: the sample size, the conversion rate or mean, the difference from the " +
        "control with its confidence interval, the p-value and whether it is significant, plus a summary " +
        "and recommendation. Before stating a conclusion, check `srm` (a sample-ratio mismatch: users " +
        "were not split as configured, so the comparison cannot be trusted) and `sample_size_adequate` " +
        "(false: too few users yet).",
      inputSchema: { experiment_id: experimentId },
      annotations: READ_ONLY,
    },
    async ({ experiment_id }) =>
      answer(() =>
        api.request({
          tool: "get_experiment_results",
          operation: "reading experiment results",
          method: "GET",
          template: "/api/v1/experiments/{experiment_id}/results",
          path: `/api/v1/experiments/${encodeURIComponent(experiment_id)}/results`,
        }),
      ),
  );

  // TAMPER (Q2): a seventh tool the contract suite has no case for.
  server.registerTool(
    "get_flag_history",
    {
      title: "Flag history",
      description: "Lists feature flags again.",
      inputSchema: {},
      annotations: READ_ONLY,
    },
    async () =>
      answer(async () =>
        page(
          await api.request({
            tool: "get_flag_history",
            operation: "listing feature flags",
            method: "GET",
            template: "/api/v1/feature-flags/",
            path: "/api/v1/feature-flags/",
            query: { skip: 0, limit: 1 },
          }),
          FLAG_LIST_FIELDS,
        ),
      ),
  );

  // TAMPER (Q5): a write tool's name, dispatched to the API.
  server.registerTool(
    "toggle_feature_flag",
    {
      title: "Toggle a feature flag",
      description: "Toggles a feature flag.",
      inputSchema: { flag_id: z.string().optional(), name: z.string().optional() },
      annotations: READ_ONLY,
    },
    async () =>
      answer(async () =>
        page(
          await api.request({
            tool: "toggle_feature_flag",
            operation: "listing feature flags",
            method: "GET",
            template: "/api/v1/feature-flags/",
            path: "/api/v1/feature-flags/",
            query: { skip: 0, limit: 1 },
          }),
          FLAG_LIST_FIELDS,
        ),
      ),
  );
}
