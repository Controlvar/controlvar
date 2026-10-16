import React, { useCallback, useMemo, useState } from 'react';
import { ExperimentationProvider } from '@getexperimently/react-sdk';
import DevicePanel from '@/components/DevicePanel';
import ControlvarPanel from '@/components/ControlvarPanel';
import PhoneFrame from '@/components/PhoneFrame';
import RolloutStoryDrawer from '@/components/RolloutStoryDrawer';
import { DEFAULT_PRESET, deviceKey, toUserContext, type Device } from '@/lib/devices';
import { API_KEY, API_URL, CACHE_TTL_MS, DASHBOARD_URL } from '@/lib/env';
import { EventLogProvider } from '@/lib/eventLog';
import { DEFAULT_SCREEN, type ScreenId } from '@/lib/screens';

interface Props {
  apiKey?: string;
  apiUrl?: string;
  initialDevice?: Device;
}

/**
 * Page layout: phone on the left, Device + Controlvar panels on the right,
 * rollout-story drawer on demand.
 *
 * The SDK provider is keyed on the device (every attribute) plus a refresh
 * counter, so changing the device — or pressing "Refresh now" — unmounts and
 * re-mounts it with a fresh client: caches are dropped and every flag and
 * experiment is fetched again with the new targeting context.
 */
export default function StreamPulseApp({ apiKey = API_KEY, apiUrl = API_URL, initialDevice = DEFAULT_PRESET.device }: Props) {
  const [device, setDevice] = useState<Device>(initialDevice);
  const [screen, setScreen] = useState<ScreenId>(DEFAULT_SCREEN);
  const [refreshCount, setRefreshCount] = useState(0);
  const [storyOpen, setStoryOpen] = useState(false);

  const missingKey = !apiKey;
  const config = useMemo(
    () => ({ apiKey: apiKey || 'missing-api-key', baseUrl: apiUrl, timeoutMs: 5000, cacheTtlMs: CACHE_TTL_MS }),
    [apiKey, apiUrl],
  );
  const user = useMemo(() => toUserContext(device), [device]);
  const providerKey = `${deviceKey(device)}#${refreshCount}`;

  const refresh = useCallback(() => setRefreshCount((n) => n + 1), []);
  const closeStory = useCallback(() => setStoryOpen(false), []);

  return (
    <div className="min-h-screen">
      <header className="border-b border-neutral-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div>
            <h1 className="text-xl font-extrabold tracking-tight">
              Stream<span className="text-pulse-600">Pulse</span>
            </h1>
            <p className="text-xs text-neutral-500">A simulated mobile app: pick a device, see which flags and experiments it gets.</p>
          </div>
          <nav className="flex items-center gap-2" aria-label="Page">
            <button type="button" className="btn-primary" onClick={() => setStoryOpen(true)} aria-haspopup="dialog" aria-expanded={storyOpen}>
              Rollout story
            </button>
            <a href={DASHBOARD_URL} target="_blank" rel="noreferrer" className="btn-secondary">
              Open dashboard ↗
            </a>
          </nav>
        </div>
      </header>

      {missingKey && (
        <div role="alert" className="border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900" data-testid="missing-key-alert">
          <strong>NEXT_PUBLIC_CONTROLVAR_API_KEY is not set.</strong> Run <code className="font-mono">python backend/scripts/seed_streampulse.py</code> (it
          writes <code className="font-mono">demo/streampulse/.env.local</code>), then restart <code className="font-mono">npm run dev</code>. Until then every
          request fails with 401.
        </div>
      )}

      <main className="mx-auto grid max-w-7xl gap-6 px-4 py-6 lg:grid-cols-[auto_minmax(0,1fr)]">
        <EventLogProvider key={device.device_id}>
          <ExperimentationProvider key={providerKey} config={config} user={user}>
            <div className="flex justify-center lg:justify-start">
              <PhoneFrame device={device} screen={screen} onScreenChange={setScreen} />
            </div>
            <div className="grid content-start gap-4 xl:grid-cols-2">
              <DevicePanel device={device} onChange={setDevice} />
              <ControlvarPanel onRefresh={refresh} />
            </div>
            <RolloutStoryDrawer open={storyOpen} onClose={closeStory} device={device} />
          </ExperimentationProvider>
        </EventLogProvider>
      </main>

      <footer className="mx-auto max-w-7xl px-4 py-6 text-xs text-neutral-500">
        StreamPulse is a demo. No audio, no images, no real users — every tap is a Controlvar event for device{' '}
        <code className="font-mono">{device.device_id}</code>.
      </footer>
    </div>
  );
}
