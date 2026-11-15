import React, { useState, useEffect } from 'react';
import { withModule } from '@/components/ModuleNotice';
import { MODULES } from '@/services/modules';
import { PageTitle } from '@/components/PageTitle';
import { Dialog } from '@/components/ui/Dialog';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Workspace, workspaceService } from '@modules/services/workspaces';

interface CreateModalProps {
  onClose: () => void;
  onCreated: (ws: Workspace) => void;
}

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .slice(0, 50);
}

function CreateWorkspaceModal({ onClose, onCreated }: CreateModalProps) {
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [description, setDescription] = useState('');
  const [slugEdited, setSlugEdited] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const slugError =
    slug.length > 0 && !/^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$/.test(slug)
      ? 'Slug must be 3-50 chars, lowercase alphanumeric and hyphens'
      : null;

  function handleNameChange(e: React.ChangeEvent<HTMLInputElement>) {
    const v = e.target.value;
    setName(v);
    if (!slugEdited) {
      setSlug(slugify(v));
    }
  }

  function handleSlugChange(e: React.ChangeEvent<HTMLInputElement>) {
    setSlugEdited(true);
    setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (slugError) return;
    setSubmitting(true);
    setError(null);
    try {
      const ws = await workspaceService.create({ name, slug, description });
      onCreated(ws);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create workspace');
    } finally {
      setSubmitting(false);
    }
  }

  // The shared Dialog closes on Escape and not on a click outside; closing is
  // never refused while the request is in flight (Cancel works then too).
  return (
    <Dialog
      open
      onClose={onClose}
      title="Create Workspace"
      headerAction={
        <button
          type="button"
          onClick={onClose}
          className="text-slate-400 hover:text-slate-600 text-xl leading-none"
          aria-label="Close"
        >
          &times;
        </button>
      }
    >
      <div>
        {error && (
          <div className="mb-4 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="create-workspace-name" className="block text-sm font-medium text-slate-700 mb-1">
              Name <span className="text-red-500">*</span>
            </label>
            <input
              id="create-workspace-name"
              data-autofocus=""
              type="text"
              value={name}
              onChange={handleNameChange}
              required
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="My Team Workspace"
            />
          </div>

          <div>
            <label htmlFor="create-workspace-slug" className="block text-sm font-medium text-slate-700 mb-1">
              Slug <span className="text-red-500">*</span>
            </label>
            <input
              id="create-workspace-slug"
              type="text"
              value={slug}
              onChange={handleSlugChange}
              required
              className={`w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 ${
                slugError ? 'border-red-400' : 'border-slate-300'
              }`}
              placeholder="my-team-workspace"
            />
            {slugError && (
              <p className="mt-1 text-xs text-red-600">{slugError}</p>
            )}
            <p className="mt-1 text-xs text-slate-400">
              Lowercase alphanumeric and hyphens, 3-50 chars
            </p>
          </div>

          <div>
            <label htmlFor="create-workspace-description" className="block text-sm font-medium text-slate-700 mb-1">
              Description
            </label>
            <textarea
              id="create-workspace-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
              placeholder="Optional description..."
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || !!slugError}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {submitting ? 'Creating...' : 'Create Workspace'}
            </button>
          </div>
        </form>
      </div>
    </Dialog>
  );
}

function WorkspaceCard({ workspace }: { workspace: Workspace }) {
  return (
    <Link
      href={`/workspaces/${workspace.id}`}
      className="block bg-white border border-slate-200 rounded-xl p-5 hover:border-blue-300 hover:shadow-sm transition-all group"
    >
      <div className="flex items-start justify-between mb-3">
        <div className="flex-1 min-w-0">
          <h3 className="font-semibold text-slate-900 group-hover:text-blue-700 truncate">
            {workspace.name}
          </h3>
          <p className="text-xs text-slate-400 mt-0.5 font-mono">{workspace.slug}</p>
        </div>
      </div>

      {workspace.description && (
        <p className="text-sm text-slate-500 mb-3 line-clamp-2">{workspace.description}</p>
      )}

      {/* The list answers each workspace's member count; without one, say nothing rather than 0. */}
      {typeof workspace.member_count === 'number' && (
        <div className="flex items-center gap-4 text-xs text-slate-500">
          <span className="flex items-center gap-1" data-testid="workspace-member-count">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
            {workspace.member_count} member{workspace.member_count !== 1 ? 's' : ''}
          </span>
        </div>
      )}
    </Link>
  );
}

function WorkspacesPage() {
  const router = useRouter();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  useEffect(() => {
    workspaceService
      .list()
      .then(setWorkspaces)
      .catch((err) =>
        setError(err instanceof Error ? err.message : 'Failed to load workspaces')
      )
      .finally(() => setIsLoading(false));
  }, []);

  function handleCreated(ws: Workspace) {
    setShowCreate(false);
    router.push(`/workspaces/${ws.id}`);
  }

  return (
    <>
      <PageTitle title="Workspaces" />

      <div className="flex-1 bg-slate-50">

        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          <div className="flex items-center justify-between mb-6">
            <div>
              <h1 className="text-2xl font-bold text-slate-900">Workspaces</h1>
              <p className="text-sm text-slate-500 mt-1">
                Manage your team workspaces and collaboration spaces
              </p>
            </div>
            <button
              onClick={() => setShowCreate(true)}
              className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
            >
              + Create Workspace
            </button>
          </div>

          {isLoading && (
            <div className="text-center py-16">
              <div className="inline-block w-6 h-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
              <p className="text-slate-500 mt-2 text-sm">Loading workspaces...</p>
            </div>
          )}

          {!isLoading && error && (
            <div className="rounded-lg bg-red-50 border border-red-200 p-4 text-sm text-red-700">
              {error}
            </div>
          )}

          {!isLoading && !error && workspaces.length === 0 && (
            <div className="text-center py-20 bg-white rounded-xl border border-slate-200">
              <div className="w-16 h-16 bg-slate-100 rounded-full flex items-center justify-center mx-auto mb-4">
                <svg className="w-8 h-8 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
                </svg>
              </div>
              <h3 className="text-slate-700 font-medium mb-1">No workspaces yet</h3>
              <p className="text-slate-400 text-sm mb-5">
                Create your first workspace to organize your team&apos;s experiments
              </p>
              <button
                onClick={() => setShowCreate(true)}
                className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
              >
                Create your first workspace
              </button>
            </div>
          )}

          {!isLoading && !error && workspaces.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {workspaces.map((ws) => (
                <WorkspaceCard key={ws.id} workspace={ws} />
              ))}
            </div>
          )}
        </div>
      </div>

      {showCreate && (
        <CreateWorkspaceModal
          onClose={() => setShowCreate(false)}
          onCreated={handleCreated}
        />
      )}
    </>
  );
}

export default withModule(WorkspacesPage, {
  title: 'Workspaces',
  module: MODULES.WORKSPACES,
  description:
    'Group members into teams with workspace roles and invites. Access to experiments and flags is by platform role.',
});
