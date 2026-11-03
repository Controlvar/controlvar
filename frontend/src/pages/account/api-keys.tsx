import React from 'react';
import { PageTitle } from '@/components/PageTitle';
import { OwnApiKeys } from '@/components/account/OwnApiKeys';

/**
 * Your own API keys, for every signed-in user. There is no admin guard: the
 * route is protected like every dashboard page (`routeKind`), and the API
 * scopes each call to the caller's own keys.
 */
export default function OwnApiKeysPage() {
  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="Your API keys" />
      <OwnApiKeys />
    </div>
  );
}
