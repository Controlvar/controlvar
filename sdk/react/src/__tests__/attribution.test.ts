import {
  ATTRIBUTION_HORIZON_MS,
  AttributedKeys,
  LIFECYCLE_REASONS,
  MAX_ATTRIBUTED_USERS,
} from '../client/attribution';

afterEach(() => {
  jest.restoreAllMocks();
});

describe('the attributed keys behind the key-less trackEvent fan-out', () => {
  it('keeps 10,000 users for 24 hours and skips the four lifecycle reasons', () => {
    expect(MAX_ATTRIBUTED_USERS).toBe(10_000);
    expect(ATTRIBUTION_HORIZON_MS).toBe(24 * 60 * 60 * 1000);
    expect(Array.from(LIFECYCLE_REASONS).sort()).toEqual(['archived', 'completed', 'draft', 'paused']);
  });

  it('keeps at most MAX_ATTRIBUTED_USERS users: one more drops the least recently answered', () => {
    const keys = new AttributedKeys();
    for (let i = 0; i <= MAX_ATTRIBUTED_USERS; i++) keys.recordExperiment(`u-${i}`, 'checkout');

    expect(keys.size).toBe(MAX_ATTRIBUTED_USERS);
    expect(keys.keys('u-0')).toEqual({ experiments: [], flags: [] });
    expect(keys.keys('u-1')).toEqual({ experiments: ['checkout'], flags: [] });
    expect(keys.keys(`u-${MAX_ATTRIBUTED_USERS}`)).toEqual({ experiments: ['checkout'], flags: [] });
  });

  it('a new answer makes its user the most recently answered, so another user is dropped first', () => {
    const keys = new AttributedKeys();
    for (let i = 0; i < MAX_ATTRIBUTED_USERS; i++) keys.recordExperiment(`u-${i}`, 'checkout');
    keys.recordFlag('u-0', 'new-search'); // u-0 answered again
    keys.recordExperiment('u-new', 'checkout');

    expect(keys.size).toBe(MAX_ATTRIBUTED_USERS);
    expect(keys.keys('u-0')).toEqual({ experiments: ['checkout'], flags: ['new-search'] });
    expect(keys.keys('u-1')).toEqual({ experiments: [], flags: [] });
  });

  it('drops a key once its latest answer is more than 24 hours old', () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(0);
    const keys = new AttributedKeys();
    keys.recordExperiment('u', 'old');
    now.mockReturnValue(ATTRIBUTION_HORIZON_MS);
    keys.recordExperiment('u', 'new');
    expect(keys.keys('u')).toEqual({ experiments: ['old', 'new'], flags: [] });

    now.mockReturnValue(ATTRIBUTION_HORIZON_MS + 1);
    expect(keys.keys('u')).toEqual({ experiments: ['new'], flags: [] });
    now.mockReturnValue(2 * ATTRIBUTION_HORIZON_MS + 1);
    expect(keys.keys('u')).toEqual({ experiments: [], flags: [] });
    expect(keys.size).toBe(0);
  });

  it('measures the 24 hours from the latest answer: a key answered at 0 h and 23 h is still used at 25 h', () => {
    const HOUR = 60 * 60 * 1000;
    const now = jest.spyOn(Date, 'now').mockReturnValue(0);
    const keys = new AttributedKeys();
    keys.recordExperiment('u', 'checkout');
    now.mockReturnValue(23 * HOUR);
    keys.recordExperiment('u', 'checkout');
    now.mockReturnValue(25 * HOUR);
    expect(keys.keys('u')).toEqual({ experiments: ['checkout'], flags: [] });
  });

  it('forgetExperiment drops that experiment only, and the user once nothing is left', () => {
    const keys = new AttributedKeys();
    keys.recordExperiment('u', 'checkout');
    keys.recordExperiment('u', 'hero');
    keys.recordFlag('u', 'new-search');
    keys.forgetExperiment('u', 'checkout');
    expect(keys.keys('u')).toEqual({ experiments: ['hero'], flags: ['new-search'] });

    keys.recordExperiment('v', 'checkout');
    keys.forgetExperiment('v', 'checkout');
    keys.forgetExperiment('nobody', 'checkout');
    expect(keys.size).toBe(1);
  });

  it("never returns one user's keys for another", () => {
    const keys = new AttributedKeys();
    keys.recordExperiment('a', 'checkout');
    keys.recordFlag('a', 'new-search');
    expect(keys.keys('b')).toEqual({ experiments: [], flags: [] });
  });
});
