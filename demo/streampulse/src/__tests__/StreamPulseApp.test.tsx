import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import StreamPulseApp from '@/components/StreamPulseApp';
import { STORY_COMMAND, STORY_STEPS } from '@/components/RolloutStoryDrawer';
import { DEVICE_PRESETS } from '@/lib/devices';
import { CACHE_TTL_MS, FLAG_KEYS } from '@/lib/env';
import { SCREENS } from '@/lib/screens';
import { __setFlag, providerMounts } from '@/__mocks__/experimently-sdk';
import { resetTestState } from '@/test-utils';

function renderApp(props: Partial<React.ComponentProps<typeof StreamPulseApp>> = {}) {
  return render(<StreamPulseApp apiKey="test-key" apiUrl="http://localhost:8000" {...props} />);
}

function lastMount() {
  return providerMounts[providerMounts.length - 1];
}

describe('StreamPulseApp', () => {
  beforeEach(resetTestState);

  it('mounts the SDK provider once for the default device with the device as user + attributes and a 5 s cache', () => {
    renderApp();
    expect(providerMounts).toHaveLength(1);
    expect(lastMount().config).toEqual({ apiKey: 'test-key', baseUrl: 'http://localhost:8000', timeoutMs: 5000, cacheTtlMs: CACHE_TTL_MS });
    expect(lastMount().user.userId).toBe('sp-iphone-15-us-premium');
    expect(lastMount().user.attributes).toMatchObject({ os: 'iOS', os_version: '17.4.0', region: 'US', tier: 'premium', employee: false });
    expect(screen.getByTestId('active-device-id')).toHaveTextContent('sp-iphone-15-us-premium');
    expect(screen.queryByTestId('missing-key-alert')).not.toBeInTheDocument();
  });

  it('choosing a preset re-keys (unmounts + mounts) the provider with the new device', () => {
    renderApp();
    fireEvent.click(screen.getByRole('radio', { name: 'Galaxy S10 · Android 12 · US · premium · app 3.1.0' }));

    expect(providerMounts).toHaveLength(2);
    expect(lastMount().user.userId).toBe('sp-galaxy-s10-us-premium');
    expect(lastMount().user.attributes).toMatchObject({ os: 'Android', os_version: '12.0.0', app_version: '3.1.0' });
    expect(screen.getByTestId('active-device-id')).toHaveTextContent('sp-galaxy-s10-us-premium');
    expect(screen.getByRole('radio', { name: 'Galaxy S10 · Android 12 · US · premium · app 3.1.0' })).toBeChecked();

    // Same preset again: nothing to re-key.
    fireEvent.click(screen.getByRole('radio', { name: 'Galaxy S10 · Android 12 · US · premium · app 3.1.0' }));
    expect(providerMounts).toHaveLength(2);
  });

  it('applying custom fields re-keys the provider with the edited attributes (and not before Apply)', () => {
    renderApp();
    fireEvent.change(screen.getByLabelText('OS version'), { target: { value: '18.0.0' } });
    fireEvent.click(screen.getByLabelText('Employee (internal tester)'));
    expect(providerMounts).toHaveLength(1); // draft only

    fireEvent.click(screen.getByRole('button', { name: 'Apply device' }));
    expect(providerMounts).toHaveLength(2);
    expect(lastMount().user.userId).toBe('sp-iphone-15-us-premium');
    expect(lastMount().user.attributes).toMatchObject({ os_version: '18.0.0', employee: true });
    expect(screen.getByTestId('custom-device-note')).toBeInTheDocument();
    expect(screen.queryByRole('radio', { checked: true })).not.toBeInTheDocument();
  });

  it('rejects an invalid custom device without re-keying', () => {
    renderApp();
    fireEvent.change(screen.getByLabelText('App version'), { target: { value: 'latest' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply device' }));
    expect(screen.getByText('Use a semver like 3.2.1.')).toBeInTheDocument();
    expect(screen.getByLabelText('App version')).toHaveAttribute('aria-invalid', 'true');
    expect(providerMounts).toHaveLength(1);
  });

  it('"Refresh now" re-keys the provider for the same device but keeps the event log', () => {
    renderApp();
    fireEvent.click(screen.getByRole('tab', { name: 'Player' }));
    fireEvent.click(screen.getByRole('button', { name: 'Play' }));
    expect(within(screen.getByTestId('panel-events')).getAllByRole('listitem')[0]).toHaveTextContent('play');

    fireEvent.click(screen.getByRole('button', { name: 'Refresh now' }));
    expect(providerMounts).toHaveLength(2);
    expect(lastMount().user.userId).toBe('sp-iphone-15-us-premium');
    expect(within(screen.getByTestId('panel-events')).getAllByRole('listitem').some((li) => li.textContent?.includes('play'))).toBe(true);
  });

  it('switching device starts a fresh event log and keeps the current screen', () => {
    renderApp();
    fireEvent.click(screen.getByRole('tab', { name: 'Notifications' }));
    fireEvent.click(screen.getByRole('button', { name: 'Open notification' }));
    expect(screen.getByTestId('panel-events')).toHaveTextContent('notification_open');

    fireEvent.click(screen.getByRole('radio', { name: 'Pixel 7 · Android 14 · DE · free' }));
    expect(screen.getByTestId('phone')).toHaveAttribute('data-screen', 'notifications');
    // Only the new device's screen_view is logged; the old device's events are gone.
    const items = within(screen.getByTestId('panel-events')).getAllByRole('listitem');
    expect(items).toHaveLength(1);
    expect(items[0]).toHaveTextContent('screen_view');
    expect(screen.queryByText(/notification_open/)).not.toBeInTheDocument();
  });

  it('the tab bar exposes all 7 screens as tabs and switches the phone screen', () => {
    renderApp();
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.getAttribute('aria-label'))).toEqual(SCREENS.map((s) => s.name));
    expect(screen.getByRole('tab', { name: 'Home' })).toHaveAttribute('aria-selected', 'true');

    for (const s of SCREENS) {
      fireEvent.click(screen.getByRole('tab', { name: s.name }));
      expect(screen.getByTestId('phone')).toHaveAttribute('data-screen', s.id);
      expect(screen.getByRole('tab', { name: s.name })).toHaveAttribute('aria-selected', 'true');
      expect(screen.getByRole('tabpanel')).toHaveAccessibleName(s.name);
    }
  });

  it('opening a track from Home switches to the Player with that track', () => {
    renderApp();
    fireEvent.click(screen.getByRole('button', { name: 'Open Static Bloom in the player' }));
    expect(screen.getByTestId('phone')).toHaveAttribute('data-screen', 'player');
    expect(screen.getByTestId('player')).toHaveTextContent('Static Bloom');
  });

  it('the rollout story drawer lists the 7 steps, the script command and the live Player v2 readout', () => {
    __setFlag(FLAG_KEYS.playerV2, { isEnabled: true, reason: 'targeting_rule' });
    renderApp({ initialDevice: DEVICE_PRESETS[4].device });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Rollout story' }));
    const dialog = screen.getByRole('dialog', { name: 'The rollout story' });
    expect(STORY_STEPS).toHaveLength(7);
    for (const step of STORY_STEPS) expect(within(dialog).getByText(step.title)).toBeInTheDocument();
    expect(within(dialog).getByText(STORY_COMMAND)).toBeInTheDocument();
    expect(within(dialog).getByTestId('story-player-state')).toHaveTextContent('on');
    expect(within(dialog).getByTestId('story-player-reason')).toHaveTextContent('on: matched a targeting rule');
    expect(within(dialog).getByTestId('story-device')).toHaveTextContent('iPhone 15 · iOS 17.4.0 · app 3.2.1 · employee');

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('warns when the API key is missing but still renders the app', () => {
    renderApp({ apiKey: '' });
    expect(screen.getByTestId('missing-key-alert')).toHaveTextContent('NEXT_PUBLIC_CONTROLVAR_API_KEY is not set');
    expect(screen.getByTestId('phone')).toBeInTheDocument();
    expect(lastMount().config.apiKey).toBe('missing-api-key');
  });
});
