import React from 'react';
import { AdminSidebar } from './AdminSidebar';
import { PageTitle } from '@/components/PageTitle';

interface AdminLayoutProps {
  children: React.ReactNode;
  title: string;
  currentPath: string;
}

/**
 * Admin area layout: sidebar + content column (the sidebar stacks above the content below `md`). The top navigation comes from
 * the application shell (`AppShell`), so this only adds the section heading.
 */
export function AdminLayout({ children, title, currentPath }: AdminLayoutProps) {
  return (
    <div data-testid="admin-layout" className="flex-1 bg-slate-50 flex flex-col md:flex-row">
      <PageTitle title={`${title} · Admin`} />
      <AdminSidebar currentPath={currentPath} />

      {/* Main Content Area */}
      <main className="flex-1 min-w-0 overflow-auto p-6">
        <header className="flex items-center gap-3 mb-6">
          <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">Admin</span>
          <span className="text-slate-300">/</span>
          <h1 className="text-lg font-semibold text-slate-900">{title}</h1>
        </header>
        {children}
      </main>
    </div>
  );
}
