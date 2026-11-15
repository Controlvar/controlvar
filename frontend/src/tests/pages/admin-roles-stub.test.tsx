/**
 * The core profile's /admin/roles stub (#891): it says what custom roles and
 * direct grants do in the full profile, which is to add to the built-in role.
 * It renders the stub file itself, so it runs in both profiles' Jest.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import RolesStubPage from '@/modules-stub/pages/admin/roles';

jest.mock('next/router', () => ({
  useRouter: () => ({ isReady: true, asPath: '/admin/roles', pathname: '/admin/roles', replace: jest.fn() }),
}));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

jest.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    status: 'authenticated',
    afterLogoutPath: null,
    user: { id: 'a1', email: 'admin@example.com', username: 'admin', role: 'ADMIN', is_superuser: true },
  }),
}));

describe('/admin/roles in the core profile', () => {
  it("says custom roles and direct grants add to the built-in role", () => {
    render(<RolesStubPage />);
    expect(
      screen.getByText(
        "Define roles beyond the four built-in ones and grant permissions directly to a user. Both add to what the user's built-in role allows.",
      ),
    ).toBeInTheDocument();
  });
});
