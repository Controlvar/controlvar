import React, { useState } from 'react';
import { withModule } from '@/components/ModuleNotice';
import { MODULES } from '@/services/modules';
import { PageTitle } from '@/components/PageTitle';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { workspaceService } from '@modules/services/workspaces';

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .slice(0, 50);
}

function validateSlug(slug: string): string | null {
  if (slug.length < 3) return 'Slug must be at least 3 characters';
  if (slug.length > 50) return 'Slug must be at most 50 characters';
  if (!/^[a-z0-9][a-z0-9-]*[a-z0-9]$/.test(slug))
    return 'Slug must be lowercase alphanumeric with hyphens, starting and ending with a letter or digit';
  return null;
}

function NewWorkspacePage() {
  const router = useRouter();
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [description, setDescription] = useState('');
  const [slugEdited, setSlugEdited] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState({ slug: false });

  const slugError = touched.slug ? validateSlug(slug) : null;

  function handleNameChange(e: React.ChangeEvent<HTMLInputElement>) {
    const v = e.target.value;
    setName(v);
    if (!slugEdited) {
      setSlug(slugify(v));
    }
  }

  function handleSlugChange(e: React.ChangeEvent<HTMLInputElement>) {
    setSlugEdited(true);
    setTouched((t) => ({ ...t, slug: true }));
    setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''));
  }

  function handleSlugBlur() {
    setTouched((t) => ({ ...t, slug: true }));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTouched({ slug: true });
    const err = validateSlug(slug);
    if (err) return;

    setSubmitting(true);
    setError(null);
    try {
      const ws = await workspaceService.create({ name, slug, description });
      router.push(`/workspaces/${ws.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create workspace');
      setSubmitting(false);
    }
  }

  return (
    <>
      <PageTitle title="New Workspace" />

      <div className="flex-1 bg-slate-50">
        {/* Nav */}

        <div className="max-w-xl mx-auto px-4 py-12">
          {/* Breadcrumb */}
          <div className="flex items-center gap-2 text-sm text-slate-500 mb-6">
            <Link href="/workspaces" className="hover:text-blue-600 transition-colors">
              Workspaces
            </Link>
            <span>/</span>
            <span className="text-slate-900 font-medium">New Workspace</span>
          </div>

          <div className="bg-white rounded-xl border border-slate-200 p-8">
            <h1 className="text-xl font-bold text-slate-900 mb-1">Create a Workspace</h1>
            <p className="text-sm text-slate-500 mb-6">
              Workspaces help you organize experiments and feature flags by team or project.
            </p>

            {error && (
              <div className="mb-5 rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">
                {error}
              </div>
            )}

            <form onSubmit={handleSubmit} className="space-y-5">
              <div>
                <label
                  htmlFor="ws-name"
                  className="block text-sm font-medium text-slate-700 mb-1"
                >
                  Workspace Name <span className="text-red-500">*</span>
                </label>
                <input
                  id="ws-name"
                  type="text"
                  value={name}
                  onChange={handleNameChange}
                  required
                  maxLength={100}
                  className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  placeholder="My Team Workspace"
                />
              </div>

              <div>
                <label
                  htmlFor="ws-slug"
                  className="block text-sm font-medium text-slate-700 mb-1"
                >
                  Slug <span className="text-red-500">*</span>
                </label>
                <div className="flex items-center gap-2">
                  <span className="text-sm text-slate-500 whitespace-nowrap">workspaces/</span>
                  <input
                    id="ws-slug"
                    type="text"
                    value={slug}
                    onChange={handleSlugChange}
                    onBlur={handleSlugBlur}
                    required
                    maxLength={50}
                    className={`flex-1 border rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 ${
                      slugError ? 'border-red-400 bg-red-50' : 'border-slate-300'
                    }`}
                    placeholder="my-team-workspace"
                  />
                </div>
                {slugError && (
                  <p className="mt-1 text-xs text-red-600">{slugError}</p>
                )}
                {!slugError && (
                  <p className="mt-1 text-xs text-slate-500">
                    3-50 chars — lowercase letters, numbers and hyphens only
                  </p>
                )}
              </div>

              <div>
                <label
                  htmlFor="ws-description"
                  className="block text-sm font-medium text-slate-700 mb-1"
                >
                  Description
                </label>
                <textarea
                  id="ws-description"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={4}
                  maxLength={500}
                  className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                  placeholder="What does this workspace contain? (optional)"
                />
                <p className="mt-1 text-xs text-slate-500 text-right">
                  {description.length}/500
                </p>
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <Link
                  href="/workspaces"
                  className="px-4 py-2 text-sm font-medium text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 transition-colors"
                >
                  Cancel
                </Link>
                <button
                  type="submit"
                  disabled={submitting}
                  className="px-5 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  {submitting ? 'Creating...' : 'Create Workspace'}
                </button>
              </div>
            </form>
          </div>
        </div>
      </div>
    </>
  );
}

export default withModule(NewWorkspacePage, {
  title: 'Workspaces',
  module: MODULES.WORKSPACES,
  description:
    'Group members into teams with workspace roles and invites. Access to experiments and flags is by platform role.',
});
