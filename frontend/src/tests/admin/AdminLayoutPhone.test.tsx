/**
 * At phone width the admin sidebar stacks above the content instead of taking
 * 224 px beside it. jsdom cannot lay anything out, so this pins the classes;
 * the admin-panel journey measures the real widths in a browser.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { AdminLayout } from '@/components/admin/AdminLayout';

jest.mock('next/link', () => {
  const MockLink = ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>{children}</a>
  );
  MockLink.displayName = 'MockLink';
  return MockLink;
});

function renderLayout() {
  render(
    <AdminLayout title="Users" currentPath="/admin/users">
      <div>Content</div>
    </AdminLayout>,
  );
}

const classes = (el: HTMLElement) => el.className.split(/\s+/);

describe('admin layout at phone width', () => {
  it('stacks the sidebar above the content below md and puts them side by side from md', () => {
    renderLayout();
    const layout = classes(screen.getByTestId('admin-layout'));
    expect(layout).toEqual(expect.arrayContaining(['flex', 'flex-col', 'md:flex-row']));
  });

  it('makes the sidebar full width below md and 14rem from md', () => {
    renderLayout();
    const sidebar = classes(screen.getByTestId('admin-sidebar'));
    expect(sidebar).toEqual(expect.arrayContaining(['w-full', 'md:w-56']));
    expect(sidebar).not.toContain('w-56');
  });

  it('wraps the links into rows below md and lists them in a column from md', () => {
    renderLayout();
    const nav = classes(screen.getByRole('navigation'));
    expect(nav).toEqual(expect.arrayContaining(['flex', 'flex-wrap', 'md:flex-col']));
  });

  it('marks the current page for assistive technology', () => {
    renderLayout();
    expect(screen.getByTestId('nav-item-users')).toHaveAttribute('aria-current', 'page');
    expect(screen.getByTestId('nav-item-safety')).not.toHaveAttribute('aria-current');
  });
});
