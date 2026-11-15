/**
 * The local sign-in form, pinned as it was before the Cognito Hosted UI
 * sign-in (#708): the snapshot was written from the page before that change,
 * and the Cognito tests compare every fallback to this same render.
 */
import React from 'react';
import { render } from '@testing-library/react';
import LoginPage from '@/pages/login';
import { AuthProvider } from '@/contexts/AuthContext';

jest.mock('next/router', () => ({
  useRouter: () => ({
    replace: jest.fn().mockResolvedValue(true),
    push: jest.fn(),
    pathname: '/login',
    asPath: '/login',
    query: {},
    isReady: true,
  }),
}));

jest.mock('next/head', () => {
  const MockHead = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  MockHead.displayName = 'MockHead';
  return MockHead;
});

/**
 * The markup with React's generated ids named by their order (the same
 * function as hostedUiHarness's; this file imports nothing the change added,
 * so it runs on the page from before it too).
 */
function normalisedMarkup(container: HTMLElement): string {
  const ids = new Map<string, string>();
  return container.innerHTML.replace(/_r_[0-9a-z]+_|[«:]r[0-9a-z]+[»:]/g, (id) => {
    if (!ids.has(id)) ids.set(id, `id-${ids.size + 1}`);
    return ids.get(id) as string;
  });
}

beforeEach(() => {
  global.fetch = jest.fn(() => new Promise<Response>(() => undefined)) as unknown as typeof fetch;
  localStorage.clear();
});

it('the local form is the one from before the Cognito sign-in', () => {
  const { container } = render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
  expect(normalisedMarkup(container)).toMatchSnapshot();
});
