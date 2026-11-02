/**
 * OfflineStorage against each AsyncStorage major the peer range allows
 * (`>=1.21.0 <4`).
 *
 * 1.x and 2.x share one default export, with `multiRemove`; the package's Jest
 * config maps the module to that shape (__mocks__/.../async-storage.ts). 3.x's
 * default export has `removeMany` and no `multiRemove`; the 3.x tests load the
 * SDK with the module mocked by __mocks__/@react-native-async-storage/v3.ts.
 * Before the SDK read both shapes, `clear()` (and so `client.clearStorage()`)
 * did nothing on 3.x: the missing `multiRemove` threw, and the best-effort
 * catch swallowed it.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import { OfflineStorage } from '../src/storage';
import type { FlagEvaluation } from '../src/types';

type StorageModule = typeof import('../src/storage');
type MockStorage = Record<string, jest.Mock>;

const FLAG: FlagEvaluation = { key: 'dark-mode', enabled: true, config: null };

/**
 * The SDK's storage module, loaded with AsyncStorage replaced by the 3.x-shaped
 * mock, and that mock: a fresh module registry, so nothing leaks into the
 * other tests.
 */
function withAsyncStorage3(): { storage: StorageModule; asyncStorage: MockStorage } {
  let storage: StorageModule | undefined;
  let asyncStorage: MockStorage | undefined;
  jest.isolateModules(() => {
    jest.doMock('@react-native-async-storage/async-storage', () =>
      jest.requireActual('../__mocks__/@react-native-async-storage/v3')
    );
    asyncStorage = require('@react-native-async-storage/async-storage').default;
    storage = require('../src/storage');
  });
  if (!storage || !asyncStorage) throw new Error('the 3.x module did not load');
  return { storage, asyncStorage };
}

beforeEach(async () => {
  await AsyncStorage.clear();
  for (const fn of Object.values(AsyncStorage)) {
    if (jest.isMockFunction(fn)) fn.mockClear();
  }
});

describe('the AsyncStorage mocks', () => {
  test("the 3.x mock has exactly the methods of 3.1.1's AsyncStorage interface", () => {
    const { asyncStorage } = withAsyncStorage3();
    // lib/typescript/AsyncStorage.d.ts in @react-native-async-storage/async-storage 3.1.1.
    expect(Object.keys(asyncStorage).sort()).toEqual([
      'clear',
      'getAllKeys',
      'getItem',
      'getMany',
      'removeItem',
      'removeMany',
      'setItem',
      'setMany',
    ]);
  });

  test('the default mock is the 1.x and 2.x shape, with multiRemove and no removeMany', () => {
    expect(Object.keys(AsyncStorage)).toContain('multiRemove');
    expect(Object.keys(AsyncStorage)).not.toContain('removeMany');
  });
});

describe('clear() on AsyncStorage 1.x and 2.x', () => {
  test('removes only the SDK keys, with multiRemove', async () => {
    await AsyncStorage.setItem('host-app-key', 'keep me');
    const storage = new OfflineStorage();
    await storage.setFlag('user-1', 'dark-mode', FLAG, 60_000);
    await storage.setAssignment(
      'user-1',
      'checkout-experiment',
      {
        experimentKey: 'checkout-experiment',
        userId: 'user-1',
        variantId: 'v2',
        variantName: 'treatment',
        isControl: false,
        configuration: null,
      },
      60_000
    );

    await storage.clear();

    expect(AsyncStorage.multiRemove).toHaveBeenCalledTimes(1);
    expect(await AsyncStorage.getAllKeys()).toEqual(['host-app-key']);
  });
});

describe('clear() on AsyncStorage 3.x', () => {
  test('regression: removes only the SDK keys, with removeMany', async () => {
    const { storage, asyncStorage } = withAsyncStorage3();
    await asyncStorage.clear();
    await asyncStorage.setItem('host-app-key', 'keep me');
    const offline = new storage.OfflineStorage();
    await offline.setFlag('user-1', 'dark-mode', FLAG, 60_000);
    await offline.setFlag('user-2', 'dark-mode', FLAG, 60_000);
    expect(await asyncStorage.getAllKeys()).toHaveLength(3);

    await offline.clear();

    expect(asyncStorage.removeMany).toHaveBeenCalledTimes(1);
    expect(asyncStorage.removeMany.mock.calls[0][0].sort()).toEqual([
      'ep_sdk_flag:user-1:dark-mode',
      'ep_sdk_flag:user-2:dark-mode',
    ]);
    expect(await asyncStorage.getAllKeys()).toEqual(['host-app-key']);
    expect(await offline.getFlag('user-1', 'dark-mode')).toBeNull();
  });

  test('reads and writes entries as on 1.x', async () => {
    const { storage, asyncStorage } = withAsyncStorage3();
    await asyncStorage.clear();
    const offline = new storage.OfflineStorage();

    await offline.setFlag('user-1', 'dark-mode', FLAG, 60_000);

    expect(asyncStorage.setItem).toHaveBeenCalledWith(
      'ep_sdk_flag:user-1:dark-mode',
      expect.any(String)
    );
    expect((await offline.getFlag('user-1', 'dark-mode'))?.value).toEqual(FLAG);
  });
});
