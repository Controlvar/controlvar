import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { STORY_STEPS } from '@/components/RolloutStoryDrawer';
import ControlvarPanel, { PANEL_EVENT_LIMIT } from '@/components/ControlvarPanel';
import { DASHBOARD_URL, EXPERIMENT_KEYS, FLAG_KEYS } from '@/lib/env';
import { EventLogProvider, useTrack } from '@/lib/eventLog';
import { __setExperiment, __setFlag, clearCacheMock, DEFAULT_TEST_USER } from '@/__mocks__/experimently-sdk';
import { resetTestState } from '@/test-utils';

function Emitter({ count }: { count: number }) {
  const track = useTrack();
  return (
    <button
      type="button"
      onClick={() =>
        Array.from({ length: count }, (_, i) =>
          track(`evt_${i + 1}`, { i: i + 1 }, i % 2 ? { experimentKey: 'exp_x' } : { featureFlagKey: 'flag_y' }),
        )
      }
    >
      emit
    </button>
  );
}

function renderPanel(extra?: React.ReactNode) {
  const onRefresh = jest.fn();
  const utils = render(
    <EventLogProvider>
      {extra}
      <ControlvarPanel onRefresh={onRefresh} />
    </EventLogProvider>,
  );
  return { ...utils, onRefresh };
}

describe('ControlvarPanel', () => {
  beforeEach(resetTestState);

  it('shows every flag as on/off with the server reason spelled out', () => {
    __setFlag(FLAG_KEYS.recsV2, { isEnabled: true, reason: 'rollout' });
    __setFlag(FLAG_KEYS.playerV2, { isEnabled: true, reason: 'targeting_rule' });
    __setFlag(FLAG_KEYS.aiSearch, { isEnabled: false, reason: 'targeting_rule' });
    __setFlag(FLAG_KEYS.offlineMode, { isEnabled: false, reason: 'inactive' });
    renderPanel();

    expect(screen.getByTestId('panel-user-id')).toHaveTextContent(DEFAULT_TEST_USER.userId);
    expect(screen.getByTestId(`panel-flag-${FLAG_KEYS.recsV2}`)).toHaveTextContent('on');
    expect(screen.getByTestId(`panel-flag-reason-${FLAG_KEYS.recsV2}`)).toHaveTextContent('on: inside the rollout %');
    expect(screen.getByTestId(`panel-flag-reason-${FLAG_KEYS.playerV2}`)).toHaveTextContent('on: matched a targeting rule');
    expect(screen.getByTestId(`panel-flag-reason-${FLAG_KEYS.aiSearch}`)).toHaveTextContent('off: matched a rule but outside its rollout %');
    expect(screen.getByTestId(`panel-flag-reason-${FLAG_KEYS.offlineMode}`)).toHaveTextContent('off: flag is inactive');
  });

  it('shows the variant for assigned experiments and a human label for every assigned:false reason', () => {
    __setExperiment(EXPERIMENT_KEYS.pushFrequency, { variantName: 'three_weekly', isControl: false, assigned: true, reason: 'assigned' });
    __setExperiment(EXPERIMENT_KEYS.wrapped, { variantName: 'control', assigned: false, reason: 'mutual_exclusion' });
    __setExperiment(EXPERIMENT_KEYS.profileBadges, { variantName: 'badges', isControl: false });
    __setExperiment(EXPERIMENT_KEYS.onboardingSteps, { variantName: 'five_step', assigned: false, reason: 'holdout' });
    __setExperiment(EXPERIMENT_KEYS.upsellModal, { variantName: 'control', assigned: false, reason: 'targeting' });
    renderPanel();

    expect(screen.getByTestId(`panel-exp-${EXPERIMENT_KEYS.pushFrequency}`)).toHaveTextContent('three_weekly');
    expect(screen.getByTestId(`panel-exp-reason-${EXPERIMENT_KEYS.pushFrequency}`)).toHaveTextContent('assigned to three_weekly');
    expect(screen.getByTestId(`panel-exp-${EXPERIMENT_KEYS.wrapped}`)).toHaveTextContent('not enrolled');
    expect(screen.getByTestId(`panel-exp-reason-${EXPERIMENT_KEYS.wrapped}`)).toHaveTextContent(
      'excluded: mutual exclusion group streampulse-profile (control shown)',
    );
    expect(screen.getByTestId(`panel-exp-${EXPERIMENT_KEYS.profileBadges}`)).toHaveTextContent('badges');
    expect(screen.getByTestId(`panel-exp-reason-${EXPERIMENT_KEYS.onboardingSteps}`)).toHaveTextContent('in global holdout streampulse-holdout (control shown)');
    expect(screen.getByTestId(`panel-exp-reason-${EXPERIMENT_KEYS.upsellModal}`)).toHaveTextContent('excluded: targeting rules (control shown)');
  });

  it('says when decisions are fetched (device pick or Refresh now), never a 5 s timer', () => {
    renderPanel();
    expect(screen.getByText(/fetched when you pick a device or press Refresh now/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/every 5 s/);
    const story = STORY_STEPS.map((s) => `${s.action} ${s.lookAt}`).join(' ');
    expect(story).not.toMatch(/Within 5 s/);
    expect(story).toMatch(/Refresh now/);
  });

  it('shows loading and error states', () => {
    __setFlag(FLAG_KEYS.recsV2, { loading: true });
    __setExperiment(EXPERIMENT_KEYS.wrapped, { loading: true });
    __setExperiment(EXPERIMENT_KEYS.upsellModal, { error: new Error('API error: 401') });
    renderPanel();
    expect(screen.getByTestId(`panel-flag-${FLAG_KEYS.recsV2}`)).toHaveTextContent('loading…');
    expect(screen.getByTestId(`panel-exp-${EXPERIMENT_KEYS.wrapped}`)).toHaveTextContent('assigning…');
    expect(screen.getByTestId(`panel-exp-reason-${EXPERIMENT_KEYS.upsellModal}`)).toHaveTextContent('error: API error: 401');
  });

  it(`lists only the last ${PANEL_EVENT_LIMIT} tracked events, newest first, with their keys`, () => {
    renderPanel(<Emitter count={10} />);
    expect(screen.getByText(/No events yet/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'emit' }));
    const items = within(screen.getByTestId('panel-events')).getAllByRole('listitem');
    expect(items).toHaveLength(PANEL_EVENT_LIMIT);
    expect(items[0]).toHaveTextContent('evt_10');
    expect(items[0]).toHaveTextContent('exp:exp_x');
    expect(items[1]).toHaveTextContent('flag:flag_y');
    expect(items[PANEL_EVENT_LIMIT - 1]).toHaveTextContent('evt_3');
    expect(screen.queryByText(/evt_1\b/)).not.toBeInTheDocument();
  });

  it('"Refresh now" clears the SDK cache and asks the page to re-evaluate; links point at the dashboard', () => {
    const { onRefresh } = renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh now' }));
    expect(clearCacheMock).toHaveBeenCalledTimes(1);
    expect(onRefresh).toHaveBeenCalledTimes(1);

    expect(screen.getByRole('link', { name: /feature flags/i })).toHaveAttribute('href', `${DASHBOARD_URL}/feature-flags`);
    expect(screen.getByRole('link', { name: /experiments/i })).toHaveAttribute('href', `${DASHBOARD_URL}/experiments`);
    expect(screen.getByRole('link', { name: /safety/i })).toHaveAttribute('href', `${DASHBOARD_URL}/admin/safety`);
    expect(screen.getByRole('link', { name: /audit log/i })).toHaveAttribute('href', `${DASHBOARD_URL}/admin/audit`);
  });
});
