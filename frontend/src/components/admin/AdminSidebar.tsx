import React from 'react';
import Link from 'next/link';
import { useModules } from '@/contexts/ModulesContext';
import { MODULES, MODULES_DOC_PATH, ModulesInfo, moduleInstalled } from '@/services/modules';

interface NavItem {
  label: string;
  href: string;
  testId: string;
  icon: string;
  /**
   * Module this item needs. Items without one are core and always render.
   * `/admin/roles` belongs to the `rbac` module, so it is listed only when
   * that module is installed: core chrome must not hard-link a route the core
   * profile does not serve.
   */
  module?: string;
}

export const NAV_ITEMS: NavItem[] = [
  { label: 'Dashboard', href: '/admin', testId: 'nav-item-dashboard', icon: '📊' },
  { label: 'Users', href: '/admin/users', testId: 'nav-item-users', icon: '👥' },
  {
    label: 'Roles',
    href: '/admin/roles',
    testId: 'nav-item-roles',
    icon: '🔑',
    module: MODULES.RBAC,
  },
  { label: 'Audit Log', href: '/admin/audit', testId: 'nav-item-audit', icon: '📋' },
  { label: 'Safety', href: '/admin/safety', testId: 'nav-item-safety', icon: '🛡️' },
  { label: 'Scheduler', href: '/admin/scheduler', testId: 'nav-item-scheduler', icon: '⏰' },
  { label: 'API Keys', href: '/admin/api-keys', testId: 'nav-item-api-keys', icon: '🗝️' },
  { label: 'Notifications', href: '/admin/notifications', testId: 'nav-item-notifications', icon: '🔔' },
];

/** Core items, plus any module item whose module is installed. */
export function visibleNavItems(info: ModulesInfo, items: NavItem[] = NAV_ITEMS): NavItem[] {
  return items.filter((item) => !item.module || moduleInstalled(info, item.module));
}

interface AdminSidebarProps {
  currentPath: string;
}

export function AdminSidebar({ currentPath }: AdminSidebarProps) {
  const { profile, modules, version, isLoading, error } = useModules();
  const items = visibleNavItems({ profile, modules, version });
  // Neither while the probe is outstanding -- the note would say an admin page
  // is missing on a full-profile instance and then vanish when the answer came
  // -- nor when the probe *failed*: a failed probe resolves to the core
  // profile, so the count would be right for a reason that is wrong, and the
  // note would tell an operator their instance has no `rbac` module when all
  // that happened was a 502. The separate note below says that instead.
  const probeFailed = !isLoading && error !== null;
  const hiddenModulePages = isLoading || probeFailed ? 0 : NAV_ITEMS.length - items.length;

  return (
    <aside
      data-testid="admin-sidebar"
      className="w-full md:w-56 flex-shrink-0 bg-white border-b md:border-b-0 md:border-r border-slate-200 flex flex-col"
    >
      <div className="px-4 py-3 md:py-6">
        <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2 md:mb-4">
          Navigation
        </p>
        <nav className="flex flex-row flex-wrap gap-1 md:flex-col">
          {items.map((item) => {
            const isActive = currentPath === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                data-testid={item.testId}
                aria-current={isActive ? 'page' : undefined}
                className={[
                  'flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-blue-50 text-blue-700 border-r-2 border-blue-600'
                    : 'text-slate-600 hover:bg-slate-50',
                ].join(' ')}
              >
                <span role="img" aria-hidden="true">{item.icon}</span>
                {item.label}
              </Link>
            );
          })}
        </nav>

        {hiddenModulePages > 0 && (
          <p data-testid="admin-sidebar-modules-note" className="mt-6 text-xs text-slate-500">
            {hiddenModulePages === 1
              ? 'One admin page belongs to a module that is not installed.'
              : `${hiddenModulePages} admin pages belong to modules that are not installed.`}{' '}
            <a href={MODULES_DOC_PATH} target="_blank" rel="noreferrer" className="underline hover:text-slate-600">
              Modules
            </a>
          </p>
        )}

        {probeFailed && (
          <p data-testid="admin-sidebar-modules-error" className="mt-6 text-xs text-slate-500">
            The dashboard could not reach the API to check which modules are installed, so any
            module pages are hidden for now.
          </p>
        )}
      </div>
    </aside>
  );
}
