/**
 * Send Test on the admin Notifications page is offered only to those
 * `POST /api/v1/notifications/test` accepts: role ADMIN or DEVELOPER, with or
 * without the superuser flag. Anyone else reads one line saying so instead of
 * a button that answers 403.
 */
import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { NotificationsAdminPage, SEND_TEST_REFUSED } from '@/pages/admin/notifications';
import { sendsTestNotification } from '@/components/admin/notifications/access';
import { AdminService } from '@/services/admin';
import { ApiError } from '@/services/api';

jest.mock('@/services/admin');

const mockUseAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const mockedAdmin = AdminService as jest.Mocked<typeof AdminService>;

beforeEach(() => {
  mockedAdmin.getMyNotificationPrefs.mockResolvedValue({} as never);
  mockedAdmin.getNotificationDeliveryLog.mockResolvedValue({ items: [], total: 0 } as never);
  mockedAdmin.getAllNotificationPrefs.mockResolvedValue([] as never);
});

async function renderAs(role: string, is_superuser: boolean) {
  mockUseAuth.mockReturnValue({ user: { id: 'u-1', role, is_superuser }, status: 'authenticated' });
  render(<NotificationsAdminPage />);
  await act(async () => {});
}

describe.each([
  ['ADMIN', true, true],
  ['ADMIN', false, true],
  ['DEVELOPER', true, true],
  ['DEVELOPER', false, true],
  ['ANALYST', true, false],
  ['ANALYST', false, false],
  ['VIEWER', true, false],
  ['VIEWER', false, false],
])('role %s, superuser %s', (role, superuser, offered) => {
  it(offered ? 'is offered Send Test and no note' : 'is told why there is no Send Test button', async () => {
    await renderAs(role, superuser);
    expect(sendsTestNotification({ role } as never)).toBe(offered);
    if (offered) {
      expect(screen.getByTestId('send-test-btn')).toBeInTheDocument();
      expect(screen.queryByTestId('send-test-refused')).not.toBeInTheDocument();
    } else {
      expect(screen.queryByTestId('send-test-btn')).not.toBeInTheDocument();
      expect(screen.getByTestId('send-test-refused')).toHaveTextContent(SEND_TEST_REFUSED);
    }
  });
});

it('offers nothing before the signed-in user is known', async () => {
  mockUseAuth.mockReturnValue({ user: null, status: 'loading' });
  render(<NotificationsAdminPage />);
  await act(async () => {});
  expect(screen.queryByTestId('send-test-btn')).not.toBeInTheDocument();
});

describe('what Send Test reports', () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  async function clickSendTest() {
    await renderAs('ADMIN', true);
    await act(async () => {
      fireEvent.click(screen.getByTestId('send-test-btn'));
    });
  }

  it('shows the reason the API gave when the request is refused', async () => {
    mockedAdmin.sendTestNotification.mockRejectedValue(
      new ApiError({ status: 429, detail: 'Rate limit exceeded' }),
    );
    await clickSendTest();
    expect(screen.getByTestId('test-result')).toHaveTextContent('Rate limit exceeded');
  });

  it('says what to check when the API answers success: false', async () => {
    mockedAdmin.sendTestNotification.mockResolvedValue({
      success: false,
      channel: 'slack',
      message: 'Test from Admin UI',
    });
    await clickSendTest();
    const shown = screen.getByTestId('test-result');
    expect(shown).toHaveTextContent('not sent');
    expect(shown).toHaveTextContent('SLACK_ENABLED');
  });

  it('keeps the outcome on screen and announces it politely', async () => {
    mockedAdmin.sendTestNotification.mockResolvedValue({
      success: true,
      channel: 'slack',
      message: 'Test from Admin UI',
    });
    await clickSendTest();
    const shown = screen.getByTestId('test-result');
    expect(shown).toHaveAttribute('role', 'status');
    expect(shown).toHaveTextContent('Test notification sent!');
    act(() => {
      jest.advanceTimersByTime(60_000);
    });
    expect(screen.getByTestId('test-result')).toHaveTextContent('Test notification sent!');
  });

  it('replaces the outcome when Send Test is pressed again', async () => {
    mockedAdmin.sendTestNotification.mockRejectedValueOnce(
      new ApiError({ status: 429, detail: 'Rate limit exceeded' }),
    );
    await clickSendTest();
    expect(screen.getByTestId('test-result')).toHaveTextContent('Rate limit exceeded');
    mockedAdmin.sendTestNotification.mockResolvedValueOnce({
      success: true,
      channel: 'slack',
      message: 'Test from Admin UI',
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('send-test-btn'));
    });
    expect(screen.getByTestId('test-result')).toHaveTextContent('Test notification sent!');
    expect(screen.getByTestId('test-result')).not.toHaveTextContent('Rate limit exceeded');
  });
});

describe('the admin layout', () => {
  it('wraps the page, with the sidebar and the Notifications link marked as the current page', async () => {
    await renderAs('ADMIN', true);
    expect(screen.getByTestId('admin-layout')).toContainElement(
      screen.getByTestId('notifications-admin-page'),
    );
    expect(screen.getByTestId('admin-sidebar')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'Notifications' })).toBeInTheDocument();
  });
});
