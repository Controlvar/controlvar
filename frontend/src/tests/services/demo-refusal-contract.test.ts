/**
 * The read-only demo's refusal, end to end in the dashboard: the body in
 * `fixtures/demo-read-only-refusal.json` (403
 * `{"detail": {"code": "demo_read_only", "message": ...}}`) goes through
 * `apiFetch` and `ApiError` exactly as a browser would receive it, and every
 * page whose failures are fixed copy by status then shows the API's sentence,
 * never its own "Your role cannot ..." for a 403.
 *
 * The code is read from `detail.code`. A body with the code beside `detail`
 * instead is not the contract, and a block below shows the dashboard does
 * not take it for one.
 *
 * The last block finds every file that maps a 403 to copy of its own, so a
 * map added later cannot skip the check: each one asks `demoRefusalMessage`,
 * or is listed with the reason it need not.
 */
import fs from 'fs';
import refusal from '@/tests/fixtures/demo-read-only-refusal.json';
import { MODULES_SRC_ROOT, SRC_ROOT, allMatches, listSourceFiles, sourceLabel, stripComments } from '../helpers/api-literals';
import { ApiError, apiFetch } from '@/services/api';
import {
  DEMO_READ_ONLY_CODE,
  DEMO_READ_ONLY_MESSAGE,
  demoRefusalMessage,
  isDemoRefusal,
} from '@/utils/demoMode';
import { ROLE_NOTE, SegmentAction, segmentErrorCopy } from '@/utils/segmentErrors';
import { describeCreateError, ROLE_CANNOT_CREATE } from '@/components/experiments/new/createErrors';
import { holdoutErrorCopy } from '@/utils/holdouts';
import { GroupAction, groupErrorCopy } from '@/utils/exclusionGroups';
import { ERROR_COPY, ManageAction, errorCopy } from '@/components/experiments/ExperimentManageSection';
import { SCHEDULE_ERROR_COPY, scheduleErrorCopy } from '@/components/experiments/ExperimentScheduleSection';
import { ROLLOUT_ERROR_COPY, rolloutErrorCopy, settingsErrorCopy } from '@/components/feature-flags/RolloutScheduleManager';
import {
  FLAG_MANAGE_ERROR_COPY,
  FlagManageAction,
  flagManageErrorCopy,
} from '@/components/feature-flags/FlagManageSection';
import { classifyPasswordError, passwordOutcomeSentence } from '@/services/password';
import { FORBIDDEN_MESSAGE, editErrorMessage } from '@/components/admin/users/EditUserModal';

const mockFetch = jest.fn();
global.fetch = mockFetch;

function answer(status: number, body: unknown): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'Forbidden',
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: () => Promise.resolve(JSON.parse(text)),
    text: () => Promise.resolve(text),
  } as unknown as Response;
}

/** The ApiError a write gets when the API answers `body` with `status`. */
async function refusedWith(status: number, body: unknown): Promise<ApiError> {
  mockFetch.mockResolvedValueOnce(answer(status, body));
  const err = await apiFetch('/api/v1/experiments', { method: 'POST', json: { name: 'x' } }).then(
    () => {
      throw new Error('the write was not refused');
    },
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(ApiError);
  return err as ApiError;
}

const SENTENCE = refusal.body.detail.message;

const SEGMENT_ACTIONS: SegmentAction[] = [
  'load',
  'load-segment',
  'create',
  'save-rules',
  'archive',
  'members',
  'check',
  'preview',
  'usage',
];
const GROUP_ACTIONS: GroupAction[] = ['list', 'load', 'save', 'archive', 'add', 'remove'];
const MANAGE_ACTIONS = Object.keys(ERROR_COPY) as ManageAction[];
const FLAG_MANAGE_ACTIONS = Object.keys(FLAG_MANAGE_ERROR_COPY) as FlagManageAction[];

/** Every fixed-copy map, as `[name, copy for an error]`. */
const MAPS: Array<[string, (err: unknown) => string]> = [
  ...SEGMENT_ACTIONS.map((a): [string, (err: unknown) => string] => [`segmentErrorCopy(${a})`, (e) => segmentErrorCopy(e, a)]),
  ['describeCreateError', (e) => describeCreateError(e, 'key', 'advanced').message],
  ['holdoutErrorCopy', (e) => holdoutErrorCopy(e)],
  ...GROUP_ACTIONS.map((a): [string, (err: unknown) => string] => [`groupErrorCopy(${a})`, (e) => groupErrorCopy(a, e)]),
  ...MANAGE_ACTIONS.map((a): [string, (err: unknown) => string] => [`errorCopy(${a})`, (e) => errorCopy(a, e)]),
  ['scheduleErrorCopy', scheduleErrorCopy],
  ['rolloutErrorCopy', rolloutErrorCopy],
  ['settingsErrorCopy', settingsErrorCopy],
  ...FLAG_MANAGE_ACTIONS.map((a): [string, (err: unknown) => string] => [
    `flagManageErrorCopy(${a})`,
    (e) => flagManageErrorCopy(a, e),
  ]),
  ['passwordOutcomeSentence', (e) => passwordOutcomeSentence(classifyPasswordError(e))],
  ['editErrorMessage', editErrorMessage],
];

beforeEach(() => {
  mockFetch.mockReset();
});

describe('the demo refusal, as the API sends it (fixtures/demo-read-only-refusal.json)', () => {
  it('is the contract: a 403 whose detail carries the code and the sentence', () => {
    expect(refusal.status).toBe(403);
    expect(refusal.body.detail.code).toBe(DEMO_READ_ONLY_CODE);
    // The dashboard's fallback for a refusal with no sentence is the same sentence.
    expect(SENTENCE).toBe(DEMO_READ_ONLY_MESSAGE);
  });

  it('reaches ApiError with its code and the API sentence as the message', async () => {
    const err = await refusedWith(refusal.status, refusal.body);
    expect(err.status).toBe(403);
    expect(err.code).toBe(DEMO_READ_ONLY_CODE);
    expect(err.message).toBe(SENTENCE);
    expect(isDemoRefusal(err)).toBe(true);
    expect(demoRefusalMessage(err)).toBe(SENTENCE);
  });

  it.each(MAPS)('%s shows the API sentence', async (_name, copy) => {
    const err = await refusedWith(refusal.status, refusal.body);
    expect(copy(err)).toBe(SENTENCE);
  });
});

describe('a role refusal (control): the same maps keep their own copy', () => {
  const roleRefusal = { detail: 'Not enough permissions' };

  it('is not a demo refusal', async () => {
    const err = await refusedWith(403, roleRefusal);
    expect(err.code).toBeUndefined();
    expect(isDemoRefusal(err)).toBe(false);
    expect(demoRefusalMessage(err)).toBeNull();
  });

  it.each(MAPS)('%s does not show the demo sentence', async (_name, copy) => {
    const err = await refusedWith(403, roleRefusal);
    expect(copy(err)).not.toBe(SENTENCE);
  });

  it('keeps each map its 403 copy', async () => {
    const err = await refusedWith(403, roleRefusal);
    expect(segmentErrorCopy(err, 'create')).toContain(ROLE_NOTE);
    expect(describeCreateError(err, 'key', 'advanced').message).toContain(ROLE_CANNOT_CREATE);
    expect(errorCopy('edit', err)).toBe(ERROR_COPY.edit[403]);
    expect(scheduleErrorCopy(err)).toBe(SCHEDULE_ERROR_COPY[403]);
    expect(rolloutErrorCopy(err)).toBe(ROLLOUT_ERROR_COPY[403]);
    expect(flagManageErrorCopy('archive', err)).toBe(FLAG_MANAGE_ERROR_COPY.archive[403]);
    expect(editErrorMessage(err)).toBe(FORBIDDEN_MESSAGE);
  });

  it('is not a demo refusal on another status, even with the code', async () => {
    const err = await refusedWith(409, refusal.body);
    expect(err.code).toBe(DEMO_READ_ONLY_CODE);
    expect(isDemoRefusal(err)).toBe(false);
  });
});

describe('the code beside detail instead of inside it is not the contract', () => {
  it('leaves ApiError without a code, so nothing reads it as a demo refusal', async () => {
    const flat = { detail: SENTENCE, code: DEMO_READ_ONLY_CODE };
    const err = await refusedWith(403, flat);
    expect(err.code).toBeUndefined();
    expect(isDemoRefusal(err)).toBe(false);
  });
});

/** A 403 given a meaning in code: a `403:` key, a comparison with 403, `case 403:` or `isForbidden`. */
const FORBIDDEN_PATTERNS = [/\b403\s*:/, /\bstatus\s*===?\s*403\b/, /\bcase\s+403\s*:/, /\.isForbidden\b/];

/**
 * The files that handle a 403 and need not ask `demoRefusalMessage`, each
 * with why. Every other file the finder sees must ask it.
 */
const NEED_NOT_ASK: Record<string, string> = {
  'services/api.ts': 'defines ApiError.isForbidden',
  'utils/demoMode.ts': 'is the check',
  'components/results/ResultsDashboard/ResultsDashboard.tsx': 'a results load (GET), which demo mode never refuses',
  'hooks/useExperimentEligibility.ts': 'eligibility loads (GET)',
  'pages/experiments/holdouts.tsx': 'the holdout load (GET); its saves use holdoutErrorCopy',
  'pages/auth/callback.tsx': 'GET /users/me after the Hosted UI sign-in',
  'modules/frontend/src/components/warehouse/runs/WarehouseAnalysisSection.tsx':
    "a runs load (GET), shown in the API's own words",
};

describe('every file that maps a 403 to its own copy asks for the demo refusal first', () => {
  const files = [...listSourceFiles(SRC_ROOT), ...listSourceFiles(MODULES_SRC_ROOT)];
  const handling = files
    .filter((file) => {
      const source = stripComments(fs.readFileSync(file, 'utf8'));
      return FORBIDDEN_PATTERNS.some((re) => allMatches(re, source).length > 0);
    })
    .map((file) => ({ label: sourceLabel(file), source: stripComments(fs.readFileSync(file, 'utf8')) }));

  it('finds the maps (the control is the files, not a number)', () => {
    const labels = handling.map((h) => h.label);
    for (const label of [
      'utils/segmentErrors.ts',
      'components/experiments/new/createErrors.ts',
      'components/feature-flags/FlagManageSection.tsx',
      'components/feature-flags/RolloutScheduleManager.tsx',
      'services/password.ts',
    ]) {
      expect(labels).toContain(label);
    }
    // Each exemption is still a file that handles a 403, or it is stale.
    const present = Object.keys(NEED_NOT_ASK).filter(
      (label) => !label.startsWith('modules/') || fs.existsSync(MODULES_SRC_ROOT),
    );
    expect(present.filter((label) => !labels.includes(label))).toEqual([]);
  });

  it('every one not exempted calls demoRefusalMessage', () => {
    const missing = handling
      .filter((h) => !(h.label in NEED_NOT_ASK))
      .filter((h) => !/\bdemoRefusalMessage\s*\(/.test(h.source))
      .map((h) => h.label);
    expect(missing).toEqual([]);
  });
});
