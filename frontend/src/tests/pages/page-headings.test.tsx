/**
 * Heading structure: each page has exactly one h1 in every state it shows.
 *
 * The Users page used to carry two (the admin layout's and its own), and the
 * results pages and the 403 view had none.
 */
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { UserManagementPage } from '@/pages/admin/users';
import ResultDetailPage from '@/pages/results/[id]';
import { ForbiddenView } from '@/components/RequireAuth';
import { AdminService } from '@/services/admin';
import { apiFetch } from '@/services/api';
import { makeRouter } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));
jest.mock('@/services/admin');

const mockRouter = makeRouter({ pathname: '/results/[id]', query: { id: 'exp-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));
jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});
jest.mock('next/link', () => {
  const MockLink = ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  );
  MockLink.displayName = 'MockLink';
  return MockLink;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;
const mockedAdmin = AdminService as jest.Mocked<typeof AdminService>;

const h1s = () => screen.queryAllByRole('heading', { level: 1 });

describe('the Users page', () => {
  it('has exactly one h1, the layout heading', async () => {
    mockedAdmin.listUsers.mockResolvedValue({ items: [], total: 0, page: 1, limit: 20 } as never);
    render(<UserManagementPage />);
    await screen.findByTestId('user-management-page');
    expect(h1s()).toHaveLength(1);
    expect(h1s()[0]).toHaveTextContent('User Management');
  });
});

describe('the results page', () => {
  it('has one h1 while loading, on error and once loaded', async () => {
    // Never answers: the page stays on its loading state.
    mockedApiFetch.mockImplementation(() => new Promise(() => {}));
    const view = render(<ResultDetailPage />);
    expect(h1s()).toHaveLength(1);
    view.unmount();

    mockedApiFetch.mockRejectedValue(new Error('boom'));
    render(<ResultDetailPage />);
    await screen.findByTestId('error-state');
    expect(h1s()).toHaveLength(1);
  });

  it('has one h1 before the route knows the experiment id', () => {
    const noId = makeRouter({ pathname: '/results/[id]', query: {} });
    Object.assign(mockRouter, noId);
    render(<ResultDetailPage />);
    expect(h1s()).toHaveLength(1);
    Object.assign(mockRouter, makeRouter({ pathname: '/results/[id]', query: { id: 'exp-1' } }));
  });
});

describe('the 403 view', () => {
  it('has exactly one h1, the refusal', async () => {
    render(<ForbiddenView user={{ email: 'a@b.test', role: 'VIEWER' }} requirement="a superuser account" />);
    await waitFor(() => expect(h1s()).toHaveLength(1));
    expect(h1s()[0]).toHaveTextContent('You do not have permission to access this page.');
  });
});
