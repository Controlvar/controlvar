/**
 * The website details the built classic script sends, against the contract
 * files: tests/sdk-contract/visual-script-vectors.json (what a browser gives
 * the script and the exact details it sends, written by hand) and
 * visual-visitor-vectors.json (the server's answers, generated from the
 * server). Every vector runs in jsdom through dist/controlvar-visual.js, so
 * the details are read from the call the page makes, exactly as sent.
 */
import { CORPORA, SCRIPT, SERVER, sentForAll, VECTORS, type Sent } from './vectors';

let results = new Map<string, Sent>();

beforeAll(async () => {
  results = await sentForAll();
});

describe('the script vectors', () => {
  test('are the counted number, each id once', () => {
    expect(CORPORA.map((corpus) => SCRIPT[corpus].length)).toEqual(CORPORA.map((corpus) => SCRIPT.counts[corpus]));
    expect(VECTORS).toHaveLength(93);
    expect(new Set(VECTORS.map((vector) => vector.id)).size).toBe(VECTORS.length);
  });

  test('every vector: the script sends exactly its details, and no other context', () => {
    const wrong: string[] = [];
    for (const vector of VECTORS) {
      const got = results.get(vector.id)!;
      if (got.calls !== 1) wrong.push(`${vector.id} ${vector.name}: ${got.calls} calls != 1`);
      else if (JSON.stringify(got.context) !== JSON.stringify(vector.context)) {
        wrong.push(`${vector.id} ${vector.name}: expected ${JSON.stringify(vector.context)}, received ${JSON.stringify(got.context)}`);
      }
      if (vector.warnings !== undefined && got.warnings !== vector.warnings) {
        wrong.push(`${vector.id} ${vector.name}: warnings ${got.warnings} != ${vector.warnings}`);
      }
      if (vector.writes !== undefined && JSON.stringify(got.writes) !== JSON.stringify(vector.writes)) {
        wrong.push(`${vector.id} ${vector.name}: writes ${JSON.stringify(got.writes)} != ${JSON.stringify(vector.writes)}`);
      }
    }
    expect(wrong).toEqual([]);
  });
});

/** The audience whose form a value takes, when the server drops it; null when the server keeps the form. */
function droppedForm(key: string, value: unknown): string | null {
  if (!SERVER.keys.includes(key)) return key === 'web_country' ? 'aud-17' : `unknown key ${key}`;
  if (value === null) return 'aud-14';
  if (value === '') return 'aud-15';
  if (typeof value !== 'string') return 'not a string';
  const closed: Record<string, string[]> = { web_device: SERVER.devices, web_browser: SERVER.browsers, web_visitor: SERVER.visitors };
  if (closed[key]) {
    if (closed[key].includes(value)) return null;
    return closed[key].includes(value.toLowerCase()) ? 'aud-06' : 'aud-16';
  }
  const limits: Record<string, number> = {
    web_language: 3,
    web_referrer: SERVER.limits.referrer,
    web_utm_source: SERVER.limits.utm,
    web_utm_medium: SERVER.limits.utm,
    web_utm_campaign: SERVER.limits.utm,
    web_landing_path: SERVER.limits.landing_path,
  };
  return value.length > limits[key] ? `over ${limits[key]} characters` : null;
}

describe('the script contexts against the server', () => {
  test('no vector names a detail in a form the server drops (null, empty, outside its set, web_country)', () => {
    const forms = VECTORS.flatMap((vector) =>
      Object.entries(vector.context)
        .map(([key, value]) => [key, droppedForm(key, value)] as const)
        .filter(([, form]) => form !== null)
        .map(([key, form]) => `${vector.id}: ${key} is ${form}'s form`),
    );
    expect(forms).toEqual([]);
  });

  test('the check finds each form the server drops', () => {
    expect([
      droppedForm('web_device', null),
      droppedForm('web_device', ''),
      droppedForm('web_device', 'mobile'),
      droppedForm('web_device', 'Phone'),
      droppedForm('web_country', 'FR'),
      droppedForm('web_device', 'phone'),
    ]).toEqual(['aud-14', 'aud-15', 'aud-16', 'aud-06', 'aud-17', null]);
  });

  test('the details the built script sent are never in a form the server drops', () => {
    // The check above reads the file; this one reads what the built script sent.
    const wrong: string[] = [];
    for (const vector of VECTORS) {
      for (const [key, value] of Object.entries(results.get(vector.id)!.context)) {
        const form = droppedForm(key, value);
        if (form !== null) wrong.push(`${vector.id} ${vector.name}: ${key} ${JSON.stringify(value)} is ${form}'s form`);
      }
    }
    expect(wrong).toEqual([]);
  });

  test("every audience the server matches has its website details in some vector's context", () => {
    const matching = SERVER.audiences.filter((audience: { matches: boolean }) => audience.matches);
    expect(matching.length).toBeGreaterThan(0);
    const unreachable = matching
      .map((audience: { id: string; context: Record<string, unknown> }) => {
        const web = Object.entries(audience.context).filter(([key]) => key.startsWith('web_'));
        const reached = VECTORS.some((vector) => web.every(([key, value]) => results.get(vector.id)!.context[key] === value));
        return reached ? null : `${audience.id}: ${JSON.stringify(Object.fromEntries(web))}`;
      })
      .filter((line: string | null) => line !== null);
    expect(unreachable).toEqual([]);
  });

  test('every detail is left out by some vector, and the server matches no negative rule on a detail left out', () => {
    const neverOmitted = SERVER.keys.filter((key: string) => VECTORS.every((vector) => key in results.get(vector.id)!.context));
    expect(neverOmitted).toEqual([]);
    const absent = SERVER.audiences.filter((audience: { id: string }) => audience.id.startsWith('absent-'));
    expect(absent).toHaveLength(SERVER.keys.length * SERVER.negative_operators.length);
    expect(absent.filter((audience: { matches: boolean }) => audience.matches).map((audience: { id: string }) => audience.id)).toEqual([]);
  });
});
