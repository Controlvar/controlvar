import React, { createContext, ReactNode, useContext, useEffect, useMemo, useState } from 'react';
import { RbacService } from '@modules/rbac';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { useModules } from '@/contexts/ModulesContext';
import { MODULES } from '@/services/modules';
import { EffectivePermissions, effectiveFromAnswer } from '@/utils/effectivePermissions';

/**
 * The signed-in user's effective permissions (#891), for the permission
 * helpers in `@/utils/experimentPermissions` and friends.
 *
 * `GET /api/v1/rbac/users/{me}/permissions` is a route of the rbac module, so
 * it is asked only when `/api/v1/modules` lists `rbac`; in the core profile
 * the `@modules/rbac` stub throws, and no request is ever sent. A superuser
 * passes every check before any map is read, so none is asked for one.
 *
 * The map is asked again on every navigation (`navigationKey`, the path the
 * app shell is on), so a grant or a revoke reaches the dashboard by the next
 * page. Until then the API decides, and a control the dashboard still shows
 * gets the API's own 403. While a new answer is on its way the last one
 * stays, so a control does not blink on each navigation.
 *
 * `loading` is true until the first answer for the current user (and while
 * the modules probe has not said whether rbac is installed): a page shows
 * neither the control nor the refusal note then, and the control once the
 * answer admits it. A failed request counts as an answer with no map: only
 * the role lists apply (fail closed).
 */
export interface EffectivePermissionsValue {
  /** The current user's map, or `undefined`: only the role lists apply. */
  effective: EffectivePermissions | undefined;
  /** True while an answer that could widen the role lists may still come. */
  loading: boolean;
}

/** Outside the provider, a core profile, no user, a superuser: role lists only. */
export const ROLE_LISTS_ONLY: EffectivePermissionsValue = { effective: undefined, loading: false };

const PENDING: EffectivePermissionsValue = { effective: undefined, loading: true };

const EffectivePermissionsContext = createContext<EffectivePermissionsValue>(ROLE_LISTS_ONLY);

export interface EffectivePermissionsProviderProps {
  children: ReactNode;
  /** Changes on every navigation; each change asks for the map again. */
  navigationKey?: string;
}

interface Answer {
  userId: string;
  effective: EffectivePermissions | undefined;
}

export function EffectivePermissionsProvider({ children, navigationKey }: EffectivePermissionsProviderProps) {
  const auth = useOptionalAuth();
  const user = auth?.user ?? null;
  const userId = user && user.is_superuser !== true ? user.id : null;
  const { isLoading: modulesLoading, hasModule } = useModules();
  const rbac = !modulesLoading && hasModule(MODULES.RBAC);
  const [answer, setAnswer] = useState<Answer | null>(null);

  useEffect(() => {
    if (!rbac || userId === null) return undefined;
    let current = true;
    RbacService.getUserPermissions(userId).then(
      (body) => {
        if (current) setAnswer({ userId, effective: effectiveFromAnswer(body) });
      },
      () => {
        if (current) setAnswer({ userId, effective: undefined });
      },
    );
    return () => {
      current = false;
    };
  }, [rbac, userId, navigationKey]);

  const value = useMemo<EffectivePermissionsValue>(() => {
    if (userId === null) return ROLE_LISTS_ONLY;
    if (modulesLoading) return PENDING;
    if (!rbac) return ROLE_LISTS_ONLY;
    if (answer === null || answer.userId !== userId) return PENDING;
    return { effective: answer.effective, loading: false };
  }, [userId, modulesLoading, rbac, answer]);

  return <EffectivePermissionsContext.Provider value={value}>{children}</EffectivePermissionsContext.Provider>;
}

/** The current user's effective map and whether it is still on its way. */
export function useEffectivePermissions(): EffectivePermissionsValue {
  return useContext(EffectivePermissionsContext);
}

export default EffectivePermissionsProvider;
