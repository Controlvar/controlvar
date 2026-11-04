import React, { useState } from 'react';
import { AdminLayout } from '@/components/admin/AdminLayout';
import { ApiKeyTable } from '@/components/admin/api-keys/ApiKeyTable';
import { CreateApiKeyModal } from '@/components/admin/api-keys/CreateApiKeyModal';
import { withAdminGuard } from '@/components/admin/withAdminGuard';

export function ApiKeysPage() {
  const [modalOpen, setModalOpen] = useState(false);
  const [newKeyBanner, setNewKeyBanner] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // What the live region says. Cleared (not switched back) when the "copied"
  // state wears off, so nothing is announced a second time.
  const [announcement, setAnnouncement] = useState('');
  const [tableKey, setTableKey] = useState(0);

  const handleKeyCreated = (keyValue: string) => {
    setNewKeyBanner(keyValue);
    setAnnouncement('Key created');
    setModalOpen(false);
    // Ask the table to refetch
    setTableKey((prev) => prev + 1);
  };

  return (
    <AdminLayout title="API Keys" currentPath="/admin/api-keys">
      <div data-testid="api-keys-page" className="flex flex-col gap-5">
        <h2 className="text-xl font-semibold text-slate-900">API Keys</h2>

        {/* Always mounted so a screen reader announces the change. Never the
            key itself: only that one was created, and that it was copied. */}
        <div role="status" aria-live="polite" data-testid="new-key-live-region" className="sr-only">
          {announcement}
        </div>

        {/* Banner showing the newly created key (shown once) */}
        {newKeyBanner && (
          <div
            data-testid="new-key-banner"
            className="p-4 bg-amber-50 border border-amber-300 rounded-md flex items-center justify-between"
          >
            <div className="min-w-0">
              <p className="text-sm font-semibold text-amber-800">
                Copy this key — it won&apos;t be shown again
              </p>
              {/* The whole key, not a preview: this is the only time the
                  plaintext exists anywhere, and a truncated one is useless. */}
              <code
                data-testid="new-key-value"
                className="block font-mono text-sm text-amber-900 mt-1 break-all select-all"
              >
                {newKeyBanner}
              </code>
            </div>
            <div className="flex items-center gap-3 ml-4 shrink-0">
              <button
                data-testid="copy-new-key"
                onClick={async () => {
                  try {
                    await navigator.clipboard?.writeText(newKeyBanner);
                    setCopied(true);
                    setAnnouncement('Key copied');
                    setTimeout(() => {
                      setCopied(false);
                      setAnnouncement('');
                    }, 2000);
                  } catch {
                    setCopied(false);
                  }
                }}
                className="px-3 py-1.5 rounded-md bg-amber-800 text-white text-sm font-medium hover:bg-amber-900"
              >
                {copied ? 'Copied' : 'Copy'}
              </button>
              <button
                data-testid="dismiss-key-banner"
                onClick={() => {
                  setNewKeyBanner(null);
                  setCopied(false);
                  setAnnouncement('');
                }}
                className="text-amber-800 hover:text-amber-900 text-sm font-medium"
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        <ApiKeyTable refreshToken={tableKey} onCreateKey={() => setModalOpen(true)} />

        <CreateApiKeyModal
          isOpen={modalOpen}
          onClose={() => setModalOpen(false)}
          onSuccess={handleKeyCreated}
        />
      </div>
    </AdminLayout>
  );
}

export default withAdminGuard(ApiKeysPage);
