import React, { useState, useEffect } from 'react';
import { withModule } from '@/components/ModuleNotice';
import { MODULES } from '@/services/modules';
import { PageTitle } from '@/components/PageTitle';
import { Dialog } from '@/components/ui/Dialog';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Workspace, workspaceService } from '@modules/services/workspaces';

interface StatCardProps {
  label: string;
  value: number | string;
  href: string;
  icon: React.ReactNode;
}

function StatCard({ label, value, href, icon }: StatCardProps) {
  return (
    <Link
      href={href}
      className="block bg-white border border-slate-200 rounded-xl p-5 hover:border-blue-300 hover:shadow-sm transition-all group"
    >
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs text-slate-500 mb-1">{label}</p>
          <p className="text-2xl font-bold text-slate-900">{value}</p>
        </div>
        <div className="w-10 h-10 bg-blue-50 rounded-lg flex items-center justify-center text-blue-600 group-hover:bg-blue-100 transition-colors">
          {icon}
        </div>
      </div>
    </Link>
  );
}

interface EditModalProps {
  workspace: Workspace;
  onClose: () => void;
  onUpdated: (ws: Workspace) => void;
}

function EditWorkspaceModal({ workspace, onClose, onUpdated }: EditModalProps) {
  const [name, setName] = useState(workspace.name);
  const [description, setDescription] = useState(workspace.description ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const updated = await workspaceService.update(workspace.id, { name, description });
      onUpdated(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update workspace');
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
      title="Edit Workspace"
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
            <label htmlFor="edit-workspace-name" className="block text-sm font-medium text-slate-700 mb-1">
              Name <span className="text-red-500">*</span>
            </label>
            <input
              id="edit-workspace-name"
              data-autofocus=""
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label htmlFor="edit-workspace-slug" className="block text-sm font-medium text-slate-700 mb-1">
              Slug
            </label>
            <input
              id="edit-workspace-slug"
              type="text"
              value={workspace.slug}
              disabled
              className="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm bg-slate-50 text-slate-400 font-mono cursor-not-allowed"
            />
            <p className="mt-1 text-xs text-slate-400">Slug cannot be changed after creation</p>
          </div>
          <div>
            <label htmlFor="edit-workspace-description" className="block text-sm font-medium text-slate-700 mb-1">
              Description
            </label>
            <textarea
              id="edit-workspace-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
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
              disabled={submitting}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {submitting ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        </form>
      </div>
    </Dialog>
  );
}

function WorkspaceOverviewPage() {
  const router = useRouter();
  const { id } = router.query as { id: string };
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showEdit, setShowEdit] = useState(false);

  useEffect(() => {
    if (!id) return;
    workspaceService
      .get(id)
      .then(setWorkspace)
      .catch((err) =>
        setError(err instanceof Error ? err.message : 'Failed to load workspace')
      )
      .finally(() => setIsLoading(false));
  }, [id]);

  if (isLoading) {
    return (
      <div className="flex-1 bg-slate-50 flex items-center justify-center">
        <div className="inline-block w-6 h-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (error || !workspace) {
    return (
      <div className="flex-1 bg-slate-50 flex items-center justify-center">
        <div className="text-center">
          <p className="text-red-600 mb-4">{error ?? 'Workspace not found'}</p>
          <Link href="/workspaces" className="text-blue-600 hover:underline text-sm">
            Back to Workspaces
          </Link>
        </div>
      </div>
    );
  }

  const wsId = workspace.id;

  return (
    <>
      <PageTitle title={workspace.name} />

      <div className="flex-1 bg-slate-50">
        {/* Nav */}

        <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          {/* Breadcrumb */}
          <div className="flex items-center gap-2 text-sm text-slate-500 mb-6">
            <Link href="/workspaces" className="hover:text-blue-600 transition-colors">
              Workspaces
            </Link>
            <span>/</span>
            <span className="text-slate-900 font-medium">{workspace.name}</span>
          </div>

          {/* Header */}
          <div className="bg-white border border-slate-200 rounded-xl p-6 mb-6">
            <div className="flex items-start justify-between">
              <div className="flex-1">
                <div className="flex items-center gap-3 mb-1">
                  <h1 className="text-2xl font-bold text-slate-900">{workspace.name}</h1>
                  {!workspace.is_active && (
                    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-700">
                      Inactive
                    </span>
                  )}
                </div>
                <p className="text-sm text-slate-400 font-mono mb-2">{workspace.slug}</p>
                {workspace.description && (
                  <p className="text-sm text-slate-600">{workspace.description}</p>
                )}
              </div>
              <button
                onClick={() => setShowEdit(true)}
                className="ml-4 px-3 py-1.5 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 transition-colors"
              >
                Edit
              </button>
            </div>
          </div>

          {/* Quick links */}
          <div className="flex gap-3 mb-6 flex-wrap">
            <Link
              href={`/workspaces/${wsId}/members`}
              className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-blue-700 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 transition-colors"
            >
              Manage Members
            </Link>
          </div>

          {/* Stats grid */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <StatCard
              label="Members"
              value={workspace.member_count ?? 0}
              href={`/workspaces/${wsId}/members`}
              icon={
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
              }
            />
          </div>
        </div>
      </div>

      {showEdit && workspace && (
        <EditWorkspaceModal
          workspace={workspace}
          onClose={() => setShowEdit(false)}
          onUpdated={(updated) => {
            setWorkspace(updated);
            setShowEdit(false);
          }}
        />
      )}
    </>
  );
}

export default withModule(WorkspaceOverviewPage, {
  title: 'Workspaces',
  module: MODULES.WORKSPACES,
  description:
    'Group members into teams with workspace roles and invites. Access to experiments and flags is by platform role.',
});
