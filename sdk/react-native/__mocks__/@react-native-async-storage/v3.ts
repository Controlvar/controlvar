/**
 * An in-memory stand-in for the default export of
 * @react-native-async-storage/async-storage 3.x, for the tests that run the
 * SDK against that major (__tests__/storage.test.ts mocks the module with this
 * file for those tests only; every other test gets the 1.x-shaped mock beside
 * it).
 *
 * Its methods are exactly those of the `AsyncStorage` interface in 3.1.1's
 * lib/typescript/AsyncStorage.d.ts: there is no `multiRemove` (3.0 replaced
 * it with `removeMany`), and nothing else. storage.test.ts pins that set.
 *
 * The file name must not contain `@react-native-async-storage/async-storage`:
 * the package's Jest `moduleNameMapper` is that unanchored pattern, so it
 * would map a path such as `.../async-storage-v3` to the 1.x mock.
 */
const store: Record<string, string> = {};

const AsyncStorage = {
  getItem: jest.fn(async (key: string): Promise<string | null> => store[key] ?? null),
  setItem: jest.fn(async (key: string, value: string): Promise<void> => {
    store[key] = value;
  }),
  removeItem: jest.fn(async (key: string): Promise<void> => {
    delete store[key];
  }),
  getMany: jest.fn(async (keys: string[]): Promise<Record<string, string | null>> => {
    const found: Record<string, string | null> = {};
    keys.forEach((k) => {
      found[k] = store[k] ?? null;
    });
    return found;
  }),
  setMany: jest.fn(async (entries: Record<string, string>): Promise<void> => {
    Object.assign(store, entries);
  }),
  removeMany: jest.fn(async (keys: string[]): Promise<void> => {
    keys.forEach((k) => delete store[k]);
  }),
  getAllKeys: jest.fn(async (): Promise<string[]> => Object.keys(store)),
  clear: jest.fn(async (): Promise<void> => {
    Object.keys(store).forEach((k) => delete store[k]);
  }),
};

export default AsyncStorage;
