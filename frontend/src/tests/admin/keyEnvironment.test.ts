/**
 * `createApiKey` (#2011): the dashboard's one way to create a key returns it
 * only if the API says it is of the environment asked for. Otherwise it
 * revokes the key and throws, and the key's value goes no further.
 */
import { ApiError, apiFetch } from '@/services/api';
import {
  CreateKeyBody,
  KEY_ENVIRONMENTS,
  KeyEnvironmentMismatch,
  createApiKey,
  createKeyErrorMessage,
  keyEnvironmentLabel,
} from '@/components/admin/api-keys/keyEnvironment';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockApi = apiFetch as jest.MockedFunction<typeof apiFetch>;

const SECRET = 'eptk_' + 'a'.repeat(32);

/** Field for field what 0.48.0 answers a create with, whatever it was asked. */
const ANSWER_OF_0_48_0 = {
  id: 'k/1',
  name: 'laptop',
  key: SECRET,
  prefix: SECRET.slice(0, 9),
  created_at: '2026-11-24T10:00:00',
  expires_at: null,
};

const STAGING: CreateKeyBody = { name: 'laptop', environment: 'staging' };

const NEXT =
  'This happens while an earlier release of the API, which has no environments, is still running: create the key again when the upgrade has finished.';

/** Answer the POST with *created*, and the DELETE with *revoke*. */
function answer(created: unknown, revoke: () => Promise<unknown> = () => Promise.resolve(undefined)) {
  mockApi.mockImplementation((_path, options) =>
    (options?.method === 'DELETE' ? revoke() : Promise.resolve(created)) as ReturnType<typeof apiFetch>,
  );
}

function calls() {
  return mockApi.mock.calls.map(([path, options]) => `${options?.method} ${path}`);
}

async function refusal(body: CreateKeyBody): Promise<KeyEnvironmentMismatch> {
  try {
    await createApiKey(body);
  } catch (err) {
    if (err instanceof KeyEnvironmentMismatch) return err;
    throw err;
  }
  throw new Error('the key was handed over');
}

beforeEach(() => {
  mockApi.mockReset();
});

it.each(KEY_ENVIRONMENTS)('hands over a %s key that the API says is one, and revokes nothing', async (environment) => {
  const created = { ...ANSWER_OF_0_48_0, environment };
  answer(created);
  await expect(createApiKey({ name: 'laptop', environment })).resolves.toBe(created);
  expect(mockApi).toHaveBeenCalledTimes(1);
  expect(mockApi).toHaveBeenCalledWith('/api/v1/api-keys', {
    method: 'POST',
    json: { name: 'laptop', environment },
  });
});

it.each([
  ['absent (0.48.0)', ANSWER_OF_0_48_0],
  ['null', { ...ANSWER_OF_0_48_0, environment: null }],
  ['another environment', { ...ANSWER_OF_0_48_0, environment: 'production' }],
  ['not the exact name', { ...ANSWER_OF_0_48_0, environment: 'Staging' }],
])('revokes the key and refuses it when the answered environment is %s', async (_, created) => {
  answer(created);
  const err = await refusal(STAGING);
  expect(calls()).toEqual(['POST /api/v1/api-keys', 'DELETE /api/v1/api-keys/k%2F1']);
  expect(err.message).toBe(
    `The API did not confirm that the new key is a Staging key, so the key was revoked. ${NEXT}`,
  );
  expect(JSON.stringify(err) + err.message + String(err.stack)).not.toContain(SECRET);
});

it('an answer that names no environment is refused for a Production key too', async () => {
  answer(ANSWER_OF_0_48_0);
  const err = await refusal({ name: 'laptop', environment: 'production' });
  expect(calls()).toEqual(['POST /api/v1/api-keys', 'DELETE /api/v1/api-keys/k%2F1']);
  expect(err.message).toBe(
    `The API did not confirm that the new key is a Production key, so the key was revoked. ${NEXT}`,
  );
});

it('when the revoke fails, says why, that the key still exists, and where to revoke it', async () => {
  answer(ANSWER_OF_0_48_0, () =>
    Promise.reject(new ApiError({ status: 500, detail: 'Internal server error' })),
  );
  const err = await refusal(STAGING);
  expect(err.message).toBe(
    'The API did not confirm that the new key is a Staging key, and revoking it failed (Internal server error). ' +
      `The key “laptop” still exists: revoke it in the list on this page. ${NEXT}`,
  );
  expect(err.message).not.toContain(SECRET);
});

it('when the revoke timed out, says the key may still exist', async () => {
  answer(ANSWER_OF_0_48_0, () =>
    Promise.reject(
      new ApiError({ status: 0, detail: 'No answer.', message: 'No answer.', timedOut: true, outcomeUnknown: true }),
    ),
  );
  const err = await refusal(STAGING);
  expect(err.message).toContain('The key “laptop” may still exist: revoke it in the list on this page.');
});

it('a create the API refused is not a mismatch, and revokes nothing', async () => {
  const refused = new ApiError({ status: 422, detail: 'no' });
  mockApi.mockRejectedValue(refused);
  await expect(createApiKey(STAGING)).rejects.toBe(refused);
  expect(mockApi).toHaveBeenCalledTimes(1);
});

it('shows a refused scope as its sentence, and any other failure as it came', () => {
  const sentence =
    'mcp:read needs a production key: the routes it opens read production only. Create the key for production, or without mcp:read.';
  const refused = new ApiError({
    status: 422,
    detail: [{ type: 'value_error', loc: ['body', 'scopes'], msg: `Value error, ${sentence}`, input: ['mcp:read'] }],
  });
  expect(createKeyErrorMessage(refused, 'fallback')).toBe(sentence);
  expect(createKeyErrorMessage(new Error('Key name already exists'), 'fallback')).toBe('Key name already exists');
  expect(createKeyErrorMessage(new Error(''), 'fallback')).toBe('fallback');
  expect(createKeyErrorMessage('not an error', 'fallback')).toBe('fallback');
});

it.each([
  ['production', 'Production'],
  ['staging', 'Staging'],
  ['development', 'Development'],
  ['canary', 'canary'],
  [undefined, '—'],
  [null, '—'],
])('lists %s as %s', (environment, label) => {
  expect(keyEnvironmentLabel(environment)).toBe(label);
});
