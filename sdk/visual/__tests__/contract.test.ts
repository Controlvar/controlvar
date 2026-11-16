/**
 * The storage keys and the error reports are the contract files'
 * (tests/sdk-contract/visual-storage-keys.json and visual-error-reports.json):
 * the script's lists are pinned equal to them, never kept as a second copy.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import { KEYS, WRITTEN } from '../src/keys';
import { MAX_REPORTS, MESSAGES } from '../src/reports';

const CONTRACT = join(__dirname, '..', '..', '..', 'tests', 'sdk-contract');
const STORAGE = JSON.parse(readFileSync(join(CONTRACT, 'visual-storage-keys.json'), 'utf8'));
const REPORTS = JSON.parse(readFileSync(join(CONTRACT, 'visual-error-reports.json'), 'utf8'));

interface StorageKey {
  name: string;
  storage: string;
  written: boolean;
}

describe('the storage keys', () => {
  const keys: StorageKey[] = STORAGE.keys;

  test('are exactly the ten in the contract file, in its order', () => {
    expect(keys).toHaveLength(10);
    expect(Object.values(KEYS)).toEqual(keys.map((key) => key.name));
  });

  test('all start with the one prefix, and each lives in one storage', () => {
    for (const key of keys) {
      expect(key.name.startsWith(STORAGE.prefix)).toBe(true);
      expect(['cookie', 'localStorage', 'sessionStorage']).toContain(key.storage);
    }
  });

  test('the ones the script writes are the ones the file marks written', () => {
    expect([...WRITTEN].sort()).toEqual(keys.filter((key) => key.written).map((key) => key.name).sort());
  });
});

describe('the error reports', () => {
  test('are sent with exactly the four documented keys, at most five per page view', () => {
    expect(REPORTS.request_keys).toEqual(['error_type', 'experiment_key', 'message', 'metadata']);
    expect(REPORTS.request_keys).not.toContain('feature_flag_key');
    expect(MAX_REPORTS).toBe(REPORTS.max_per_page_view);
  });

  test("visual_change_not_applied's reasons are the script's, each with its own fixed message", () => {
    const notApplied = REPORTS.types.find((type: { error_type: string }) => type.error_type === 'visual_change_not_applied');
    expect(notApplied.metadata_keys).toEqual(['index', 'reason']);
    expect(Object.keys(MESSAGES).sort()).toEqual(Object.keys(notApplied.reasons).sort());
    expect(new Set(Object.values(MESSAGES)).size).toBe(Object.keys(MESSAGES).length);
  });

  test('the types sent today are exactly visual_change_not_applied', () => {
    const sent = REPORTS.types.filter((type: { sent: boolean }) => type.sent).map((type: { error_type: string }) => type.error_type);
    expect(sent).toEqual(['visual_change_not_applied']);
  });
});
