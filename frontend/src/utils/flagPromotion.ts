/**
 * Promoting a flag from one environment to the next.
 *
 * Development is promoted to Staging, and Staging to Production; Production
 * is promoted nowhere. The API copies four things and nothing else: the
 * status (on or off), the rollout percentage, the targeting rules and the
 * split (#2012). A boolean flag has no split, so its dialog lists the first
 * three. This file works out what the dialog shows before anything is sent:
 * each of them, as it is in the target now and as it will be after, and
 * whether it changes.
 */
import type { FlagEnvironmentSettings } from '@/services/featureFlags';
import { isNoRules } from '@/utils/experimentTargeting';
import { FLAG_ENVIRONMENT_LABELS, FlagEnvironment } from '@/utils/flagEnvironments';
import { describeSplit, sameSplit } from '@/utils/flagSplit';

/** The environment a flag is promoted into from `environment`, or null (Production). */
export function promotionTarget(environment: FlagEnvironment | null): FlagEnvironment | null {
  if (environment === 'development') return 'staging';
  if (environment === 'staging') return 'production';
  return null;
}

/** The button's words: "Promote to Staging". */
export function promoteLabel(target: FlagEnvironment): string {
  return `Promote to ${FLAG_ENVIRONMENT_LABELS[target]}`;
}

export type PromotionField = 'status' | 'rollout' | 'rules' | 'split';

export interface PromotionRow {
  field: PromotionField;
  label: string;
  /** What the target has now, one line per entry. */
  now: string[];
  /** What it has after the promotion. */
  after: string[];
  /** The two differ. */
  changes: boolean;
}

/** The copied fields, in the order the dialog lists them; a boolean flag's has no split. */
export const PROMOTED_FIELDS: Record<PromotionField, string> = {
  status: 'Status',
  rollout: 'Rollout percentage',
  rules: 'Targeting rules',
  split: 'Split',
};

function isOn(settings: FlagEnvironmentSettings): boolean {
  return typeof settings.status === 'string' && settings.status.toLowerCase() === 'active';
}

/** "On", or "Off" for an off flag, an archived one and one nobody configured. */
export function statusWord(settings: FlagEnvironmentSettings): string {
  return isOn(settings) ? 'On' : 'Off';
}

function percentWord(settings: FlagEnvironmentSettings): string {
  return `${settings.rollout_percentage ?? 0}%`;
}

function valueWord(value: unknown): string {
  if (Array.isArray(value)) return value.map(valueWord).join(', ');
  if (typeof value === 'string') return value;
  return JSON.stringify(value) ?? '';
}

/** The same JSON whatever order the keys came in, so reordered rules are not a change. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    const entries = Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`);
    return `{${entries.join(',')}}`;
  }
  return JSON.stringify(value ?? null);
}

/**
 * Targeting rules as lines of text, one per group: the condition's attribute,
 * operator and value, so two rule sets that differ in a value do not read as
 * the same. "None" for no rules; a shape the dashboard does not know is shown
 * as it is stored, cut at 200 characters.
 */
export function describeRules(rules: unknown): string[] {
  if (isNoRules(rules)) return ['None'];
  const groups = typeof rules === 'object' ? (rules as { groups?: unknown }).groups : undefined;
  if (!Array.isArray(groups)) {
    const text = canonical(rules);
    return [text.length > 200 ? `${text.slice(0, 200)}…` : text];
  }
  if (groups.length === 0) return ['None'];
  const lines: string[] = [];
  const all = (op: unknown) => (String(op).toUpperCase() === 'OR' ? 'any' : 'all');
  if (groups.length > 1) {
    lines.push(`Match ${all((rules as { logical_operator?: unknown }).logical_operator)} of these groups`);
  }
  groups.forEach((group, index) => {
    const g = group as { logical_operator?: unknown; conditions?: unknown };
    const conditions = Array.isArray(g.conditions) ? g.conditions : [];
    const text = conditions
      .map((c) => {
        const condition = c as { attribute?: unknown; operator?: unknown; value?: unknown };
        const operator = String(condition.operator ?? '').replace(/_/g, ' ');
        return `${String(condition.attribute ?? '')} ${operator} ${valueWord(condition.value)}`.trim();
      })
      .join('; ');
    lines.push(`Group ${index + 1}, match ${all(g.logical_operator)}: ${text || 'no conditions'}`);
  });
  return lines;
}

/**
 * The copied fields, as the target has them and as they will be once `source`
 * is copied in. Every field is listed, with whether it changes: none is left
 * out. A string, number or json flag (`valueFlag`) lists its split too, each
 * value in a bounded preview with its weight.
 */
export function promotionRows(
  source: FlagEnvironmentSettings,
  target: FlagEnvironmentSettings,
  valueFlag = false,
): PromotionRow[] {
  // No rules, however stored (null, {} or no groups), is one "None".
  const sameRules =
    (isNoRules(source.targeting_rules) && isNoRules(target.targeting_rules)) ||
    canonical(source.targeting_rules ?? null) === canonical(target.targeting_rules ?? null);
  const split: PromotionRow[] = valueFlag
    ? [
        {
          field: 'split',
          label: PROMOTED_FIELDS.split,
          now: describeSplit(target.split),
          after: describeSplit(source.split),
          changes: !sameSplit(source.split, target.split),
        },
      ]
    : [];
  return [
    {
      field: 'status',
      label: PROMOTED_FIELDS.status,
      now: [statusWord(target)],
      after: [statusWord(source)],
      changes: statusWord(source) !== statusWord(target),
    },
    {
      field: 'rollout',
      label: PROMOTED_FIELDS.rollout,
      now: [percentWord(target)],
      after: [percentWord(source)],
      changes: percentWord(source) !== percentWord(target),
    },
    {
      field: 'rules',
      label: PROMOTED_FIELDS.rules,
      now: describeRules(target.targeting_rules),
      after: describeRules(source.targeting_rules),
      changes: !sameRules,
    },
    ...split,
  ];
}

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

/** What promotion never copies. The schedule and safety settings belong to Production. */
export const NOT_COPIED_NOTE =
  "Not copied: the flag's rollout schedules and safety settings, and its type and values. They stay as they are.";

/**
 * The same for a string, number or json flag, whose split is copied: its
 * values in the split go with it, and what stays is the flag's own type, on
 * value and off value.
 */
export const NOT_COPIED_VALUE_FLAG_NOTE =
  "Not copied: the flag's rollout schedules and safety settings, and its type, on value and off value. They stay as they are.";

/** The note for a flag of this kind. */
export function notCopiedNote(valueFlag: boolean): string {
  return valueFlag ? NOT_COPIED_VALUE_FLAG_NOTE : NOT_COPIED_NOTE;
}

/** The source has nothing configured, so the target is turned off. */
export function nothingConfiguredText(source: FlagEnvironment, target: FlagEnvironment, targetIsOn: boolean): string {
  const from = FLAG_ENVIRONMENT_LABELS[source];
  const into = FLAG_ENVIRONMENT_LABELS[target];
  return targetIsOn
    ? `Nothing is configured in ${from}, so ${into} will be turned off.`
    : `Nothing is configured in ${from}, so ${into} stays off.`;
}

/** The dialog's summary sentence; a string, number or json flag's names the split too. */
export function promoteSummary(source: FlagEnvironment, target: FlagEnvironment, valueFlag = false): string {
  const fields = valueFlag ? 'status, rollout percentage, targeting rules and split' : 'status, rollout percentage and targeting rules';
  return `Copies ${FLAG_ENVIRONMENT_LABELS[source]}'s ${fields} into ${FLAG_ENVIRONMENT_LABELS[target]}.`;
}

/** Production's rollout schedule: promotion changes its percentage, not its progress. */
export function scheduleNote(status: string): string {
  // A draft schedule has not set it yet, so "again" would say it had.
  const again = status === 'Draft' ? '' : ' again';
  return `Production has a rollout schedule (${status}). Promotion does not copy or change its progress: the next stage that starts sets the rollout percentage${again}.`;
}

/** Production's schedule could not be read, so the dialog cannot say what it holds. */
export const SCHEDULE_UNKNOWN_NOTE =
  "Production's rollout schedule could not be read. Promotion does not copy or change its progress.";

/** The second step for Production. */
export const PRODUCTION_CONFIRM_TEXT = 'Production serves live users. Promoting changes what they get now.';

/** After a promotion, in the past: what the target was set to then, not what it is now. */
export function promotedText(source: FlagEnvironment, target: FlagEnvironment, done: FlagEnvironmentSettings): string {
  return `Promoted ${FLAG_ENVIRONMENT_LABELS[source]} to ${FLAG_ENVIRONMENT_LABELS[target]}. ${FLAG_ENVIRONMENT_LABELS[target]} was set to ${statusWord(done)} at ${percentWord(done)}.`;
}
