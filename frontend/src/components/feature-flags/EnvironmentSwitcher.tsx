import React, { useEffect, useId, useRef, useState } from 'react';
import { FlagEnvironmentValue, useFlagEnvironment } from '@/contexts/FlagEnvironmentContext';
import {
  ENVIRONMENT_LABEL,
  FLAG_ENVIRONMENTS,
  FLAG_ENVIRONMENT_LABELS,
  FlagEnvironment,
  PRODUCTION_MARKER,
  parseFlagEnvironment,
} from '@/utils/flagEnvironments';

/**
 * What the status says: the environment on screen, once the page has loaded it
 * and the URL names it. It follows what is on screen, not the select, so Back
 * and Forward are said too, and it never names another environment than the
 * select does. It is empty for the first environment shown (nothing changed),
 * for a new one until it has loaded, and after a load that failed. Once said,
 * it stays beside a later load of the same environment, and is not said again.
 */
function useAnnouncement({
  environment,
  shown,
  routing,
}: Pick<FlagEnvironmentValue, 'environment' | 'shown' | 'routing'>): string {
  const [said, setSaid] = useState<FlagEnvironment | null>(null);
  // The environment the user knows is on screen: undefined before the first
  // has loaded, null once another is chosen or a load has failed.
  const known = useRef<FlagEnvironment | null | undefined>(undefined);
  const last = useRef<FlagEnvironment | null>(null);

  useEffect(() => {
    if (!environment) return;
    if (last.current !== null && last.current !== environment) {
      known.current = null;
      setSaid(null);
    }
    last.current = environment;
    if (!shown || shown.environment !== environment || routing) return;
    if (!shown.loaded) {
      known.current = null;
      setSaid(null);
    } else if (known.current === undefined) {
      known.current = environment;
    } else if (known.current !== environment) {
      known.current = environment;
      setSaid(environment);
    }
  }, [environment, shown, routing]);

  return said !== null && said === environment ? `Showing ${FLAG_ENVIRONMENT_LABELS[said]}.` : '';
}

/**
 * The environment switcher on the flag pages: a native select labelled
 * "Environment" (Development, Staging, Production), so it is keyboard
 * operable and its value is read out by assistive technology.
 *
 * Production carries a text marker beside it, not only a colour. A polite
 * status says which environment the page shows after it changes.
 */
export function EnvironmentSwitcher() {
  const { environment, choose, shown, routing } = useFlagEnvironment();
  const id = useId();
  const announcement = useAnnouncement({ environment, shown, routing });

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
          if (next) choose(next);
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
