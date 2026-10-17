import React from 'react';
import { fireEvent, screen, within } from '@testing-library/react';
import HomeScreen from '@/components/screens/HomeScreen';
import PlayerScreen from '@/components/screens/PlayerScreen';
import SearchScreen from '@/components/screens/SearchScreen';
import NotificationsScreen from '@/components/screens/NotificationsScreen';
import ProfileScreen from '@/components/screens/ProfileScreen';
import OnboardingScreen from '@/components/screens/OnboardingScreen';
import PaymentsScreen from '@/components/screens/PaymentsScreen';
import { EXPERIMENT_KEYS, FLAG_KEYS } from '@/lib/env';
import { __setExperiment, __setFlag, trackEventMock } from '@/__mocks__/experimently-sdk';
import { callsFor, renderScreen, resetTestState } from '@/test-utils';

beforeEach(resetTestState);

describe('Home (streampulse_recs_v2)', () => {
  it('flag on: "For you" feed and one recs_impression attributed to the flag', () => {
    __setFlag(FLAG_KEYS.recsV2, { isEnabled: true, reason: 'rollout' });
    renderScreen(<HomeScreen onOpenTrack={jest.fn()} />);

    expect(screen.getByRole('heading', { name: 'For you' })).toBeInTheDocument();
    expect(screen.getByTestId('home-feed')).toHaveAttribute('data-feed', 'recs_v2');
    const [call] = callsFor('recs_impression');
    expect(call[1]).toMatchObject({ algorithm: 'recs_v2' });
    expect(call[2]).toEqual({ featureFlagKey: FLAG_KEYS.recsV2 });
    expect(callsFor('recs_impression')).toHaveLength(1);
    expect(callsFor('screen_view')[0][1]).toEqual({ screen: 'home' });
  });

  it('flag off (kill switch): "Classic" chronological feed and no impression', () => {
    __setFlag(FLAG_KEYS.recsV2, { isEnabled: false, reason: 'rollout' });
    const onOpenTrack = jest.fn();
    renderScreen(<HomeScreen onOpenTrack={onOpenTrack} />);

    expect(screen.getByRole('heading', { name: 'Classic' })).toBeInTheDocument();
    expect(screen.getByTestId('home-feed')).toHaveAttribute('data-feed', 'chronological');
    expect(callsFor('recs_impression')).toHaveLength(0);
    const items = within(screen.getByTestId('home-feed')).getAllByRole('listitem');
    expect(items[0]).toHaveTextContent('Neon Tide');

    fireEvent.click(screen.getByRole('button', { name: 'Open Neon Tide in the player' }));
    expect(onOpenTrack).toHaveBeenCalledWith('trk-001');
  });

  it('does not fire an impression while the flag is still loading', () => {
    __setFlag(FLAG_KEYS.recsV2, { isEnabled: false, loading: true });
    renderScreen(<HomeScreen onOpenTrack={jest.fn()} />);
    expect(callsFor('recs_impression')).toHaveLength(0);
  });
});

describe('Player (streampulse_player_v2, streampulse_offline_mode)', () => {
  it('flag on: redesigned player; Play sends play {track_id, player: v2} attributed to the flag', () => {
    __setFlag(FLAG_KEYS.playerV2, { isEnabled: true, reason: 'targeting_rule' });
    renderScreen(<PlayerScreen trackId="trk-003" onChangeTrack={jest.fn()} />);

    expect(screen.getByTestId('player')).toHaveAttribute('data-player', 'v2');
    expect(screen.getByText('Player v2')).toBeInTheDocument();
    expect(screen.queryByTestId('download-button')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Play' }));
    const [call] = callsFor('play');
    expect(call[1]).toEqual({ track_id: 'trk-003', player: 'v2' });
    expect(call[2]).toEqual({ featureFlagKey: FLAG_KEYS.playerV2 });
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument();
  });

  it('flag off: classic player, play {player: classic}; Next asks the frame to change track', () => {
    __setFlag(FLAG_KEYS.playerV2, { isEnabled: false });
    const onChangeTrack = jest.fn();
    renderScreen(<PlayerScreen trackId="trk-010" onChangeTrack={onChangeTrack} />);

    expect(screen.getByTestId('player')).toHaveAttribute('data-player', 'classic');
    fireEvent.click(screen.getByRole('button', { name: 'Play' }));
    expect(callsFor('play')[0][1]).toEqual({ track_id: 'trk-010', player: 'classic' });
    fireEvent.click(screen.getByRole('button', { name: 'Next track' }));
    expect(onChangeTrack).toHaveBeenCalledWith('trk-001'); // wraps around
  });

  it('offline mode flag on: a Download button appears (no event, UI only)', () => {
    __setFlag(FLAG_KEYS.offlineMode, { isEnabled: true });
    renderScreen(<PlayerScreen trackId="trk-001" onChangeTrack={jest.fn()} />);
    const button = screen.getByTestId('download-button');
    fireEvent.click(button);
    expect(button).toHaveTextContent('Available offline ✓');
    expect(button).toBeDisabled();
    expect(trackEventMock.mock.calls.map((c) => c[0])).toEqual(['screen_view']);
  });
});

describe('Search (streampulse_ai_search)', () => {
  it('flag on: "AI search ✨" with explanations; search {query, engine: ai, results} attributed to the flag', () => {
    __setFlag(FLAG_KEYS.aiSearch, { isEnabled: true, reason: 'targeting_rule' });
    renderScreen(<SearchScreen />);

    expect(screen.getByRole('heading', { name: 'AI search ✨' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Search tracks'), { target: { value: 'late night drive' } });
    fireEvent.submit(screen.getByRole('search'));

    expect(screen.getAllByTestId('search-explanation').length).toBeGreaterThan(0);
    const [call] = callsFor('search');
    expect(call[1]).toMatchObject({ query: 'late night drive', engine: 'ai' });
    expect(call[1]?.results).toBeGreaterThan(0);
    expect(call[2]).toEqual({ featureFlagKey: FLAG_KEYS.aiSearch });
  });

  it('flag off: keyword search, no explanations, zero results for a mood query', () => {
    __setFlag(FLAG_KEYS.aiSearch, { isEnabled: false, reason: 'targeting_rule' });
    renderScreen(<SearchScreen />);

    expect(screen.getByRole('heading', { name: 'Search' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'late night drive' }));
    expect(screen.getByTestId('search-summary')).toHaveTextContent('0 results · keyword engine');
    expect(screen.queryByTestId('search-explanation')).not.toBeInTheDocument();
    expect(callsFor('search')[0][1]).toEqual({ query: 'late night drive', engine: 'keyword', results: 0 });
  });
});

describe('Notifications (streampulse_push_frequency)', () => {
  it('three_weekly variant: shows "3× a week"; buttons send notification_open / app_uninstall with the experiment key', () => {
    __setExperiment(EXPERIMENT_KEYS.pushFrequency, { variantName: 'three_weekly', isControl: false, configuration: { frequency: '3x_week' } });
    renderScreen(<NotificationsScreen />);

    expect(screen.getByTestId('push-frequency')).toHaveTextContent('3× a week');
    fireEvent.click(screen.getByRole('button', { name: 'Open notification' }));
    const [open] = callsFor('notification_open');
    expect(open[1]).toMatchObject({ frequency: '3x_week' });
    expect(open[2]).toEqual({ experimentKey: EXPERIMENT_KEYS.pushFrequency });

    fireEvent.click(screen.getByRole('button', { name: 'Uninstall app' }));
    const [uninstall] = callsFor('app_uninstall');
    expect(uninstall[1]).toEqual({ frequency: '3x_week' });
    expect(uninstall[2]).toEqual({ experimentKey: EXPERIMENT_KEYS.pushFrequency });
    expect(screen.getByRole('status')).toHaveTextContent('App uninstalled');
  });

  it('control (daily) and not-enrolled devices show the daily digest', () => {
    __setExperiment(EXPERIMENT_KEYS.pushFrequency, { variantName: 'daily', configuration: { frequency: 'daily' }, assigned: false, reason: 'holdout' });
    renderScreen(<NotificationsScreen />);
    expect(screen.getByTestId('push-frequency')).toHaveTextContent('Daily digest');
    expect(screen.getByText(/not enrolled — default shown/)).toBeInTheDocument();
  });
});

describe('Profile (streampulse_wrapped + streampulse_profile_badges)', () => {
  it('wrapped_2026: Wrapped card, wrapped_view on mount, share {surface: wrapped}', () => {
    __setExperiment(EXPERIMENT_KEYS.wrapped, { variantName: 'wrapped_2026', isControl: false, configuration: { wrapped: true } });
    renderScreen(<ProfileScreen deviceModel="iPhone 15" tier="premium" />);

    expect(screen.getByTestId('wrapped-card')).toBeInTheDocument();
    expect(callsFor('wrapped_view')).toHaveLength(1);
    expect(callsFor('wrapped_view')[0][2]).toEqual({ experimentKey: EXPERIMENT_KEYS.wrapped });
    fireEvent.click(screen.getByRole('button', { name: 'Share your Wrapped' }));
    const [share] = callsFor('share');
    expect(share[1]).toEqual({ surface: 'wrapped' });
    expect(share[2]).toEqual({ experimentKey: EXPERIMENT_KEYS.wrapped });
    expect(screen.queryByTestId('badge-row')).not.toBeInTheDocument();
  });

  it('badges variant: badge row and badge_tap with the experiment key; no Wrapped card, no wrapped_view', () => {
    __setExperiment(EXPERIMENT_KEYS.wrapped, { variantName: 'control', configuration: { wrapped: false }, assigned: false, reason: 'mutual_exclusion' });
    __setExperiment(EXPERIMENT_KEYS.profileBadges, { variantName: 'badges', isControl: false, configuration: { badges: true } });
    renderScreen(<ProfileScreen deviceModel="Pixel 7" tier="free" />);

    expect(screen.queryByTestId('wrapped-card')).not.toBeInTheDocument();
    expect(callsFor('wrapped_view')).toHaveLength(0);
    expect(screen.getByTestId('badge-row')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Badge: Night owl' }));
    const [tap] = callsFor('badge_tap');
    expect(tap[1]).toEqual({ badge: 'night-owl' });
    expect(tap[2]).toEqual({ experimentKey: EXPERIMENT_KEYS.profileBadges });
  });

  it('in neither experiment: plain profile that points at the panel when excluded', () => {
    __setExperiment(EXPERIMENT_KEYS.wrapped, { configuration: { wrapped: false }, assigned: false, reason: 'holdout' });
    __setExperiment(EXPERIMENT_KEYS.profileBadges, { configuration: { badges: false }, assigned: false, reason: 'holdout' });
    renderScreen(<ProfileScreen deviceModel="Pixel 7" tier="free" />);
    expect(screen.getByTestId('profile-plain')).toHaveTextContent('see the Controlvar panel for why');
  });
});

describe('Onboarding (streampulse_onboarding_steps)', () => {
  it('three_step: finishes after 3 steps → onboarding_complete {steps: 3} then first_play', () => {
    __setExperiment(EXPERIMENT_KEYS.onboardingSteps, { variantName: 'three_step', isControl: false, configuration: { steps: 3 } });
    renderScreen(<OnboardingScreen />);

    expect(screen.getByTestId('onboarding')).toHaveAttribute('data-steps', '3');
    expect(screen.getByTestId('onboarding-progress')).toHaveTextContent('Step 1 of 3');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(callsFor('onboarding_complete')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Finish' }));

    const [done] = callsFor('onboarding_complete');
    expect(done[1]).toEqual({ steps: 3 });
    expect(done[2]).toEqual({ experimentKey: EXPERIMENT_KEYS.onboardingSteps });

    fireEvent.click(screen.getByRole('button', { name: 'Play your first track' }));
    expect(callsFor('first_play')[0][2]).toEqual({ experimentKey: EXPERIMENT_KEYS.onboardingSteps });
  });

  it('five_step control: five steps before Finish', () => {
    __setExperiment(EXPERIMENT_KEYS.onboardingSteps, { variantName: 'five_step', configuration: { steps: 5 } });
    renderScreen(<OnboardingScreen />);
    expect(screen.getByTestId('onboarding')).toHaveAttribute('data-steps', '5');
    for (let i = 0; i < 4; i += 1) fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByTestId('onboarding-progress')).toHaveTextContent('Step 5 of 5');
    fireEvent.click(screen.getByRole('button', { name: 'Finish' }));
    expect(callsFor('onboarding_complete')[0][1]).toEqual({ steps: 5 });
  });
});

describe('Payments (streampulse_upsell_modal)', () => {
  it('value_modal: benefits-first dialog; Subscribe sends subscribe {plan, modal: value}', () => {
    __setExperiment(EXPERIMENT_KEYS.upsellModal, { variantName: 'value_modal', isControl: false, configuration: { modal: 'value' } });
    renderScreen(<PaymentsScreen tier="free" />);

    expect(screen.getByTestId('current-plan')).toHaveTextContent('Free with ads');
    fireEvent.click(screen.getByRole('button', { name: 'Go Premium' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAccessibleName('Everything you love, without the ads');
    fireEvent.click(within(dialog).getByLabelText(/Monthly/));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Subscribe' }));

    const [sub] = callsFor('subscribe');
    expect(sub[1]).toEqual({ plan: 'monthly', modal: 'value' });
    expect(sub[2]).toEqual({ experimentKey: EXPERIMENT_KEYS.upsellModal });
    expect(screen.getByTestId('current-plan')).toHaveTextContent('Premium · Monthly');
  });

  it('classic control: plain "Choose a plan" dialog, annual by default', () => {
    __setExperiment(EXPERIMENT_KEYS.upsellModal, { variantName: 'control', configuration: { modal: 'classic' } });
    renderScreen(<PaymentsScreen tier="premium" />);
    fireEvent.click(screen.getByRole('button', { name: 'Go Premium' }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Choose a plan');
    fireEvent.click(screen.getByRole('button', { name: 'Subscribe' }));
    expect(callsFor('subscribe')[0][1]).toEqual({ plan: 'annual', modal: 'classic' });
  });
});
