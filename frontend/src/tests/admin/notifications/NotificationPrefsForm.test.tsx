import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { NotificationPrefsForm } from '../../../components/admin/notifications/NotificationPrefsForm';
import { NotificationPreference } from '../../../types/admin';

const mockPrefs: NotificationPreference = {
  id: 'pref-001',
  user_id: 'user-001',
  notify_experiment_started: true,
  notify_experiment_completed: false,
  notify_safety_rollback: true,
  notify_rollout_advanced: false,
  slack_channel: '#general',
  email_override: 'user@example.com',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

describe('NotificationPrefsForm', () => {
  it('renders_form_with_checkboxes', () => {
    render(<NotificationPrefsForm prefs={mockPrefs} onSave={jest.fn()} />);
    expect(screen.getByTestId('notification-prefs-form')).toBeInTheDocument();
    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes.length).toBeGreaterThanOrEqual(4);
  });

  it('shows_all_four_event_toggles', () => {
    render(<NotificationPrefsForm prefs={mockPrefs} onSave={jest.fn()} />);
    expect(screen.getByTestId('pref-notify_experiment_started')).toBeInTheDocument();
    expect(screen.getByTestId('pref-notify_experiment_completed')).toBeInTheDocument();
    expect(screen.getByTestId('pref-notify_safety_rollback')).toBeInTheDocument();
    expect(screen.getByTestId('pref-notify_rollout_advanced')).toBeInTheDocument();
  });

  it('renders_slack_channel_input', () => {
    render(<NotificationPrefsForm prefs={mockPrefs} onSave={jest.fn()} />);
    const input = screen.getByTestId('slack-channel-input') as HTMLInputElement;
    expect(input).toBeInTheDocument();
    expect(input.value).toBe('#general');
  });

  it('renders_email_override_input', () => {
    render(<NotificationPrefsForm prefs={mockPrefs} onSave={jest.fn()} />);
    const input = screen.getByTestId('email-override-input') as HTMLInputElement;
    expect(input).toBeInTheDocument();
    expect(input.value).toBe('user@example.com');
  });

  it('slack_channel_label_names_its_input', () => {
    render(<NotificationPrefsForm prefs={mockPrefs} onSave={jest.fn()} />);
    expect(screen.getByLabelText('Slack Channel Override')).toBe(screen.getByTestId('slack-channel-input'));
  });

  it('email_override_label_names_its_input', () => {
    render(<NotificationPrefsForm prefs={mockPrefs} onSave={jest.fn()} />);
    expect(screen.getByLabelText('Email Override')).toBe(screen.getByTestId('email-override-input'));
  });

  it('checkbox_toggle_updates_state', () => {
    render(<NotificationPrefsForm prefs={mockPrefs} onSave={jest.fn()} />);
    const checkbox = screen.getByTestId('pref-notify_experiment_completed') as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
    fireEvent.click(checkbox);
    expect(checkbox.checked).toBe(true);
  });

  it('save_button_calls_on_save', async () => {
    const onSave = jest.fn().mockResolvedValue(undefined);
    render(<NotificationPrefsForm prefs={mockPrefs} onSave={onSave} />);
    const form = screen.getByTestId('notification-prefs-form');
    fireEvent.submit(form);
    await waitFor(() => {
      expect(onSave).toHaveBeenCalledTimes(1);
    });
  });

  it('save_button_shows_saving_state', async () => {
    let resolve: () => void;
    const onSave = jest.fn().mockReturnValue(new Promise<void>(r => { resolve = r; }));
    render(<NotificationPrefsForm prefs={mockPrefs} onSave={onSave} />);
    const btn = screen.getByTestId('save-prefs-btn');
    fireEvent.click(btn);
    await waitFor(() => {
      expect(screen.getByTestId('save-prefs-btn')).toHaveTextContent('Saving...');
    });
    await act(async () => { resolve!(); });
  });

  it('form_shows_saved_confirmation', async () => {
    const onSave = jest.fn().mockResolvedValue(undefined);
    render(<NotificationPrefsForm prefs={mockPrefs} onSave={onSave} />);
    const btn = screen.getByTestId('save-prefs-btn');
    fireEvent.click(btn);
    await waitFor(() => {
      expect(screen.getByTestId('save-prefs-btn')).toHaveTextContent('Saved!');
    });
  });
});
