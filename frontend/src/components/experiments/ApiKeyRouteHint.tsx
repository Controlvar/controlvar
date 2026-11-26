import React from 'react';
import Link from 'next/link';
import { docsUrl } from '@/services/docs';

/**
 * The dashboard page where any signed-in user creates an API key for
 * themselves (More → Your API keys). Admin → API Keys (/admin/api-keys) opens
 * only for a superuser (withAdminGuard), so everyone else is pointed here
 * instead (#920, #1974).
 */
export const OWN_API_KEYS_PATH = '/account/api-keys';

/**
 * One sentence telling a reader who cannot open Admin → API Keys how to get a
 * key. Inline, so it can end a sentence of the caller's own.
 */
export function ApiKeyRouteHint({ testId }: { testId: string }) {
  return (
    <span data-testid={testId}>
      Create one for yourself under More →{' '}
      <Link href={OWN_API_KEYS_PATH} className="text-blue-600 underline">
        Your API keys
      </Link>{' '}
      (see{' '}
      <a href={docsUrl('security/api-keys')} className="text-blue-600 underline">
        API Key Management
      </a>
      ).
    </span>
  );
}
