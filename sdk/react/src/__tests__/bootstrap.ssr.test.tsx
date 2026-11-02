/**
 * @jest-environment node
 *
 * The bootstrap during a server render (#1518): the Provider's hooks render the
 * bootstrapped answers, with no request, and the render warns about nothing
 * (warnings come from an effect, and a server render runs none). The browser's
 * side (hydration, refresh, warnings) is in bootstrap.test.tsx.
 *
 * Requests and warnings are counted only after two macrotask flushes: one
 * started during the render would come later, so counting right after
 * `renderToString` would prove nothing.
 */
import React, { StrictMode } from 'react';
import { renderToString } from 'react-dom/server';
import { ExperimentationProvider } from '../context/ExperimentationProvider';
import { ServerClient } from '../client/ServerClient';
import { useExperiment } from '../hooks/useExperiment';
import { useFeatureFlag } from '../hooks/useFeatureFlag';
import { useMultipleFlags } from '../hooks/useMultipleFlags';
import { useVariant } from '../hooks/useVariant';
import type {
  ExperimentAssignResponse,
  ExperimentationBootstrap,
  FeatureFlagEvaluateResponse,
  SdkConfig,
} from '../client/types';

const config: SdkConfig = { apiKey: 'test-key', baseUrl: 'https://api.example.com' };
const flagOn: FeatureFlagEvaluateResponse = { key: 'flag-1', enabled: true, config: null, reason: 'rollout' };
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
const bootstrap: ExperimentationBootstrap = {
  userId: 'u-1',
  flags: { 'flag-1': flagOn },
  experiments: { 'exp-1': enrolled },
};

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

function Banner() {
  const flag = useFeatureFlag('flag-1');
  const exp = useExperiment('exp-1');
  const text = `${flag.isEnabled ? 'FLAG-ON' : 'FLAG-OFF'}|${exp.variantName}|loading=${flag.loading || exp.loading}`;
  return <span>{text}</span>;
}

let fetchMock: jest.Mock;

beforeEach(() => {
  fetchMock = jest.fn().mockImplementation((url: string) =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(url.includes('/tracking/assign') ? enrolled : flagOn),
    })
  );
  global.fetch = fetchMock;
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe('a server render with a bootstrap', () => {
  it('renders the bootstrapped flag and variant, and makes no request', async () => {
    expect(typeof window).toBe('undefined');
    const html = renderToString(
      <ExperimentationProvider config={config} user={{ userId: 'u-1' }} bootstrap={bootstrap}>
        <Banner />
      </ExperimentationProvider>
    );
    expect(html).toBe('<span>FLAG-ON|treatment|loading=false</span>');
    await flush();
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(0);
  });

  it('renders the loading defaults without one, as before', () => {
    const html = renderToString(
      <ExperimentationProvider config={config} user={{ userId: 'u-1' }}>
        <Banner />
      </ExperimentationProvider>
    );
    expect(html).toBe('<span>FLAG-OFF|Control|loading=true</span>');
  });

  it('renders useVariant and useMultipleFlags from it too', () => {
    function Others() {
      const variant = useVariant('flag-1');
      const many = useMultipleFlags(['flag-1', 'flag-2']);
      const text = `${variant}|${many['flag-1']?.isEnabled}|${many['flag-2']}`;
      return <span>{text}</span>;
    }
    const html = renderToString(
      <ExperimentationProvider config={config} user={{ userId: 'u-1' }} bootstrap={bootstrap}>
        <Others />
      </ExperimentationProvider>
    );
    expect(html).toBe('<span>on|true|null</span>');
  });

  it('never warns during a server render, whatever the bootstrap', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    const cases: unknown[] = [
      bootstrap,
      { ...bootstrap, userId: 'someone-else' },
      'not an object',
      { userId: 'u-1', flags: [] },
      { userId: 'u-1', flags: { 'flag-1': { isEnabled: true } } },
    ];
    for (const value of cases) {
      renderToString(
        <StrictMode>
          <ExperimentationProvider
            config={config}
            user={{ userId: 'u-1' }}
            bootstrap={value as ExperimentationBootstrap}
          >
            <Banner />
          </ExperimentationProvider>
        </StrictMode>
      );
    }
    await flush();
    await flush();
    expect(warn).toHaveBeenCalledTimes(0);
    expect(fetchMock).toHaveBeenCalledTimes(0);
  });

  it("calls no Provider's onExposure: the server render records nothing", async () => {
    const hook = jest.fn();
    renderToString(
      <ExperimentationProvider
        config={{ ...config, onExposure: hook }}
        user={{ userId: 'u-1' }}
        bootstrap={bootstrap}
      >
        <Banner />
      </ExperimentationProvider>
    );
    await flush();
    await flush();
    expect(hook).toHaveBeenCalledTimes(0);
    expect(fetchMock).toHaveBeenCalledTimes(0);
  });
});

describe('ServerClient.bootstrap() in Node', () => {
  it('builds the bootstrap a server render then shows, and calls its own onExposure once per assigned experiment', async () => {
    const hook = jest.fn();
    const server = new ServerClient({ ...config, onExposure: hook });
    const built = await server.bootstrap({ flags: ['flag-1'], experiments: ['exp-1'] }, { userId: 'u-1' });
    expect(built).toEqual(bootstrap);
    expect(hook).toHaveBeenCalledTimes(1);
    expect(hook).toHaveBeenCalledWith({
      experimentKey: 'exp-1',
      userId: 'u-1',
      variantId: 'v-2',
      variantName: 'treatment',
    });
    const html = renderToString(
      <ExperimentationProvider config={config} user={{ userId: 'u-1' }} bootstrap={JSON.parse(JSON.stringify(built))}>
        <Banner />
      </ExperimentationProvider>
    );
    expect(html).toBe('<span>FLAG-ON|treatment|loading=false</span>');
  });
});
