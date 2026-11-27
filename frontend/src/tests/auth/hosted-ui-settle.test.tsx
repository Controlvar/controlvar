/**
 * `settle()` in hostedUiHarness.tsx returns only after the sign-in it started
 * has sent the browser to the Hosted UI. The redirect waits for Web Crypto's
 * digest, which Node answers from its thread pool: on a loaded runner that
 * took longer than the twelve turns of the event loop `settle()` used to
 * wait, the redirect had not happened, and the next line read an authorize
 * URL that was not there (`TypeError: Invalid URL: undefined`).
 */
import { authorizeHits, hostedUiReturns, renderLogin, resetHarness, restoreHarness, settle, storedPending } from './hostedUiHarness';
import { act, fireEvent, screen } from '@testing-library/react';

const SLOW_TURNS = 50;

/** Web Crypto's digest, answering only after `turns` more turns of the event loop. */
function slowDigest(turns: number): void {
  const proto = Object.getPrototypeOf(globalThis.crypto.subtle) as SubtleCrypto;
  const real = proto.digest;
  jest.spyOn(proto, 'digest').mockImplementation(function (this: SubtleCrypto, algorithm, data) {
    const answer = real.call(this, algorithm, data);
    return new Promise<ArrayBuffer>((resolve, reject) => {
      let left = turns;
      const turn = (): void => {
        if (left > 0) {
          left -= 1;
          setTimeout(turn, 0);
        } else {
          answer.then(resolve, reject);
        }
      };
      turn();
    });
  });
}

async function turnsOfTheEventLoop(n: number): Promise<void> {
  for (let i = 0; i < n; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

beforeEach(() => {
  resetHarness();
});

afterEach(() => {
  restoreHarness();
  jest.restoreAllMocks();
});

describe('regression: settle() waits for a slow digest', () => {
  it('a digest slower than twelve turns: the redirect is made before settle() returns', async () => {
    slowDigest(SLOW_TURNS);
    await renderLogin();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    });
    // The plant holds the redirect past the twelve turns settle() used to wait.
    await turnsOfTheEventLoop(12);
    expect(authorizeHits()).toEqual([]);
    await settle();
    expect(authorizeHits()).toHaveLength(1);
    const state = new URL(authorizeHits()[0]).searchParams.get('state');
    expect(storedPending()?.state).toBe(state);
    expect(hostedUiReturns()).toBe(`?code=code-1&state=${state}`);
  });
});
