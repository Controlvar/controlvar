import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { NotificationsAdminPage } from '../../../pages/admin/notifications';
import { AdminService } from '../../../services/admin';
import { NotificationPreference, NotificationDeliveryLogListResponse } from '../../../types/admin';

jest.mock('../../../services/admin');

// The page reads who is signed in: a superuser who is an ADMIN (the one person
// the page's reads and the test button all accept; #2277).
jest.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    status: 'authenticated',
    user: { id: 'user-1', role: 'ADMIN', is_superuser: true },
  }),
}));

jest.mock('../../../components/admin/notifications/NotificationPrefsForm', () => ({
  NotificationPrefsForm: ({
    prefs,
    onSave,
  }: {
    prefs: NotificationPreference;
    onSave: (data: Partial<NotificationPreference>) => Promise<void>;
  }) => (
    <div data-testid="notification-prefs-form">
      Notification Prefs Form
    </div>
  ),
}));

jest.mock('../../../components/admin/notifications/DeliveryLogTable', () => ({
  DeliveryLogTable: ({
    logs,
    loading,
  }: {
    logs: unknown[];
    loading?: boolean;
  }) => (
    <div data-testid="delivery-log-table">
      {loading ? 'Loading...' : `${logs.length} logs`}
    </div>
  ),
}));

const mockPrefs: NotificationPreference = {
  id: 'pref-001',
  user_id: 'user-001',
  notify_experiment_started: true,
  notify_experiment_completed: true,
  notify_safety_rollback: true,
  notify_rollout_advanced: false,
  slack_channel: '#alerts',
  email_override: null,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const mockLogResponse: NotificationDeliveryLogListResponse = {
  items: [
    {
      id: 'log-001',
      event_type: 'experiment_started',
      channel: 'slack',
      recipient: '#alerts',
      subject: null,
      status: 'sent',
      error_message: null,
      created_at: '2024-01-01T10:00:00Z',
    },
  ],
  total: 1,
  page: 1,
  limit: 20,
};

const mockGetMyNotificationPrefs = AdminService.getMyNotificationPrefs as jest.Mock;
const mockGetNotificationDeliveryLog = AdminService.getNotificationDeliveryLog as jest.Mock;
const mockSendTestNotification = AdminService.sendTestNotification as jest.Mock;
const mockGetAllNotificationPrefs = AdminService.getAllNotificationPrefs as jest.Mock;

beforeEach(() => {
  jest.clearAllMocks();
  mockGetMyNotificationPrefs.mockResolvedValue(mockPrefs);
  mockGetAllNotificationPrefs.mockResolvedValue([]);
  mockGetNotificationDeliveryLog.mockResolvedValue(mockLogResponse);
  mockSendTestNotification.mockResolvedValue({ success: true, channel: 'slack', message: 'Test notification sent' });
});

describe('NotificationsAdminPage', () => {
  it('renders_page_title', async () => {
    render(<NotificationsAdminPage />);
    await waitFor(() => {
      expect(screen.getByTestId('notifications-admin-page')).toBeInTheDocument();
    });
    expect(screen.getByRole('heading', { level: 1, name: 'Notifications' })).toBeInTheDocument();
  });

  it('renders_my_preferences_section', async () => {
    render(<NotificationsAdminPage />);
    await waitFor(() => {
      expect(screen.getByTestId('notification-prefs-form')).toBeInTheDocument();
    });
    expect(screen.getByText('My Preferences')).toBeInTheDocument();
  });

  it('renders_delivery_log_section', async () => {
    render(<NotificationsAdminPage />);
    await waitFor(() => {
      expect(screen.getByTestId('delivery-log-table')).toBeInTheDocument();
    });
    expect(screen.getByText('Delivery Log')).toBeInTheDocument();
  });

  it('renders_send_test_button', async () => {
    render(<NotificationsAdminPage />);
    await waitFor(() => {
      expect(screen.getByTestId('send-test-btn')).toBeInTheDocument();
    });
  });

  it('shows_loading_state_before_data_loads', () => {
    mockGetMyNotificationPrefs.mockImplementation(() => new Promise(() => {}));
    mockGetNotificationDeliveryLog.mockImplementation(() => new Promise(() => {}));
    render(<NotificationsAdminPage />);
    expect(screen.getByTestId('prefs-loading')).toBeInTheDocument();
  });

  it('shows_the_error_in_place_of_loading_and_an_empty_log_when_a_read_fails', async () => {
    mockGetMyNotificationPrefs.mockRejectedValue(new Error('prefs read failed'));
    mockGetNotificationDeliveryLog.mockRejectedValue(new Error('log read failed'));
    render(<NotificationsAdminPage />);
    await waitFor(() => {
      expect(screen.getByTestId('prefs-error')).toHaveTextContent('prefs read failed');
    });
    expect(screen.getByTestId('log-error')).toHaveTextContent('log read failed');
    expect(screen.queryByTestId('prefs-loading')).not.toBeInTheDocument();
    expect(screen.queryByTestId('delivery-log-table')).not.toBeInTheDocument();
  });

  it('send_test_button_shows_result_message', async () => {
    render(<NotificationsAdminPage />);
    await waitFor(() => {
      expect(screen.getByTestId('send-test-btn')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('send-test-btn'));
    await waitFor(() => {
      expect(screen.getByTestId('test-result')).toBeInTheDocument();
      expect(screen.getByTestId('test-result')).toHaveTextContent('Test notification sent!');
    });
  });
});
