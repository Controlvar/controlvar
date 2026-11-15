/**
 * @jest-environment node
 *
 * The exposure hook during a server render (#1499): rendering the Provider and
 * `useExperiment` on the server makes no request and calls no `onExposure`,
 * because `useExperiment` assigns in an effect and a server render runs none.
 * Hydration in the browser then makes the one request and the one call
 * (exposure.test.tsx, 'calls it once after hydrating server markup').
 *
 * The hook is counted only after two macrotask flushes: a request started
 * during the render would call it later, so counting it right after
 * `renderToString` would prove nothing.
 */
import React, { StrictMode } from 'react';
import { renderToString } from 'react-dom/server';
import { ExperimentationProvider } from '../context/ExperimentationProvider';
import { useExperiment } from '../hooks/useExperiment';
import type { ExperimentAssignResponse, SdkConfig } from '../client/types';

const config: SdkConfig = { apiKey: 'test-key', baseUrl: 'https://api.example.com' };
const enrolled: ExperimentAssignResponse = {
  experiment_key: 'exp-1',
  user_id: 'u-1',
  variant_id: 'v-2',
  variant_name: 'treatment',
  is_control: false,
  configuration: null,
  assigned: true,
  reason: 'assigned',
};

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

function Variant() {
  const { variantKey } = useExperiment('exp-1');
  return <span>{variantKey}</span>;
}

let fetchMock: jest.Mock;

beforeEach(() => {
  fetchMock = jest.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve(enrolled) });
  global.fetch = fetchMock;
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe('onExposure during a server render', () => {
  it('is not called, and the render makes no request', async () => {
    expect(typeof window).toBe('undefined');
    const hook = jest.fn();

    const html = renderToString(
      <ExperimentationProvider config={{ ...config, onExposure: hook }} user={{ userId: 'u-1' }}>
        <Variant />
      </ExperimentationProvider>
    );

    expect(fetchMock).toHaveBeenCalledTimes(0);
    await flush();
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(0);
    expect(hook).toHaveBeenCalledTimes(0);
    // The markup exposure.test.tsx hydrates: the control default, as no answer exists yet.
    expect(html).toBe('<span>control</span>');
  });

  it('is not called under StrictMode either', async () => {
    const hook = jest.fn();
    renderToString(
      <StrictMode>
        <ExperimentationProvider config={{ ...config, onExposure: hook }} user={{ userId: 'u-1' }}>
          <Variant />
          <Variant />
        </ExperimentationProvider>
      </StrictMode>
    );
    await flush();
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(0);
    expect(hook).toHaveBeenCalledTimes(0);
  });
});
