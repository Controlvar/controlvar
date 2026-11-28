/**
 * Send Test on the admin Notifications page is offered only to those
 * `POST /api/v1/notifications/test` accepts: role ADMIN or DEVELOPER, with or
 * without the superuser flag. Anyone else reads one line saying so instead of
 * a button that answers 403.
 */
import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { NotificationsAdminPage, SEND_TEST_REFUSED } from '@/pages/admin/notifications';
import { sendsTestNotification } from '@/components/admin/notifications/access';
import { AdminService } from '@/services/admin';

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
