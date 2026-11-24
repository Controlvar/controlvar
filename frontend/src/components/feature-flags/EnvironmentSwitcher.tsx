import React, { useId, useState } from 'react';
import { useFlagEnvironment } from '@/contexts/FlagEnvironmentContext';
import {
  ENVIRONMENT_LABEL,
  FLAG_ENVIRONMENTS,
  FLAG_ENVIRONMENT_LABELS,
  PRODUCTION_MARKER,
  parseFlagEnvironment,
} from '@/utils/flagEnvironments';

/**
 * The environment switcher on the flag pages: a native select labelled
 * "Environment" (Development, Staging, Production), so it is keyboard
 * operable and its value is read out by assistive technology.
 *
 * Production carries a text marker beside it, not only a colour. After a
 * change a polite status says which environment the page now shows; the
 * status is empty until then.
 */
export function EnvironmentSwitcher() {
  const { environment, choose } = useFlagEnvironment();
  const id = useId();
  const [announcement, setAnnouncement] = useState('');

  if (!environment) return null;
  const production = environment === 'production';

  return (
    <div className="flex flex-wrap items-center justify-end gap-2" data-testid="environment-switcher">
      <label htmlFor={id} className="text-sm font-medium text-slate-700">
        {ENVIRONMENT_LABEL}
      </label>
      <select
        id={id}
        value={environment}
        onChange={(event) => {
          const next = parseFlagEnvironment(event.target.value);
          if (!next) return;
          choose(next);
          setAnnouncement(`Showing ${FLAG_ENVIRONMENT_LABELS[next]}.`);
        }}
        className="rounded-md border border-slate-300 bg-white py-1 pl-2 pr-8 text-sm text-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600"
        data-testid="environment-select"
      >
        {/* Development first: the order a change usually travels in. */}
        {[...FLAG_ENVIRONMENTS].reverse().map((env) => (
          <option key={env} value={env}>
            {FLAG_ENVIRONMENT_LABELS[env]}
          </option>
        ))}
      </select>
      {production && (
        <span
          className="inline-flex items-center gap-1 rounded-full border border-amber-700 bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-900"
          data-testid="production-marker"
        >
          <span aria-hidden="true">●</span>
          {PRODUCTION_MARKER}
        </span>
      )}
      <span role="status" className="sr-only" data-testid="environment-announcement">
        {announcement}
      </span>
    </div>
  );
}

export default EnvironmentSwitcher;
