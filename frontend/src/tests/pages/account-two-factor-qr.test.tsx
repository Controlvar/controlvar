/**
 * The QR image on `/account/two-factor` (#1812) encodes the secret shown
 * beside it, and no other: a QR still being drawn for a secret the user has
 * since replaced (Cancel, then Set up again) never stands in for the new one.
 */
import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import TwoFactorPage from '@/pages/account/two-factor';

const mockAuth = jest.fn();
jest.mock('@/contexts/AuthContext', () => ({ useOptionalAuth: () => mockAuth() }));
jest.mock('@/contexts/AuthConfigContext', () => ({ useDemoMode: () => false }));
jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

/** Each call's promise, settled by the test in the order it chooses. */
const pending: { uri: string; resolve: (src: string) => void }[] = [];
jest.mock('@/services/twoFactor', () => ({
  ...jest.requireActual('@/services/twoFactor'),
  qrCodeDataUrl: (uri: string) =>
    new Promise<string>((resolve) => {
      pending.push({ uri, resolve });
    }),
}));

const mockFetch = jest.fn();
global.fetch = mockFetch;

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

const uri = (secret: string) => `otpauth://totp/Controlvar:u@example.com?secret=${secret}&issuer=Controlvar`;

beforeEach(() => {
  pending.length = 0;
  mockFetch.mockReset();
  mockAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 'u1', email: 'u@example.com', username: 'u', full_name: null, role: 'VIEWER', is_superuser: false, is_active: true, auth_provider: 'local' },
  });
});

it('shows only the QR drawn for the secret on the page', async () => {
  mockFetch.mockResolvedValueOnce(response({ enabled: false, required: false, recovery_codes_remaining: 0 }));
  render(<TwoFactorPage />);
  await screen.findByTestId('two-factor-off');

  // Each set-up asks for the current password; it is cleared once answered.
  const typePassword = () =>
    fireEvent.change(screen.getByTestId('two-factor-current-password'), { target: { value: 'Current-Passw0rd' } });
  typePassword();
  mockFetch.mockResolvedValueOnce(response({ secret: 'AAAAAAAA', otpauth_uri: uri('AAAAAAAA') }));
  await act(async () => {
    fireEvent.click(screen.getByTestId('two-factor-start'));
  });
  fireEvent.click(screen.getByTestId('two-factor-cancel'));
  typePassword();
  mockFetch.mockResolvedValueOnce(response({ secret: 'BBBBBBBB', otpauth_uri: uri('BBBBBBBB') }));
  await act(async () => {
    fireEvent.click(screen.getByTestId('two-factor-start'));
  });
  expect(screen.getByTestId('two-factor-key')).toHaveTextContent('BBBB BBBB');
  expect(pending.map((p) => p.uri)).toEqual([uri('AAAAAAAA'), uri('BBBBBBBB')]);

  // The new secret's QR arrives first, then the old one's.
  await act(async () => {
    pending[1].resolve('data:image/gif;base64,QkJCQg==');
  });
  await act(async () => {
    pending[0].resolve('data:image/gif;base64,QUFBQQ==');
  });
  expect(screen.getByTestId('two-factor-qr')).toHaveAttribute('src', 'data:image/gif;base64,QkJCQg==');
});
