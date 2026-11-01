import React, { useState } from 'react';
import { useRouter } from 'next/router';
import Link from 'next/link';
import { TargetingRuleBuilder } from '@/components/targeting';
import { TargetingRules } from '@/types/targeting';
import { FLAG_OPERATOR_OPTIONS, createEmptyRules } from '@/utils/targeting';
import { SaveAlert } from '@/components/experiments/TargetingSection';
import { checkTargeting } from '@/components/experiments/new/formState';
import {
  TARGETING_SAVE_INCOMPLETE,
  TargetingSaveError,
  describeTargetingSaveError,
} from '@/utils/experimentTargeting';
import { targetingToSend } from '@/utils/flagTargeting';
import { CreateFeatureFlagRequest, FeatureFlagsService } from '@/services/featureFlags';
import { PageTitle } from '@/components/PageTitle';
import { FlagTypeChoice, FlagValueFields, useFlagValueFields } from '@/components/feature-flags/FlagValueFields';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { FLAG_ROLE_NOTE, canChangeFeatureFlags } from '@/utils/experimentPermissions';
import {
  FlagValueType,
  ROLLOUT_VALUE_HINT,
  checkNewValues,
  firstInvalid,
  isValueFlagType,
  valueRefusals,
} from '@/utils/flagValues';

function generateKey(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .substring(0, 64);
}

export default function NewFeatureFlagPage() {
  const router = useRouter();
  const auth = useOptionalAuth();
  const [name, setName] = useState('');
  const [key, setKey] = useState('');
  const [keyEdited, setKeyEdited] = useState(false);
  const [description, setDescription] = useState('');
  const [rolloutPercentage, setRolloutPercentage] = useState(0);
  const [rules, setRules] = useState<TargetingRules>(createEmptyRules());
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [targetingError, setTargetingError] = useState<TargetingSaveError | null>(null);
  // What the flag serves (#1497). The typed text is kept while the user moves
  // between String, Number and JSON, and checked against the type chosen when
  // the form is sent; Boolean removes the two fields and sends neither.
  const [valueType, setValueType] = useState<FlagValueType>('boolean');
  const {
    texts: valueTexts,
    errors: valueErrors,
    setErrors: setValueErrors,
    showErrors: showValueErrors,
    change: handleValueChange,
    fieldRefs: valueRefs,
  } = useFlagValueFields();

  const handleTypeChange = (type: FlagValueType) => {
    setValueType(type);
    setValueErrors({});
  };

  const handleNameChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newName = e.target.value;
    setName(newName);
    if (!keyEdited) {
      setKey(generateKey(newName));
    }
  };

  const handleKeyChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setKey(e.target.value);
    setKeyEdited(true);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setTargetingError(null);
    setValueErrors({});

    // The values come first, as on the page. Only what is needed to send what
    // was typed is checked here; the API checks the rest and its 422 is shown
    // at the field it names.
    let values: { on_value?: unknown; off_value?: unknown } = {};
    if (isValueFlagType(valueType)) {
      const checked = checkNewValues(valueType, valueTexts);
      if (firstInvalid(checked.errors)) {
        showValueErrors(checked.errors);
        return;
      }
      values = checked.values;
    }

    const problems = checkTargeting(rules);
    if (problems.length > 0) {
      setTargetingError({ message: TARGETING_SAVE_INCOMPLETE, problems });
      return;
    }

    const body: CreateFeatureFlagRequest = {
      name,
      key: key || generateKey(name),
      description: description || undefined,
      // `FeatureFlagCreate.is_active` defaults to true server-side; new flags
      // start off so turning them on is an explicit, audited action.
      is_active: false,
      rollout_percentage: rolloutPercentage,
    };
    // A boolean flag's body is what it was before flags had values: no
    // value_type, no on_value, no off_value.
    if (isValueFlagType(valueType)) {
      body.value_type = valueType;
      body.on_value = values.on_value;
      body.off_value = values.off_value;
    }
    // A new flag has no stored rules: rules are sent only when the builder has some.
    const targeting = targetingToSend(rules, null, false);
    if (targeting !== undefined) body.targeting_rules = targeting;

    setIsSubmitting(true);
    try {
      const created = await FeatureFlagsService.create(body);
      router.push(`/feature-flags/${created.id}`);
    } catch (err) {
      const refused = valueRefusals(err);
      if (refused) showValueErrors(refused.fields);
      const other = refused ? refused.rest : err;
      if (other) {
        const described = describeTargetingSaveError(other);
        if (described.problems) {
          setTargetingError(described);
        } else {
          setError(other instanceof Error ? other.message : 'Failed to create feature flag');
        }
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  // The page asks the API's own create question (#917): an ANALYST or VIEWER
  // sees a notice instead of a form that could only earn a 403, and no
  // request can be sent from it.
  if (!canChangeFeatureFlags(auth?.user)) {
    return (
      <div className="flex-1 bg-slate-50">
        <PageTitle title="New Feature Flag" />
        <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          <h1 className="text-2xl font-bold text-slate-900">New Feature Flag</h1>
          <p className="mt-4 text-sm text-slate-700" data-testid="flag-new-role-note">
            Your role can view feature flags but not create them. {FLAG_ROLE_NOTE}
          </p>
          <Link href="/feature-flags" className="mt-4 inline-block text-sm text-blue-700 underline">
            Back to feature flags
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="New Feature Flag" />
      <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Header */}
        <div className="flex items-center gap-4 mb-6">
          <Link href="/feature-flags" className="text-slate-500 hover:text-slate-700 text-sm">
            &larr; Feature Flags
          </Link>
          <h1 className="text-2xl font-bold text-slate-900">New Feature Flag</h1>
        </div>

        <form onSubmit={handleSubmit} className="space-y-6">
          {/* Basic info */}
          <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-4">
            <h2 className="text-base font-semibold text-slate-800">Basic Information</h2>

            <div>
              <label htmlFor="flag-name" className="block text-sm font-medium text-slate-700 mb-1">
                Name <span className="text-red-500">*</span>
              </label>
              <input
                id="flag-name"
                type="text"
                required
                value={name}
                onChange={handleNameChange}
                placeholder="My Feature Flag"
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
                data-testid="flag-name-input"
              />
            </div>

            <div>
              <label htmlFor="flag-key" className="block text-sm font-medium text-slate-700 mb-1">
                Key <span className="text-red-500">*</span>
              </label>
              <input
                id="flag-key"
                type="text"
                required
                value={key}
                onChange={handleKeyChange}
                placeholder="my_feature_flag"
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-400"
                data-testid="flag-key-input"
              />
              <p className="text-xs text-slate-600 mt-1">
                Auto-generated from name. Used in SDK calls.
              </p>
            </div>

            <div>
              <label htmlFor="flag-description" className="block text-sm font-medium text-slate-700 mb-1">
                Description
              </label>
              <textarea
                id="flag-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={2}
                placeholder="What does this feature flag control?"
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 resize-none"
              />
            </div>
          </section>

          {/* Values (#1497): Boolean by default, and then exactly as before */}
          <section
            className="bg-white rounded-lg border border-slate-200 p-6 space-y-4"
            data-testid="flag-values-section"
          >
            <h2 className="text-base font-semibold text-slate-800">Values</h2>
            <FlagTypeChoice value={valueType} onChange={handleTypeChange} />
            {isValueFlagType(valueType) && (
              <FlagValueFields
                type={valueType}
                texts={valueTexts}
                errors={valueErrors}
                onChange={handleValueChange}
                fieldRefs={valueRefs}
              />
            )}
          </section>

          {/* Targeting Rules */}
          <section className="bg-white rounded-lg border border-slate-200 p-6" data-testid="flag-targeting-section">
            <TargetingRuleBuilder value={rules} onChange={setRules} operatorOptions={FLAG_OPERATOR_OPTIONS} />
            {targetingError && <SaveAlert error={targetingError} />}
          </section>

          {/* Rollout */}
          <section className="bg-white rounded-lg border border-slate-200 p-6 space-y-4">
            <h2 className="text-base font-semibold text-slate-800">Rollout</h2>
            <div>
              <div className="flex items-center justify-between mb-2">
                <label htmlFor="rollout-percentage" className="text-sm font-medium text-slate-700">
                  Rollout percentage
                </label>
                <span className="text-sm font-semibold text-slate-900">
                  {rolloutPercentage}%
                </span>
              </div>
              <input
                id="rollout-percentage"
                type="range"
                min={0}
                max={100}
                value={rolloutPercentage}
                onChange={(e) => setRolloutPercentage(Number(e.target.value))}
                aria-describedby={isValueFlagType(valueType) ? 'rollout-value-hint' : undefined}
                className="w-full accent-blue-600"
                data-testid="rollout-percentage"
              />
              <div className="flex justify-between text-xs text-slate-600 mt-1">
                <span>0%</span>
                <span>100%</span>
              </div>
              {isValueFlagType(valueType) && (
                <p id="rollout-value-hint" className="mt-2 text-sm text-slate-600" data-testid="rollout-value-hint">
                  {ROLLOUT_VALUE_HINT}
                </p>
              )}
            </div>
          </section>

          {/* Error message */}
          {error && (
            <div
              role="alert"
              className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700"
              data-testid="create-error"
            >
              {error}
            </div>
          )}

          {/* Submit */}
          <div className="flex gap-3 justify-end">
            <Link
              href="/feature-flags"
              className="px-4 py-2 rounded-lg border border-slate-300 text-sm font-medium text-slate-600 hover:bg-slate-50 transition-colors"
            >
              Cancel
            </Link>
            <button
              type="submit"
              disabled={isSubmitting}
              className="px-6 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              data-testid="submit-flag"
            >
              {isSubmitting ? 'Creating...' : 'Create Feature Flag'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
