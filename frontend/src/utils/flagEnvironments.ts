/**
 * The flag environments the dashboard shows: Production, Staging and
 * Development, in one install.
 *
 * The names are the API's, exactly these lowercase strings. Production is a
 * flag's own row (`/api/v1/feature-flags/{id}`); Staging and Development are
 * per-environment settings read and written through the beta environment
 * routes. The dashboard never changes Production through those routes: a
 * Production change keeps using the stable flag routes, so the pages that
 * edit a flag choose the route by the environment on screen.
 */

export const FLAG_ENVIRONMENTS = ['production', 'staging', 'development'] as const;

export type FlagEnvironment = (typeof FLAG_ENVIRONMENTS)[number];

/** Staging and Development: the environments the beta routes write. */
export type NonProductionEnvironment = Exclude<FlagEnvironment, 'production'>;

/** What the switcher shows when nothing else is chosen. */
export const DEFAULT_FLAG_ENVIRONMENT: FlagEnvironment = 'production';

export const FLAG_ENVIRONMENT_LABELS: Record<FlagEnvironment, string> = {
  production: 'Production',
  staging: 'Staging',
  development: 'Development',
};

/** The query parameter that names the environment in a flag page's URL. */
export const FLAG_ENVIRONMENT_QUERY = 'env';

/**
 * The pages the environment applies to, as `router.pathname` patterns: the
 * flag list and a flag's page. The switcher is shown on these only.
 */
export const FLAG_ENVIRONMENT_PAGES: readonly string[] = ['/feature-flags', '/feature-flags/[id]'];

export function isFlagEnvironment(value: unknown): value is FlagEnvironment {
  return typeof value === 'string' && (FLAG_ENVIRONMENTS as readonly string[]).includes(value);
}

export function isNonProductionEnvironment(value: unknown): value is NonProductionEnvironment {
  return isFlagEnvironment(value) && value !== 'production';
}

/**
 * The environment a `?env=` value names, or null. Only the exact lowercase
 * names count; anything else (`prod`, `Staging`, a repeated parameter) is
 * ignored, never mapped to the nearest name.
 */
export function parseFlagEnvironment(value: unknown): FlagEnvironment | null {
  return isFlagEnvironment(value) ? value : null;
}

/**
 * *path* (a flag page) with the environment on screen in its URL, so a reload, a
 * new tab or a copied link opens the same one. Null (nothing to choose, or not
 * resolved yet) leaves the path as it is.
 */
export function inEnvironmentUrl(path: string, environment: FlagEnvironment | null): string {
  return environment ? `${path}?${FLAG_ENVIRONMENT_QUERY}=${environment}` : path;
}

export function flagEnvironmentApplies(pathname: string): boolean {
  return FLAG_ENVIRONMENT_PAGES.includes(pathname);
}

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

/** The switcher's label. */
export const ENVIRONMENT_LABEL = 'Environment';

/** Beside the switcher while Production is chosen: the marker is text, not only a colour. */
export const PRODUCTION_MARKER = 'Live';

/** On a flag page and the flag list in Production, for a user who may change flags. */
export const PRODUCTION_BANNER = 'You are editing Production. Changes affect live users.';

/** The heading of that section. */
export const PRODUCTION_ONLY_HEADING = 'What runs in Production only';

/** In place of the rollout schedule and the safety check outside Production. */
export const PRODUCTION_ONLY_NOTE = 'Rollout schedules, safety monitoring and experiments run in Production only.';

/** Outside Production: what the flag's page does not change there. */
export const SHARED_SETTINGS_NOTE =
  "A flag's values, and archiving or deleting it, apply in every environment, so they are done in Production only.";

/**
 * The same for a string, number or json flag, whose split (#2012) is each
 * environment's own: what applies in every environment is its value type, on
 * value and off value.
 */
export const SHARED_VALUE_SETTINGS_NOTE =
  "A flag's value type, on value and off value, and archiving or deleting it, apply in every environment, so they are done in Production only. Its split is each environment's own.";

/** On the flag list outside Production, where "+ New Flag" would be. */
export const NEW_FLAGS_IN_PRODUCTION = 'New flags are created in Production.';

/** Beside "Off" for a flag with no setting in this environment. */
export function notConfiguredText(environment: NonProductionEnvironment): string {
  return `Not configured in ${FLAG_ENVIRONMENT_LABELS[environment]}`;
}

/** The flag list outside Production, when it holds every flag and none has a setting there. */
export function noFlagsConfiguredText(environment: NonProductionEnvironment): string {
  return environment === 'staging'
    ? 'No flags configured in Staging yet. Promote a flag from Development, or configure one here.'
    : 'No flags configured in Development yet. Configure one here.';
}
