/**
 * Every page the admin sidebar links to is drawn inside `AdminLayout`, so
 * following a sidebar link never drops the sidebar. `/admin/notifications`
 * was the one page that rendered on its own.
 */
import fs from 'fs';
import path from 'path';
import { NAV_ITEMS } from '@/components/admin/AdminSidebar';

const PAGES = path.join(__dirname, '..', '..', 'pages');

function pageFile(href: string): string {
  const base = path.join(PAGES, href);
  for (const candidate of [`${base}.tsx`, path.join(base, 'index.tsx')]) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error(`no page file for ${href}`);
}

// A module's page (`/admin/roles`) is a re-export of the module's own file.
describe('admin sidebar pages', () => {
  it.each(NAV_ITEMS.filter((item) => !item.module).map((item) => [item.href]))('%s is drawn inside AdminLayout', (href) => {
    const source = fs.readFileSync(pageFile(href), 'utf8');
    expect(source).toMatch(/import \{ AdminLayout \} from '@\/components\/admin\/AdminLayout'/);
    expect(source).toMatch(/<AdminLayout\b/);
  });
});
