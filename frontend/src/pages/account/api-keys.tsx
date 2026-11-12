import React from 'react';
import { PageTitle } from '@/components/PageTitle';
import { OwnApiKeys } from '@/components/account/OwnApiKeys';
import { DemoNote } from '@/components/DemoNote';
import { useDemoMode } from '@/contexts/AuthConfigContext';

/**
 * Your own API keys, for every signed-in user. There is no admin guard: the
 * route is protected like every dashboard page (`routeKind`), and the API
 * scopes each call to the caller's own keys. A read-only demo refuses keys,
 * so there the page says so instead.
 */
export default function OwnApiKeysPage() {
  const demo = useDemoMode();
  if (demo) {
    return (
      <div className="flex-1 bg-slate-50">
        <PageTitle title="Your API keys" />
        <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          <h1 className="text-2xl font-bold text-slate-900">Your API keys</h1>
          <DemoNote action="Creating and changing API keys" walkthroughs={false} className="mt-4 text-sm text-slate-700" />
        </div>
      </div>
    );
  }
  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="Your API keys" />
      <OwnApiKeys />
    </div>
  );
}
