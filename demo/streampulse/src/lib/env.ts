// Each setting is read under its name, then under the name it had before the
// project was renamed (an existing .env.local), then defaults. Each read is a
// literal `process.env.NAME`: Next.js inlines only that form into the bundle.
function stripTrailingSlash(url: string): string {
  return url.replace(/\/+$/, '');
}

/** Controlvar API origin. The SDK appends `/api/v1` to every request. */
export const API_URL = stripTrailingSlash(process.env.NEXT_PUBLIC_CONTROLVAR_API_URL || process.env.NEXT_PUBLIC_EXPERIMENTLY_API_URL || 'http://localhost:8000');

/** Plaintext API key (`streampulse-app`, owned by admin@demo.com). Written by the seed script. */
export const API_KEY = process.env.NEXT_PUBLIC_CONTROLVAR_API_KEY || process.env.NEXT_PUBLIC_EXPERIMENTLY_API_KEY || '';

/** Controlvar dashboard, used by the Controlvar panel's links. */
export const DASHBOARD_URL = stripTrailingSlash(
  process.env.NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL || process.env.NEXT_PUBLIC_EXPERIMENTLY_DASHBOARD_URL || 'http://localhost:3100',
);

/**
 * Flag results are cached per device for this long, so a flip in the platform
 * dashboard (kill switch, rollback, rule change) shows up within seconds.
 */
export const CACHE_TTL_MS = 5000;

/** Feature flag keys — must match backend/scripts/seed_streampulse.py exactly. */
export const FLAG_KEYS = {
  recsV2: 'streampulse_recs_v2',
  playerV2: 'streampulse_player_v2',
  aiSearch: 'streampulse_ai_search',
  offlineMode: 'streampulse_offline_mode',
} as const;

/** Experiment keys — must match the seed script exactly. */
export const EXPERIMENT_KEYS = {
  pushFrequency: 'streampulse_push_frequency',
  wrapped: 'streampulse_wrapped',
  profileBadges: 'streampulse_profile_badges',
  onboardingSteps: 'streampulse_onboarding_steps',
  upsellModal: 'streampulse_upsell_modal',
} as const;

/** Mutual exclusion group holding the two Profile experiments (seeded name). */
export const PROFILE_MEG_NAME = 'streampulse-profile';

/** Global holdout name (seeded). */
export const HOLDOUT_NAME = 'streampulse-holdout';

export const ALL_FLAG_KEYS: string[] = Object.values(FLAG_KEYS);
export const ALL_EXPERIMENT_KEYS: string[] = Object.values(EXPERIMENT_KEYS);
