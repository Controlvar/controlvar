/**
 * `createApiKey` (#2011): the dashboard's one way to create a key returns it
 * only if the API says it is of the environment asked for. Otherwise it
 * revokes the key and throws, and the key's value goes no further.
 */
import { ApiError, apiFetch, serverErrorMessage, timeoutMessage, unreachableMessage } from '@/services/api';
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
  'This happens while an earlier release of the API, which has no environments, is still running: create the key again once the API is upgraded.';

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

// Each error as `apiFetch` builds it, its message included: a timeout, an
// unreachable API and a 5xx with no JSON are whole sentences, which the reason
// in parentheses never is. Only a 403 says that the key is there.
it.each([
  ['403', new ApiError({ status: 403, detail: "You don't have permission to delete API keys" }), 'HTTP 403', 'still exists'],
  ['401', new ApiError({ status: 401, detail: 'Not authenticated' }), 'HTTP 401', 'may still exist'],
  ['404', new ApiError({ status: 404, detail: 'API key not found' }), 'HTTP 404', 'may still exist'],
  ['429', new ApiError({ status: 429, detail: 'Rate limit exceeded' }), 'HTTP 429', 'may still exist'],
  ['500', new ApiError({ status: 500, message: serverErrorMessage(500, 'req-1') }), 'HTTP 500', 'may still exist'],
  ['502', new ApiError({ status: 502, message: serverErrorMessage(502) }), 'HTTP 502', 'may still exist'],
  ['a 2xx that could not be read', new ApiError({ status: 200, message: 'Cut off.', unreadableAnswer: true }), 'HTTP 200', 'may still exist'],
  [
    'a timeout',
    new ApiError({ status: 0, message: timeoutMessage(30_000, true), timedOut: true, outcomeUnknown: true }),
    'no answer',
    'may still exist',
  ],
  ['an unreachable API', new ApiError({ status: 0, detail: unreachableMessage() }), 'no answer', 'may still exist'],
  ['something that is not an ApiError', new Error('Failed to fetch.'), 'no answer', 'may still exist'],
])('a revoke that fails with %s is reported in a few words', async (_, error, reason, exists) => {
  answer(ANSWER_OF_0_48_0, () => Promise.reject(error));
  const err = await refusal(STAGING);
  expect(err.message).toBe(
    `The API did not confirm that the new key is a Staging key, and revoking it failed (${reason}). ` +
      `The key “laptop” ${exists}: revoke it in the list on this page. ${NEXT}`,
  );
  expect(JSON.stringify(err) + err.message + String(err.stack)).not.toContain(SECRET);
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
