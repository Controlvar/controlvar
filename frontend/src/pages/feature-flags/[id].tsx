import React, { useState, useEffect, useCallback, useId, useLayoutEffect, useRef } from 'react';
import { useRouter } from 'next/router';
import Link from 'next/link';
import { TargetingRuleBuilder } from '@/components/targeting';
import { InlineConfirm } from '@/components/ui/InlineConfirm';
import { OUTSIDE_BUILDER_NOTE, SaveAlert } from '@/components/experiments/TargetingSection';
import { checkTargeting } from '@/components/experiments/new/formState';
import { TargetingRules } from '@/types/targeting';
import { FLAG_OPERATOR_OPTIONS, createEmptyRules } from '@/utils/targeting';
import {
  TARGETING_SAVE_INCOMPLETE,
  TargetingSaveError,
  describeTargetingSaveError,
} from '@/utils/experimentTargeting';
import {
  NOT_GROUPS_NOTE,
  RuleServe,
  flagTargetingToSend,
  isEditableFlagTargeting,
  rulesFromStored,
  storedRuleValue,
  usesNotGroups,
} from '@/utils/flagTargeting';
import {
  RULE_VALUE_LABEL,
  SPLIT_CAPTION,
  SPLIT_ON_VALUE_HINT,
  SPLIT_ROLLOUT_HINT,
  SPLIT_STATUS_ON,
  SplitErrors,
  SplitRow,
  SplitToSend,
  hasSplitErrors,
  rowsFromSplit,
  ruleValueRefusal,
  splitHeading,
  splitRefusals,
  splitToSend,
  storedSplit,
  valueLabel,
  weightLabel,
} from '@/utils/flagSplit';
import {
  RuleServeControl,
  ServeState,
  SplitEditor,
  SplitErrorSummary,
  SplitReadOnly,
  SummaryItem,
} from '@/components/feature-flags/FlagSplit';
import { docsUrl } from '@/services/docs';
import { isApiError } from '@/services/api';
import {
  FeatureFlag,
  FeatureFlagsService,
  FlagEnvironmentSettings,
  FlagSplitEntry,
  RolloutSchedule,
  UpdateFeatureFlagRequest,
  UpdateFlagEnvironmentRequest,
  isFlagOn,
} from '@/services/featureFlags';
import { useFlagEnvironment } from '@/contexts/FlagEnvironmentContext';
import {
  FLAG_ENVIRONMENT_LABELS,
  FlagEnvironment,
  NonProductionEnvironment,
  inEnvironmentUrl,
  notConfiguredText,
} from '@/utils/flagEnvironments';
import {
  EnvironmentComparison,
  NonProductionNote,
  ProductionBanner,
} from '@/components/feature-flags/FlagEnvironmentSections';
import type { SafetyCheckResponse } from '@/types/safety';
import { PageTitle } from '@/components/PageTitle';
import { formatSafetyMetricValue } from '@/components/admin/safety/SafetyStatusCard';
import { FlagToggle } from '@/components/feature-flags/FlagToggle';
import {
  ARCHIVED_LABEL,
  ArchivedFlagNote,
  isFlagArchived,
} from '@/components/feature-flags/ArchivedFlag';
import { FlagManageSection, FlagStatusChange } from '@/components/feature-flags/FlagManageSection';
import { PromoteFlag } from '@/components/feature-flags/PromoteFlag';
import { promotionTarget } from '@/utils/flagPromotion';
import { RolloutScheduleManager } from '@/components/feature-flags/RolloutScheduleManager';
import {
  FlagValueFields,
  FlagValueReadOnly,
  useFlagValueFields,
} from '@/components/feature-flags/FlagValueFields';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { useEffectivePermissions } from '@/contexts/EffectivePermissionsContext';
import { useDemoMode } from '@/contexts/AuthConfigContext';
import { DemoNote } from '@/components/DemoNote';
import { DEMO_FLAGS_ACTION } from '@/utils/demoMode';
import { FLAG_ROLE_NOTE, canChangeFeatureFlags, canDeleteFeatureFlags } from '@/utils/experimentPermissions';
import { parseApiTimestamp } from '@/utils/apiTimestamp';
import {
  ROLLOUT_VALUE_HINT,
  TYPE_FIXED_ON_PAGE,
  VALUE_FIELDS,
  VALUE_FLAG_STATUS_OFF,
  VALUE_FLAG_STATUS_ON,
  ValueField,
  ValueTexts,
  checkEditedValues,
  firstInvalid,
  flagValueType,
  isValueFlagType,
  parseValue,
  typeLabel,
  valueRefusals,
  valueToText,
} from '@/utils/flagValues';

// The rollout schedule's stage summary lives with the component that draws
// it; the page's tests read it from here.
export { stageSummary } from '@/components/feature-flags/RolloutScheduleManager';

const SCHEDULE_STATUS_COLORS: Record<string, string> = {
  draft: 'bg-slate-100 text-slate-700',
  active: 'bg-green-100 text-green-800',
  paused: 'bg-yellow-100 text-yellow-800',
  completed: 'bg-blue-100 text-blue-800',
  cancelled: 'bg-gray-100 text-gray-600',
};

/** The rollout schedule section's load failure, in fixed words. */
export const SCHEDULES_LOAD_ERROR = 'Could not load the rollout schedule. Reload the page to try again.';

/** Under the slider while a schedule is active. */
export const SLIDER_SCHEDULE_NOTE =
  'A rollout schedule is active: the next stage that starts sets this percentage again.';

function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const d = parseApiTimestamp(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

function humanize(value: string): string {
  return value.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

/** Metrics computed by dividing by the flag's recorded evaluations. */
const RATE_METRICS = new Set(['error_rate']);

/**
 * Errors reported but no evaluations recorded in the check's window: the error
 * rate has nothing to divide by and reads 0, which is not "healthy". Returns
 * the counts to show, or null when the rate is measurable.
 */
export function unmeasuredErrorRate(
  check: SafetyCheckResponse | null,
): { errors: number; minutes: number } | null {
  const details = check?.details;
  if (!details) return null;
  const errors = details.error_count;
  const evaluations = details.total_evaluations;
  if (typeof errors !== 'number' || typeof evaluations !== 'number') return null;
  if (errors <= 0 || evaluations !== 0) return null;
  const minutes = typeof details.timeframe_minutes === 'number' ? details.timeframe_minutes : 15;
  return { errors, minutes };
}

/**
 * Error reports in the check's window that were stored but do not count toward
 * the error rate or automatic rollback: they were sent with a key without the
 * `sdk:ruleset` scope, or one whose owner can no longer change flags. Returns
 * the counts to show, or null when there are none.
 */
export function uncountedErrorReports(
  check: SafetyCheckResponse | null,
): { reports: number; minutes: number } | null {
  const details = check?.details;
  if (!details) return null;
  const reports = details.uncounted_error_count;
  if (typeof reports !== 'number' || reports <= 0) return null;
  const minutes = typeof details.timeframe_minutes === 'number' ? details.timeframe_minutes : 15;
  return { reports, minutes };
}

/**
 * The flag's stored `targeting_rules` exactly as the API returned them, under
 * either key the detail and list shapes use. Unlike `flagRules`, a value that
 * is not an object is kept, so it is shown rather than read as "no rules".
 */
function storedRules(flag: Pick<FeatureFlag, 'targeting_rules' | 'rules'>): unknown {
  return flag.targeting_rules ?? flag.rules ?? null;
}

/** Pick the schedule worth showing: active > paused > draft > most recent. */
export function pickSchedule(items: RolloutSchedule[]): RolloutSchedule | null {
  if (items.length === 0) return null;
  const rank: Record<string, number> = { active: 0, paused: 1, draft: 2, completed: 3, cancelled: 4 };
  return [...items].sort((a, b) => {
    const r = (rank[a.status] ?? 9) - (rank[b.status] ?? 9);
    if (r !== 0) return r;
    return parseApiTimestamp(b.updated_at).getTime() - parseApiTimestamp(a.updated_at).getTime();
  })[0];
}

/**
 * A flag's page in the environment on screen. Choosing another environment
 * starts the page again (the key), so nothing of the last one is left: a
 * change still being saved there cannot answer into this one's form and be
 * saved from it.
 */
export default function FeatureFlagDetailPage() {
  const { environment } = useFlagEnvironment();
  const [loading, setLoading] = useState(true);
  return (
    <>
      {/* On the page from the first render, so a load after a switch is announced. */}
      <div role="status" className={loading ? 'flex-1 bg-slate-50 flex items-center justify-center' : undefined}>
        {loading && (
          <>
            <PageTitle title="Feature Flag" />
            <div className="text-center" data-testid="flag-loading">
              <div className="inline-block w-6 h-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
              <p className="text-slate-500 mt-2 text-sm">Loading feature flag...</p>
            </div>
          </>
        )}
      </div>
      <FlagInEnvironment key={environment ?? 'resolving'} environment={environment} onLoading={setLoading} />
    </>
  );
}

function FlagInEnvironment({
  environment,
  onLoading,
}: {
  environment: FlagEnvironment | null;
  onLoading: (loading: boolean) => void;
}) {
  const router = useRouter();
  const { reportShown, switchable } = useFlagEnvironment();
  const rawId = router.query.id;
  const id = typeof rawId === 'string' ? rawId : undefined;
  const auth = useOptionalAuth();
  const { effective, loading: permissionsLoading } = useEffectivePermissions();
  // A user without FEATURE_FLAG UPDATE (by role, custom role or direct grant)
  // reads a flag and changes none of it (#917, #891): no switch, the rule
  // builder read-only, the slider disabled, no Save, no Archive or Unarchive
  // (#442), and a note why -- the note only once the effective map has
  // answered. Delete asks its own question (FEATURE_FLAG DELETE), below.
  const canChange = canChangeFeatureFlags(auth?.user, effective);
  // Delete is FEATURE_FLAG DELETE, which a custom role or grant can give
  // without UPDATE, or withhold from a holder of it (#442).
  const canDelete = canDeleteFeatureFlags(auth?.user, effective);
  const changePending = !canChange && permissionsLoading;
  // A read-only demo shows the demo note in place of the role note.
  const demo = useDemoMode();
  // `environment` is null while it is resolved, and the page is loading until
  // then. Production is the flag itself, read and changed through the stable
  // routes as before; Staging and Development are the flag's settings there,
  // read and changed through the beta environment routes. Nothing else
  // decides the route.
  const otherEnvironment: NonProductionEnvironment | null =
    environment && environment !== 'production' ? environment : null;
  const production = environment === 'production';

  const [flag, setFlag] = useState<FeatureFlag | null>(null);
  // The flag's settings in `otherEnvironment`; null in Production.
  const [envSettings, setEnvSettings] = useState<FlagEnvironmentSettings | null>(null);
  // Moves after every change made on this page, so an open comparison reloads.
  const [changeCount, setChangeCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<{ status: number; message: string } | null>(null);
  const loading = !id || isLoading;
  const failed = loadError !== null;

  // Before the paint, so the loading state and the flag are never both on screen.
  useLayoutEffect(() => onLoading(loading), [loading, onLoading]);
  // What is on screen, for the environment switcher's status.
  useEffect(() => {
    reportShown(environment && !loading ? { environment, loaded: !failed } : null);
    return () => reportShown(null);
  }, [environment, loading, failed, reportShown]);

  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  // On/off
  const [toggling, setToggling] = useState(false);
  // Set at once by a press; `toggling` only changes on the next render, so a second
  // press in the same tick would otherwise send a second request.
  const toggleLock = useRef(false);
  const saveLock = useRef(false);
  const [toggleError, setToggleError] = useState<string | null>(null);

  // Editable fields. `stored` is the flag's targeting_rules as last loaded or
  // saved: the baseline a save compares the builder against. `rules` is the
  // builder's state, or null while stored rules the builder cannot show are
  // displayed read-only; `replaced` is set once the user confirms replacing them.
  const [stored, setStored] = useState<unknown>(null);
  const [rules, setRules] = useState<TargetingRules | null>(null);
  const [replaced, setReplaced] = useState(false);
  const [confirmingReplace, setConfirmingReplace] = useState(false);
  const [targetingError, setTargetingError] = useState<TargetingSaveError | null>(null);
  const [rolloutPercentage, setRolloutPercentage] = useState(0);
  const saveErrorRef = useRef<HTMLDivElement>(null);
  // Replace rules (#1797): the button the question replaces, the stored
  // rules beside it, and the section the empty builder opens in. A confirmed
  // replace moves focus to the builder's one control, + Add Group.
  const replaceButton = useRef<HTMLButtonElement>(null);
  const storedRulesRegion = useRef<HTMLPreElement>(null);
  const targetingSection = useRef<HTMLElement>(null);
  const replaceTextId = useId();
  const [focusAddGroup, setFocusAddGroup] = useState<object | null>(null);

  useEffect(() => {
    if (focusAddGroup) targetingSection.current?.querySelector<HTMLElement>('[data-testid="add-group"]')?.focus();
  }, [focusAddGroup]);

  // A value flag's two values (#1497). `storedValues` is the pair as last
  // loaded or saved; `initialTexts` the text each field started from, so a
  // save sends a value only when its field was changed.
  const [storedValues, setStoredValues] = useState<Record<ValueField, unknown>>({
    on_value: undefined,
    off_value: undefined,
  });
  const [initialTexts, setInitialTexts] = useState<ValueTexts>({ on_value: '', off_value: '' });
  const {
    texts: valueTexts,
    setTexts: setValueTexts,
    errors: valueErrors,
    setErrors: setValueErrors,
    showErrors: showValueErrors,
    change: handleValueChange,
    fieldRefs: valueRefs,
  } = useFlagValueFields();

  useEffect(() => {
    if (saveError) saveErrorRef.current?.focus();
  }, [saveError]);

  /** Show *values* as the flag's stored pair, in the fields when it has a type the page can edit. */
  const loadValues = useCallback(
    (type: string, values: Record<ValueField, unknown>) => {
      setStoredValues(values);
      const texts: ValueTexts = { on_value: '', off_value: '' };
      if (isValueFlagType(type)) {
        for (const field of VALUE_FIELDS) texts[field] = valueToText(type, values[field]);
      }
      setValueTexts(texts);
      setInitialTexts(texts);
      setValueErrors({});
    },
    [setValueTexts, setValueErrors],
  );

  // The environment's split (#2012). `loadedSplit` is the split as last loaded
  // or saved: `undefined` when the API answered no `split` (it is older, and
  // refuses the field, so the page offers none and never sends one), `null`
  // for none. `splitRows` is the editor's state; a save sends `split` only
  // when it was touched and differs from `loadedSplit` (`splitToSend`).
  const [loadedSplit, setLoadedSplit] = useState<FlagSplitEntry[] | null | undefined>(undefined);
  const [splitRows, setSplitRows] = useState<SplitRow[] | null>(null);
  const [splitTouched, setSplitTouched] = useState(false);
  const [splitErrors, setSplitErrors] = useState<SplitErrors>({ rows: {} });
  // Moves on every load, so the editor starts again from what was loaded.
  const [splitLoads, setSplitLoads] = useState(0);
  // The dashboard rule's Serve control: its default, or a value of its own.
  // An untouched stored value is sent back exactly as stored.
  const [serve, setServe] = useState<ServeState>({ own: false, text: '' });
  const [serveTouched, setServeTouched] = useState(false);
  const [serveError, setServeError] = useState<string | null>(null);
  // What stopped the last save, each linked to its field; the first takes focus.
  const [summary, setSummary] = useState<SummaryItem[]>([]);
  const focusFirst = useRef<string | null>(null);

  useEffect(() => {
    const id = focusFirst.current;
    if (!id) return;
    focusFirst.current = null;
    document.getElementById(id)?.focus();
  }, [summary]);

  /** Show `value` as the stored rules: in the builder when it can, read-only otherwise. */
  const loadRules = useCallback((value: unknown, type: string) => {
    setStored(value);
    const editable = isEditableFlagTargeting(value, type);
    setRules(editable ? rulesFromStored(value) : null);
    setReplaced(false);
    setConfirmingReplace(false);
    const own = storedRuleValue(value);
    setServe(
      editable && own !== undefined && isValueFlagType(type)
        ? { own: true, text: valueToText(type, own) }
        : { own: false, text: '' },
    );
    setServeTouched(false);
    setServeError(null);
  }, []);

  /** Show the split of `body` (a flag or an environment's settings), when it answered one. */
  const loadSplit = useCallback((type: string, body: { split?: unknown } | null) => {
    let split: FlagSplitEntry[] | null | undefined;
    if (body === null || !('split' in body)) split = undefined;
    else if (body.split === null || body.split === undefined) split = null;
    else split = storedSplit(body.split) ?? undefined;
    setLoadedSplit(split);
    setSplitRows(split && isValueFlagType(type) ? rowsFromSplit(type, split) : null);
    setSplitTouched(false);
    setSplitErrors({ rows: {} });
    setSplitLoads((n) => n + 1);
  }, []);

  /** What stopped a save: each refusal at its field, a summary linking them, focus on the first. */
  const showSplitProblems = useCallback((errors: SplitErrors, ruleValue: string | null) => {
    setSplitErrors(errors);
    setServeError(ruleValue);
    const items: SummaryItem[] = [];
    for (const index of Object.keys(errors.rows).map(Number).sort((a, b) => a - b)) {
      const at = errors.rows[index];
      if (at.value) items.push({ target: `split-value-${index}`, text: `${valueLabel(index + 1)}: ${at.value}` });
      if (at.weight) items.push({ target: `split-weight-${index}`, text: `${weightLabel(index + 1)}: ${at.weight}` });
    }
    if (errors.total) {
      const weights = errors.total.startsWith('Weights');
      items.push({ target: weights ? 'split-weight-0' : 'split-value-0', text: `${SPLIT_CAPTION}: ${errors.total}` });
    }
    if (ruleValue) items.push({ target: 'rule-serve-value', text: `${RULE_VALUE_LABEL}: ${ruleValue}` });
    focusFirst.current = items[0]?.target ?? null;
    setSummary(items);
  }, []);

  const clearSplitProblems = () => {
    setSplitErrors({ rows: {} });
    setServeError(null);
    setSummary([]);
  };

  // Rollout schedule
  const [schedules, setSchedules] = useState<RolloutSchedule[] | null>(null);
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  // Where focus goes once a schedule change lands: the button that asked for
  // it unmounts or changes (Activate becomes Pause, Start stage becomes
  // Complete stage), so the section's heading is the stable place.
  const scheduleHeading = useRef<HTMLHeadingElement>(null);

  // Safety
  const [safety, setSafety] = useState<SafetyCheckResponse | null>(null);
  const [safetyError, setSafetyError] = useState<string | null>(null);
  const [safetyLoading, setSafetyLoading] = useState(false);
  const unmeasured = unmeasuredErrorRate(safety);
  const uncounted = uncountedErrorReports(safety);

  const loadSafety = useCallback(async (flagId: string) => {
    setSafetyLoading(true);
    setSafetyError(null);
    try {
      setSafety(await FeatureFlagsService.safetyCheck(flagId));
    } catch (err) {
      setSafetyError(err instanceof Error ? err.message : 'Safety check unavailable');
    } finally {
      setSafetyLoading(false);
    }
  }, []);

  const loadSchedules = useCallback(async (flagId: string) => {
    setScheduleError(null);
    try {
      const res = await FeatureFlagsService.listRolloutSchedules(flagId);
      setSchedules(res.items ?? []);
    } catch {
      setSchedules([]);
      setScheduleError(SCHEDULES_LOAD_ERROR);
    }
  }, []);

  /**
   * After a schedule change: the schedules again and, when a stage was
   * advanced, the flag too. An advance sets the flag's rollout percentage on
   * the server, so the slider takes the new value; a Save with the old one
   * would undo the stage.
   */
  const afterRolloutChange = useCallback(
    async (flagId: string, { flagMoved }: { flagMoved: boolean }) => {
      await loadSchedules(flagId);
      if (!flagMoved) return;
      try {
        const data = await FeatureFlagsService.get(flagId);
        setFlag((current) => (current ? { ...current, ...data } : data));
        setRolloutPercentage(data.rollout_percentage ?? 0);
      } catch {
        // The schedule list above already shows the stage; the slider keeps
        // its value until the page is reloaded.
      }
    },
    [loadSchedules],
  );

  useEffect(() => {
    if (!id || !environment) return;
    let cancelled = false;
    const inEnvironment = environment === 'production' ? null : environment;

    const fetchFlag = async () => {
      setIsLoading(true);
      setLoadError(null);
      try {
        // Outside Production the flag itself still names the page and holds
        // the values; its settings there come from the environment's route.
        const [data, settings] = await Promise.all([
          FeatureFlagsService.get(id),
          inEnvironment ? FeatureFlagsService.getInEnvironment(id, inEnvironment) : Promise.resolve(null),
        ]);
        if (cancelled) return;
        setFlag(data);
        setEnvSettings(settings);
        const type = flagValueType(data);
        loadValues(type, { on_value: data.on_value, off_value: data.off_value });
        if (settings) {
          loadRules(settings.targeting_rules ?? null, type);
          loadSplit(type, settings);
          setRolloutPercentage(settings.rollout_percentage ?? 0);
          // The rollout schedule and the safety check are Production's only.
          return;
        }
        loadRules(storedRules(data), type);
        loadSplit(type, data);
        setRolloutPercentage(data.rollout_percentage ?? 0);
        // Secondary panels load in parallel; their failures are non-fatal.
        void loadSchedules(id);
        void loadSafety(id);
      } catch (err) {
        if (cancelled) return;
        setLoadError({
          status: isApiError(err) ? err.status : 0,
          message: err instanceof Error ? err.message : 'Failed to load feature flag',
        });
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    void fetchFlag();
    return () => {
      cancelled = true;
    };
  }, [id, environment, loadSchedules, loadSafety, loadRules, loadValues, loadSplit]);

  /** On/off in Staging or Development: the environment's PUT, never the flag's enable or disable. */
  const handleEnvironmentToggle = async (next: boolean) => {
    if (!flag || !otherEnvironment || !envSettings) return;
    const previous = envSettings;
    setToggleError(null);
    setToggling(true);
    setEnvSettings({ ...envSettings, is_active: next, status: next ? 'active' : 'inactive', configured: true });
    try {
      // The PUT answers the setting as it now is.
      setEnvSettings(await FeatureFlagsService.updateInEnvironment(flag.id, otherEnvironment, { is_active: next }));
      setChangeCount((n) => n + 1);
    } catch (err) {
      setEnvSettings(previous);
      setToggleError(err instanceof Error ? err.message : 'Failed to update the flag');
    } finally {
      setToggling(false);
    }
  };

  const handleToggle = async (next: boolean) => {
    if (!flag) return;
    if (otherEnvironment) {
      await handleEnvironmentToggle(next);
      return;
    }
    const previous = flag;
    setToggleError(null);
    setToggling(true);
    setFlag({ ...flag, status: next ? 'active' : 'inactive', is_active: next });
    try {
      const res = await FeatureFlagsService.setEnabled(flag.id, next);
      setFlag((current) =>
        current
          ? { ...current, status: res.status, is_active: res.status === 'active', updated_at: res.updated_at }
          : current,
      );
      setChangeCount((n) => n + 1);
    } catch (err) {
      setFlag(previous);
      setToggleError(err instanceof Error ? err.message : 'Failed to update the flag');
    } finally {
      setToggling(false);
    }
  };

  /**
   * A refusal of the split or of the rule's own value, shown at its field;
   * answers what is left of the error for the page's other places, or null.
   */
  const placeSplitRefusals = (err: unknown): unknown => {
    const split = splitRefusals(err);
    const afterSplit = split ? split.rest : err;
    const ruleValue = ruleValueRefusal(afterSplit);
    if (split || ruleValue) showSplitProblems(split?.errors ?? { rows: {} }, ruleValue?.message ?? null);
    return ruleValue ? ruleValue.rest : afterSplit;
  };

  /**
   * Save in Staging or Development: the rollout, and the rules and the split
   * when they were changed, through the environment's PUT.
   */
  const saveInEnvironment = async (
    env: NonProductionEnvironment,
    targeting: Record<string, unknown> | undefined,
    split: SplitToSend,
  ) => {
    if (!flag) return;
    const type = flagValueType(flag);
    const body: UpdateFlagEnvironmentRequest = { rollout_percentage: rolloutPercentage };
    if (targeting !== undefined) body.targeting_rules = targeting;
    if (split.kind === 'send') body.split = split.split;
    setIsSaving(true);
    try {
      // The PUT answers the setting as it now is.
      const saved = await FeatureFlagsService.updateInEnvironment(flag.id, env, body);
      setEnvSettings(saved);
      loadRules(saved.targeting_rules ?? null, type);
      loadSplit(type, saved);
      setRolloutPercentage(saved.rollout_percentage ?? 0);
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 3000);
      setChangeCount((n) => n + 1);
    } catch (err) {
      const other = placeSplitRefusals(err);
      if (other) {
        const described = describeTargetingSaveError(other);
        if (described.problems) {
          setTargetingError(described);
        } else {
          setSaveError(other instanceof Error ? other.message : 'Failed to save changes');
        }
      }
    } finally {
      setIsSaving(false);
    }
  };

  const handleSave = async () => {
    if (!flag) return;
    setSaveError(null);
    setTargetingError(null);
    setSaveSuccess(false);
    setValueErrors({});
    clearSplitProblems();

    // A value is sent only when its field was changed, as the rules are below:
    // a save that moves only the slider never rewrites a value someone else
    // has just changed. The type is never sent.
    const type = flagValueType(flag);
    let values: Partial<Record<ValueField, unknown>> = {};
    // Values belong to the flag, so only Production's page edits them.
    if (canChange && isValueFlagType(type) && !otherEnvironment) {
      const checked = checkEditedValues(type, valueTexts, initialTexts, storedValues);
      if (firstInvalid(checked.errors)) {
        showValueErrors(checked.errors);
        return;
      }
      values = checked.values;
    }

    // The split (#2012) is sent only when it was changed from what was loaded,
    // as the rules are below, and never to an API that answered none. The
    // rule's own value is part of the rules: an untouched stored one is sent
    // back exactly as stored, a typed one as the flag's type. The stored one
    // is kept on a page whose API answered no split too (no Serve control is
    // shown there): the rules' baseline holds it, so leaving it out would send
    // the rules without it.
    let split: SplitToSend = { kind: 'leave' };
    let ruleServe: RuleServe =
      serve.own && !serveTouched && rules && rules.groups.length > 0
        ? { own: true, value: storedRuleValue(stored) }
        : { own: false };
    if (canChange && isValueFlagType(type) && loadedSplit !== undefined) {
      split = splitToSend(type, splitRows, loadedSplit, splitTouched);
      let ruleValueError: string | null = null;
      if (serve.own && serveTouched && rules && rules.groups.length > 0) {
        const parsed = parseValue(type, 'value', serve.text);
        if (parsed.ok) ruleServe = { own: true, value: parsed.value };
        else ruleValueError = parsed.message;
      }
      const splitErrorsNow = split.kind === 'refused' ? split.errors : { rows: {} };
      if (hasSplitErrors(splitErrorsNow) || ruleValueError) {
        showSplitProblems(splitErrorsNow, ruleValueError);
        return;
      }
    }

    // Rules are sent only when they were changed (or replaced), so a save
    // that moves only the rollout leaves the stored rules exactly as they are.
    const targeting = flagTargetingToSend(rules, ruleServe, stored, replaced);
    if (targeting !== undefined && rules) {
      const problems = checkTargeting(rules);
      if (problems.length > 0) {
        setTargetingError({ message: TARGETING_SAVE_INCOMPLETE, problems });
        return;
      }
    }

    // Staging and Development: the environment's PUT. Production: the flag's
    // own PUT below, exactly as before environments.
    if (otherEnvironment) {
      await saveInEnvironment(otherEnvironment, targeting, split);
      return;
    }

    const body: UpdateFeatureFlagRequest = { rollout_percentage: rolloutPercentage };
    for (const field of VALUE_FIELDS) {
      if (field in values) body[field] = values[field];
    }
    if (targeting !== undefined) body.targeting_rules = targeting;
    if (split.kind === 'send') body.split = split.split;

    setIsSaving(true);
    try {
      const updated = await FeatureFlagsService.update(flag.id, body);
      setFlag((current) => ({ ...(current ?? updated), ...updated }));
      const answered = 'targeting_rules' in updated || 'rules' in updated;
      loadRules(answered ? storedRules(updated) : (targeting ?? stored), type);
      // The split as the PUT answered it; one that answered none keeps what was sent, or loaded.
      loadSplit(
        type,
        'split' in updated
          ? updated
          : split.kind === 'send'
            ? { split: split.split }
            : loadedSplit === undefined
              ? null
              : { split: loadedSplit },
      );
      const saved = { ...storedValues, ...values };
      for (const field of VALUE_FIELDS) {
        if (field in updated) saved[field] = updated[field];
      }
      loadValues(type, saved);
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 3000);
      setChangeCount((n) => n + 1);
    } catch (err) {
      const refused = valueRefusals(err);
      if (refused) showValueErrors(refused.fields);
      const afterValues = refused ? refused.rest : err;
      const other = afterValues ? placeSplitRefusals(afterValues) : null;
      if (other) {
        const described = describeTargetingSaveError(other);
        if (described.problems) {
          setTargetingError(described);
        } else {
          setSaveError(other instanceof Error ? other.message : 'Failed to save changes');
        }
      }
    } finally {
      setIsSaving(false);
    }
  };

  // Back to the list in the environment on screen.
  const listUrl = inEnvironmentUrl('/feature-flags', switchable ? environment : null);

  // FeatureFlagDetailPage shows the loading state.
  if (loading) return null;

  if (loadError || !flag) {
    const notFound = loadError?.status === 404;
    return (
      <div className="flex-1 bg-slate-50 flex items-center justify-center">
        <PageTitle title={notFound ? 'Flag not found' : 'Feature Flag'} />
        <div
          role="alert"
          className="text-center max-w-md px-4"
          data-testid={notFound ? 'flag-not-found' : 'flag-error'}
        >
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-600 mb-2">
            {notFound ? '404' : 'Error'}
          </p>
          <p className="text-slate-800 text-lg mb-2">
            {notFound ? 'Feature flag not found' : 'Could not load this feature flag'}
          </p>
          {!notFound && loadError?.message && (
            <p className="text-sm text-red-600 mb-4">{loadError.message}</p>
          )}
          <Link href={listUrl} className="text-blue-600 hover:underline text-sm">
            &larr; Back to Feature Flags
          </Link>
        </div>
      </div>
    );
  }

  // Outside Production the pill, the switch and the archived note follow the
  // flag's setting in that environment; an archived flag is archived in all.
  const on = isFlagOn(envSettings ?? flag);
  // The API refuses to turn an archived flag on (400), so the page offers no
  // switch for one: it says to unarchive it first (#1748), and Unarchive is
  // in "Manage this flag" below (#442).
  const archived = isFlagArchived(flag) || (envSettings !== null && isFlagArchived(envSettings));
  // Off there because nothing was ever set there: "Off", with the reason beside it.
  const notConfigured = otherEnvironment !== null && envSettings !== null && !envSettings.configured;
  const environmentLabel = otherEnvironment ? FLAG_ENVIRONMENT_LABELS[otherEnvironment] : null;
  // Where this environment's settings are promoted to: null in Production.
  const promotionInto = promotionTarget(environment);
  // In Production the banner says what a change here reaches; a user who
  // cannot change the flag, or a read-only demo, is not editing.
  const showProductionBanner = production && canChange && !demo;
  /** An archive or unarchive has landed: the pill, the switch and the note follow it. */
  const handleStatusChange = (change: FlagStatusChange) => {
    setToggleError(null);
    setFlag((current) => (current ? { ...current, ...change } : current));
  };
  const schedule = schedules ? pickSchedule(schedules) : null;
  // A boolean flag (or one from a server without values) keeps the page it
  // had: no type, no Values section, "Serving" / "Not serving".
  const valueType = flagValueType(flag);
  const valueFlag = valueType !== 'boolean';
  // A string, number or json flag, from an API that answers its split (#2012):
  // the Split section and the rule's Serve control. A boolean flag has neither.
  const splitType = isValueFlagType(valueType) && loadedSplit !== undefined ? valueType : null;
  // What this environment serves now, for the words that describe it.
  const servesSplit = Array.isArray(loadedSplit) && loadedSplit.length > 0;
  const statusText = valueFlag
    ? on
      ? servesSplit
        ? SPLIT_STATUS_ON
        : VALUE_FLAG_STATUS_ON
      : VALUE_FLAG_STATUS_OFF
    : on
      ? 'Serving'
      : 'Not serving';

  return (
    <div className="flex-1 bg-slate-50" data-testid="flag-detail">
      <PageTitle title={environmentLabel ? `${flag.name} in ${environmentLabel}` : flag.name} />
      <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Header */}
        <div className="flex items-center gap-4 mb-4">
          <Link href={listUrl} className="text-slate-500 hover:text-slate-700 text-sm">
            &larr; Feature Flags
          </Link>
        </div>

        {showProductionBanner && <ProductionBanner />}

        <div className="bg-white rounded-lg border border-slate-200 p-6 mb-6" data-testid="flag-header">
          <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-3">
                <h1 className="text-2xl font-bold text-slate-900 break-words" data-testid="flag-name">
                  {flag.name}
                </h1>
                <span
                  className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                    on
                      ? 'bg-green-100 text-green-800'
                      : archived
                        ? 'bg-slate-200 text-slate-800'
                        : 'bg-slate-100 text-slate-700'
                  }`}
                  data-testid="flag-status"
                  data-status={archived ? 'archived' : on ? 'active' : 'inactive'}
                >
                  {archived ? ARCHIVED_LABEL : on ? 'On' : 'Off'}
                </span>
                {notConfigured && otherEnvironment && (
                  <span className="text-sm text-slate-700" data-testid="flag-not-configured">
                    {notConfiguredText(otherEnvironment)}
                  </span>
                )}
                {valueFlag && (
                  <span
                    className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-slate-100 text-slate-700"
                    data-testid="flag-value-type"
                  >
                    {typeLabel(valueType)} flag
                  </span>
                )}
              </div>
              <p className="text-sm text-slate-500 mt-1 font-mono" data-testid="flag-key">
                {flag.key}
              </p>
              {flag.description && (
                <p className="text-sm text-slate-600 mt-2" data-testid="flag-description">
                  {flag.description}
                </p>
              )}
            </div>

            <div className="flex items-center gap-3 shrink-0">
              <span className="text-sm text-slate-600">{statusText}</span>
              {canChange && !archived && (
                <FlagToggle
                  on={on}
                  pending={toggling}
                  label={`Turn ${flag.key} ${on ? 'off' : 'on'}${environmentLabel ? ` in ${environmentLabel}` : ''}`}
                  onChange={async (next) => {
                    if (toggleLock.current) return;
                    toggleLock.current = true;
                    try {
                      await handleToggle(next);
                    } finally {
                      toggleLock.current = false;
                    }
                  }}
                  data-testid="flag-toggle"
                />
              )}
            </div>
          </div>
          {archived && <ArchivedFlagNote />}
          {/* Development is promoted to Staging and Staging to Production, by a user who may
              change the target (the same permission as a change there), never an archived
              flag. The dialog reads both environments itself. */}
          {environment && promotionInto && canChange && !demo && !archived && (
            <PromoteFlag
              flagId={flag.id}
              flagKey={flag.key}
              source={environment}
              target={promotionInto}
              valueFlag={splitType !== null}
              onPromoted={() => setChangeCount((n) => n + 1)}
            />
          )}
          {demo ? (
            <DemoNote action={DEMO_FLAGS_ACTION} className="mt-4 text-sm text-slate-600" />
          ) : (
            !canChange &&
            !changePending && (
              <p className="mt-4 text-sm text-slate-600" data-testid="flag-role-note">
                {FLAG_ROLE_NOTE}
              </p>
            )
          )}
          {/* Archive and Unarchive for a user who may change flags, Delete for one who may
              delete them (#442); the role note above otherwise. They change the flag in
              every environment, so they are offered in Production only. */}
          {production && (
            <FlagManageSection
              flag={flag}
              canChange={canChange}
              canDelete={canDelete}
              onChanged={handleStatusChange}
            />
          )}
          {toggleError && (
            <div
              role="alert"
              className="mt-4 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
              data-testid="flag-toggle-error"
            >
              {toggleError}
            </div>
          )}
        </div>

        {/* Outside Production: no schedule or safety controls, and why. */}
        {!production && <NonProductionNote valueFlag={splitType !== null} />}

        {production && (
        <div className="grid gap-6 lg:grid-cols-2 mb-6">
          {/* Rollout schedule */}
          <section className="bg-white rounded-lg border border-slate-200 p-6" data-testid="rollout-schedule-section">
            <div className="flex items-center justify-between mb-3">
              <h2
                ref={scheduleHeading}
                tabIndex={-1}
                className="text-base font-semibold text-slate-800 focus:outline-none"
              >
                Rollout schedule
              </h2>
              {schedule && (
                <span
                  className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                    SCHEDULE_STATUS_COLORS[schedule.status] ?? 'bg-slate-100 text-slate-700'
                  }`}
                  data-testid="rollout-schedule-status"
                >
                  {humanize(schedule.status)}
                </span>
              )}
            </div>

            {schedules === null && !scheduleError && (
              <p className="text-sm text-slate-500" data-testid="rollout-schedule-loading">
                Loading…
              </p>
            )}
            {scheduleError && (
              <p role="alert" className="text-sm text-red-700" data-testid="rollout-schedule-error">
                {scheduleError}
              </p>
            )}
            {schedules !== null && !scheduleError && !schedule && (
              <p className="text-sm text-slate-500" data-testid="rollout-schedule-empty">
                {canChange ? (
                  <>
                    No rollout schedule. The rollout percentage below applies at once; a schedule
                    raises it in stages. Create one here, or through{' '}
                    <code className="font-mono">/api/v1/rollout-schedules</code>.
                  </>
                ) : (
                  'No rollout schedule.'
                )}
              </p>
            )}
            {schedules !== null && !scheduleError && (
              <RolloutScheduleManager
                flagId={flag.id}
                flagPercentage={flag.rollout_percentage ?? 0}
                schedule={schedule}
                canChange={canChange}
                headingRef={scheduleHeading}
                onChanged={(change) => afterRolloutChange(flag.id, change)}
              />
            )}
          </section>

          {/* Safety */}
          <section className="bg-white rounded-lg border border-slate-200 p-6" data-testid="safety-section">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-base font-semibold text-slate-800">Safety check</h2>
              <div className="flex items-center gap-2">
                {safety && unmeasured && (
                  <span
                    className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border border-slate-300 bg-white text-slate-800"
                    data-testid="safety-status"
                    data-healthy="unknown"
                  >
                    Error rate unknown
                  </span>
                )}
                {safety && !unmeasured && (
                  <span
                    className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                      safety.is_healthy ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                    }`}
                    data-testid="safety-status"
                    data-healthy={safety.is_healthy ? 'true' : 'false'}
                  >
                    {safety.is_healthy ? 'Healthy' : 'Unhealthy'}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => void loadSafety(flag.id)}
                  disabled={safetyLoading}
                  className="text-xs text-blue-600 hover:underline disabled:opacity-50"
                  data-testid="safety-recheck"
                >
                  {safetyLoading ? 'Checking…' : 'Re-check'}
                </button>
              </div>
            </div>

            {!safety && safetyLoading && (
              <p className="text-sm text-slate-500" data-testid="safety-loading">Checking…</p>
            )}
            {safetyError && (
              <p className="text-sm text-red-600" data-testid="safety-error">{safetyError}</p>
            )}
            {safety && (
              <div>
                {unmeasured && (
                  <p className="text-sm text-slate-700 mb-3" role="status" data-testid="safety-no-evaluations">
                    {unmeasured.errors} {unmeasured.errors === 1 ? 'error' : 'errors'} reported, but no
                    evaluations reported in the last {unmeasured.minutes} minutes: the error rate cannot be
                    computed.
                  </p>
                )}
                {uncounted && (
                  <p className="text-sm text-slate-700 mb-3" role="status" data-testid="safety-uncounted-reports">
                    {uncounted.reports === 1
                      ? `1 error report in the last ${uncounted.minutes} minutes was sent with a key that does not count toward automatic rollback (no sdk:ruleset scope, or the key's owner can no longer change flags). It is stored, but does not count toward the error rate.`
                      : `${uncounted.reports} error reports in the last ${uncounted.minutes} minutes were sent with keys that do not count toward automatic rollback (no sdk:ruleset scope, or the key's owner can no longer change flags). They are stored, but do not count toward the error rate.`}
                  </p>
                )}
                {safety.metrics.length === 0 ? (
                  <p className="text-sm text-slate-500">No safety metrics recorded yet.</p>
                ) : (
                  <ul className="space-y-2" data-testid="safety-metrics">
                    {safety.metrics.map((m) => (
                      <li
                        key={m.name}
                        className="flex items-center justify-between gap-3 text-sm"
                        data-testid="safety-metric"
                      >
                        <span className="text-slate-800">{humanize(m.name)}</span>
                        <span className="flex items-center gap-2 shrink-0">
                          <span className="tabular-nums text-slate-700">
                            {formatSafetyMetricValue(m.name, m.current_value, m.unit)}{' '}
                            <span className="text-slate-500">/ {formatSafetyMetricValue(m.name, m.threshold, m.unit)}</span>
                          </span>
                          {unmeasured && RATE_METRICS.has(m.name) ? (
                            <span className="text-xs text-slate-700" data-testid="safety-metric-not-computed">
                              not computed
                            </span>
                          ) : (
                            <span
                              role="img"
                              className={`inline-block h-2 w-2 rounded-full ${m.is_healthy ? 'bg-green-500' : 'bg-red-500'}`}
                              aria-label={m.is_healthy ? 'healthy' : 'unhealthy'}
                            />
                          )}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
                <p className="text-xs text-slate-500 mt-3" data-testid="safety-last-checked">
                  Last checked {formatDateTime(safety.last_checked)}
                </p>
              </div>
            )}
          </section>
        </div>
        )}

        <div className="space-y-6">
          {/* Values (#1497): a string, number or JSON flag's two values */}
          {valueFlag && (
            <section
              className="bg-white rounded-lg border border-slate-200 p-6 space-y-4"
              data-testid="flag-values-section"
            >
              <div>
                <h2 className="text-base font-semibold text-slate-800">Values</h2>
                <p className="mt-2 text-sm text-slate-800" data-testid="flag-values-type">
                  Type: {typeLabel(valueType)}
                </p>
                <p className="text-sm text-slate-600">{TYPE_FIXED_ON_PAGE}</p>
              </div>
              {canChange && isValueFlagType(valueType) && production ? (
                <FlagValueFields
                  type={valueType}
                  texts={valueTexts}
                  errors={valueErrors}
                  onChange={handleValueChange}
                  fieldRefs={valueRefs}
                  hints={servesSplit ? { on_value: SPLIT_ON_VALUE_HINT } : undefined}
                />
              ) : (
                <div className="space-y-4">
                  {VALUE_FIELDS.map((field) => (
                    <FlagValueReadOnly key={field} field={field} value={storedValues[field]} />
                  ))}
                </div>
              )}
            </section>
          )}

          {/* The split (#2012): editable in every environment by a user who may change
              flags, read-only otherwise. A boolean flag has no Split section at all. */}
          {splitType && (
            <section
              className="bg-white rounded-lg border border-slate-200 p-6 space-y-4"
              aria-labelledby="flag-split-heading"
              data-testid="flag-split-section"
            >
              <h2 id="flag-split-heading" className="text-base font-semibold text-slate-800">
                {splitHeading(environmentLabel)}
              </h2>
              {canChange ? (
                <SplitEditor
                  key={splitLoads}
                  type={splitType}
                  rows={splitRows}
                  stored={loadedSplit ?? null}
                  errors={splitErrors}
                  onChange={(next) => {
                    setSplitRows(next);
                    setSplitTouched(true);
                    if (hasSplitErrors(splitErrors)) clearSplitProblems();
                  }}
                  onKeepStored={() => {
                    setSplitRows(loadedSplit ? rowsFromSplit(splitType, loadedSplit) : null);
                    setSplitTouched(false);
                    clearSplitProblems();
                  }}
                />
              ) : (
                <SplitReadOnly split={loadedSplit ?? null} />
              )}
            </section>
          )}

          {/* Targeting Rules */}
          <section
            ref={targetingSection}
            className="bg-white rounded-lg border border-slate-200 p-6"
            data-testid="flag-targeting-section"
          >
            {rules !== null ? (
              <>
                <TargetingRuleBuilder
                  value={rules}
                  onChange={setRules}
                  operatorOptions={FLAG_OPERATOR_OPTIONS}
                  readOnly={!canChange}
                  data-testid="targeting-rule-builder"
                />
                {splitType && (
                  <RuleServeControl
                    type={splitType}
                    hasSplit={splitRows !== null}
                    serve={serve}
                    hasCondition={rules.groups.length > 0}
                    error={serveError}
                    readOnly={!canChange}
                    storedValue={storedRuleValue(stored)}
                    onChange={(next) => {
                      setServe(next);
                      setServeTouched(true);
                      if (serveError) clearSplitProblems();
                    }}
                  />
                )}
                {replaced && (
                  <div
                    className="mt-3 flex flex-wrap items-center gap-3 text-sm text-amber-900"
                    data-testid="targeting-replacing"
                  >
                    <p>The stored rules are replaced when you save. Until then, nothing changes.</p>
                    <button
                      type="button"
                      onClick={() => loadRules(stored, valueType)}
                      className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50"
                      data-testid="targeting-keep-stored"
                    >
                      Keep the stored rules
                    </button>
                  </div>
                )}
              </>
            ) : (
              <div data-testid="targeting-raw">
                <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
                  <h3 className="text-sm font-semibold text-slate-700">Targeting Rules</h3>
                  {!confirmingReplace && canChange && (
                    <button
                      ref={replaceButton}
                      type="button"
                      onClick={() => setConfirmingReplace(true)}
                      className="px-3 py-1.5 rounded-md text-sm font-medium bg-white text-slate-700 border border-slate-300 hover:bg-slate-50"
                      data-testid="targeting-replace"
                    >
                      Replace rules
                    </button>
                  )}
                </div>
                {/* Rules the builder cannot show only because of a NOT group
                    are applied as stored, so they get their own note (#918);
                    every other shape keeps the general one. */}
                <p className="text-sm text-amber-900 mb-2" data-testid="targeting-raw-note">
                  {usesNotGroups(stored) ? NOT_GROUPS_NOTE : OUTSIDE_BUILDER_NOTE}{' '}
                  <a
                    href={docsUrl('api/endpoints', 'targeting-rules')}
                    className="text-blue-600 underline"
                    target="_blank"
                    rel="noreferrer"
                  >
                    Read about targeting rules
                  </a>
                  .
                </p>
                <pre
                  ref={storedRulesRegion}
                  role="region"
                  aria-label="Stored targeting rules"
                  tabIndex={0}
                  className="bg-slate-50 border border-slate-200 text-xs text-slate-800 rounded-md p-3 overflow-x-auto focus:outline-none focus:ring-2 focus:ring-blue-600"
                  data-testid="targeting-raw-json"
                >
                  {JSON.stringify(stored, null, 2)}
                </pre>
                {confirmingReplace && (
                  // Replace rules goes while this is open and comes back on
                  // Cancel, so focus returns through its ref.
                  <InlineConfirm
                    label="Replace rules"
                    describedBy={replaceTextId}
                    onCancel={() => setConfirmingReplace(false)}
                    returnFocus={replaceButton}
                    fallbackFocus={storedRulesRegion}
                    tone="warning"
                    className="mt-3 rounded-lg border p-3"
                    testId="targeting-replace-confirm"
                  >
                    <p id={replaceTextId} className="text-sm text-amber-900">
                      These rules will be discarded when you save new ones. Until you save, nothing changes.
                    </p>
                    <div className="mt-3 flex gap-2">
                      <button
                        type="button"
                        data-autofocus
                        onClick={() => setConfirmingReplace(false)}
                        className="px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 border border-slate-300 bg-white hover:bg-slate-50"
                        data-testid="targeting-replace-cancel"
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setConfirmingReplace(false);
                          setReplaced(true);
                          setRules(createEmptyRules());
                          setFocusAddGroup({});
                        }}
                        className="px-3 py-1.5 rounded-md text-sm font-medium bg-amber-700 text-white hover:bg-amber-800"
                        data-testid="targeting-replace-yes"
                      >
                        Start with no rules
                      </button>
                    </div>
                  </InlineConfirm>
                )}
              </div>
            )}
            {targetingError && <SaveAlert error={targetingError} />}
          </section>

          {/* Rollout Percentage */}
          <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-4">
            <h2 className="text-base font-semibold text-slate-800">
              {environmentLabel ? `Rollout in ${environmentLabel}` : 'Rollout'}
            </h2>
            <div>
              <div className="flex items-center justify-between mb-2">
                <label htmlFor="rollout-percentage" className="text-sm font-medium text-slate-700">
                  Rollout percentage
                </label>
                <span className="text-sm font-semibold text-slate-900" data-testid="rollout-value">
                  {rolloutPercentage}%
                </span>
              </div>
              <input
                id="rollout-percentage"
                name="rollout_percentage"
                type="range"
                min={0}
                max={100}
                value={rolloutPercentage}
                onChange={(e) => setRolloutPercentage(Number(e.target.value))}
                disabled={!canChange}
                aria-describedby={valueFlag ? 'rollout-value-hint' : undefined}
                className="w-full accent-blue-600"
                data-testid="rollout-percentage"
              />
              <div className="flex justify-between text-xs text-slate-500 mt-1">
                <span>0%</span>
                <span>100%</span>
              </div>
              {valueFlag && (
                <p id="rollout-value-hint" className="mt-2 text-sm text-slate-600" data-testid="rollout-value-hint">
                  {servesSplit ? SPLIT_ROLLOUT_HINT : ROLLOUT_VALUE_HINT}
                </p>
              )}
              {production && schedule?.status === 'active' && (
                <p className="mt-2 text-sm text-slate-600" data-testid="rollout-schedule-slider-note">
                  {SLIDER_SCHEDULE_NOTE}
                </p>
              )}
            </div>
          </section>

          <SplitErrorSummary items={summary} />
          {saveError && (
            <div
              ref={saveErrorRef}
              role="alert"
              tabIndex={-1}
              className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700 focus:outline-none focus:ring-2 focus:ring-blue-600"
              data-testid="save-error"
            >
              {saveError}
            </div>
          )}
          {saveSuccess && (
            <div
              role="status"
              className="rounded-lg bg-green-50 border border-green-200 p-3 text-sm text-green-700"
              data-testid="save-success"
            >
              Changes saved successfully.
            </div>
          )}

          {canChange && (
            <div className="flex justify-end">
              <button
                type="button"
                onClick={async () => {
                  // aria-disabled, not disabled: focus stays on the button while it saves.
                  if (saveLock.current) return;
                  saveLock.current = true;
                  try {
                    await handleSave();
                  } finally {
                    saveLock.current = false;
                  }
                }}
                aria-disabled={isSaving ? true : undefined}
                className="px-6 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 aria-disabled:opacity-50 aria-disabled:cursor-not-allowed transition-colors"
                data-testid="save-flag"
              >
                {isSaving ? 'Saving...' : environmentLabel ? `Save changes in ${environmentLabel}` : 'Save Changes'}
              </button>
            </div>
          )}

          {/* The same flag in every environment, side by side (loaded when opened). */}
          <EnvironmentComparison flagId={flag.id} refreshKey={changeCount} />
        </div>
      </div>
    </div>
  );
}
