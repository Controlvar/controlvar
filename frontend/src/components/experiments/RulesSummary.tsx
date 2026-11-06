import React from 'react';
import { combineWord, describeCondition, describeDraftCondition } from '@/utils/experimentTargeting';

/** Shown for a group whose conditions have all been removed in the rule builder. */
export const NO_CONDITIONS = '(no conditions)';

interface RulesSummaryProps {
  /** Targeting rules in the dashboard shape: `{logical_operator, groups: [{logical_operator, conditions}]}`. */
  value: Record<string, unknown>;
  /**
   * Rules still in the rule builder, not yet sent (the new-experiment Review).
   * A blank attribute or value is then shown as not set, and a group left with
   * no conditions says so; creating refuses both and lists what to fix.
   */
  draft?: boolean;
}

/**
 * Targeting rules in words: who can join. The experiment page shows its
 * stored rules with this, and the new-experiment Review shows the rules it is
 * about to send, so the two read the same.
 */
export function RulesSummary({ value, draft = false }: RulesSummaryProps) {
  const groups = (value.groups as Record<string, unknown>[]) ?? [];
  const describe = draft ? describeDraftCondition : describeCondition;
  const conditionList = (group: Record<string, unknown>) => {
    const conditions = (group.conditions as Record<string, unknown>[]) ?? [];
    return (
      <ul className="mt-1 list-disc pl-5 space-y-0.5">
        {conditions.map((condition, ci) => (
          <li key={ci}>
            <code className="font-mono text-slate-800">{describe(condition)}</code>
          </li>
        ))}
        {draft && conditions.length === 0 && <li>{NO_CONDITIONS}</li>}
      </ul>
    );
  };

  if (groups.length === 1) {
    return (
      <div className="text-sm text-slate-700" data-testid="targeting-summary">
        <p>People who match {combineWord(groups[0].logical_operator)} of these conditions:</p>
        {conditionList(groups[0])}
      </div>
    );
  }
  return (
    <div className="text-sm text-slate-700" data-testid="targeting-summary">
      <p>People who match {combineWord(value.logical_operator)} of these groups:</p>
      <ol className="mt-1 space-y-2">
        {groups.map((group, gi) => (
          <li key={gi}>
            <p className="font-medium">
              Group {gi + 1}: {combineWord(group.logical_operator)} of these conditions
            </p>
            {conditionList(group)}
          </li>
        ))}
      </ol>
    </div>
  );
}
