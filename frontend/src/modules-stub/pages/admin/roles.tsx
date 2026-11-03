import { modulePageStub } from '@/components/ModuleNotice';
import { withAdminGuard } from '@/components/admin/withAdminGuard';
import { MODULES } from '@/services/modules';

// Guarded like the page it stands in for: /admin/roles is an admin route in
// every profile, and the notice must not be the one admin page a VIEWER can
// open.
export default withAdminGuard(
  modulePageStub({
    title: 'Custom Roles',
    module: MODULES.RBAC,
    description:
      'Define roles beyond the four built-in ones and grant permissions directly to a user. ' +
      "Both add to what the user's built-in role allows.",
  }),
  { requiredRole: 'ADMIN' },
);
