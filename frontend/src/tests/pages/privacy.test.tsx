/**
 * The privacy page makes claims about the marketing site. These tests pin the
 * ones a code change could make false, so that change has to touch this page.
 */
import React from 'react';
import fs from 'fs';
import path from 'path';
import { render, screen } from '@testing-library/react';
import PrivacyPage from '@/pages/privacy';

jest.mock('next/head', () => { const H=({children}:{children:React.ReactNode})=><>{children}</>; H.displayName='H'; return H; });

describe('/privacy', () => {
  it('gives the contact address as a mailto link', () => {
    render(<PrivacyPage />);
    expect(screen.getByRole('link', { name: 'hello@controlvar.com' }))
      .toHaveAttribute('href', 'mailto:hello@controlvar.com');
  });

  it('says the site sets no cookies and runs no analytics', () => {
    render(<PrivacyPage />);
    expect(screen.getByText(/sets no cookies and runs no analytics/)).toBeInTheDocument();
  });

  // The page says "no cookies, no analytics". If someone adds a tracking
  // script or a cookie to the frontend, this fails and points them here.
  it('is still true of the frontend sources', () => {
    const SRC = path.resolve(__dirname, '..', '..');
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, e.name);
        if (e.isDirectory()) { if (e.name !== 'tests' && e.name !== '__tests__') walk(p); }
        else if (/\.(tsx?|jsx?|css)$/.test(e.name)) files.push(p);
      }
    };
    walk(SRC);
    expect(files.length).toBeGreaterThan(50);
    const PATTERN = /gtag|googletagmanager|google-analytics|plausible|posthog|segment\.(io|com)|amplitude|mixpanel|hotjar|document\.cookie/i;
    const hits = files.filter((f) => PATTERN.test(fs.readFileSync(f, 'utf8')))
      .map((f) => path.relative(SRC, f));
    expect(hits).toEqual([]);
  });
});
