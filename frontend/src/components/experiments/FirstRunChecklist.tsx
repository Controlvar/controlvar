import React, { useState } from 'react';
import Link from 'next/link';
import { useOptionalAuth } from '@/contexts/AuthContext';
import { apiBase } from '@/services/api';
import { ApiKeyRouteHint } from '@/components/experiments/ApiKeyRouteHint';

/** Origin shown in the curl sample: configured API URL, else the page origin. */
export function sampleApiOrigin(): string {
  const base = apiBase();
  if (base) return base;
  if (typeof window !== 'undefined' && window.location) return window.location.origin;
  return 'http://localhost:8000';
}

/**
 * What the sample shows where the experiment's key goes. The card is shown only
 * while there is no experiment, so it has no real key to show: the reader puts
 * in the key that the experiment's page shows.
 */
export const EXPERIMENT_KEY_PLACEHOLDER = 'YOUR_EXPERIMENT_KEY';

export function assignCurlSample(origin: string, experimentKey = EXPERIMENT_KEY_PLACEHOLDER): string {
  return [
    `curl -X POST "${origin}/api/v1/tracking/assign" \\`,
    `  -H "X-API-Key: $API_KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{"experiment_key": "${experimentKey}", "user_id": "user-123"}'`,
  ].join('\n');
}

/**
 * Three-step onboarding card shown on `/experiments` when the list is empty:
 * create an API key, create an experiment (which opens the experiment's page),
 * then start it and send an assignment with its key. The API key comes first
 * because creating the experiment leaves this page, and the card with it. The
 * `first-run` docs journey walks these steps on a stack with no seed data.
 */
export function FirstRunChecklist() {
  const [copied, setCopied] = useState(false);
  const curl = assignCurlSample(sampleApiOrigin());
  // /admin/api-keys opens only for a superuser (withAdminGuard). Anyone else,
  // and a reader whose account is not known yet, is pointed at the route
  // instead of a page that answers 403 (#920).
  const superuser = useOptionalAuth()?.user?.is_superuser === true;

  const copy = async () => {
    try {
      await navigator.clipboard?.writeText(curl);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable — the text is selectable */
    }
  };

  const stepClass = 'flex gap-4';
  const badgeClass =
    'flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-blue-600 text-white text-xs font-semibold';

  return (
    <div
      className="bg-white rounded-lg border border-slate-200 p-6 sm:p-8"
      data-testid="first-run-checklist"
    >
      <h2 className="text-lg font-semibold text-slate-900">Assign your first user in three steps</h2>
      <p className="text-sm text-slate-500 mt-1 mb-6">
        No experiments yet. These steps take you from an empty workspace to a live assignment.
      </p>

      <ol className="space-y-6">
        <li className={stepClass} data-testid="checklist-step-1">
          <span className={badgeClass}>1</span>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-slate-800">Create an API key</h3>
            <p className="text-sm text-slate-500 mt-0.5">
              SDKs and the tracking API authenticate with <code className="font-mono">X-API-Key</code>.
              A key starts with <code className="font-mono">eptk_</code> and is shown once — copy it
              when it appears.
            </p>
            {superuser ? (
              <Link
                href="/admin/api-keys"
                className="inline-flex items-center mt-2 text-sm font-medium text-blue-600 hover:underline"
                data-testid="checklist-api-keys"
              >
                Open Admin → API Keys
              </Link>
            ) : (
              <p className="text-sm text-slate-500 mt-2">
                <ApiKeyRouteHint testId="checklist-api-key-route" />
              </p>
            )}
          </div>
        </li>

        <li className={stepClass} data-testid="checklist-step-2">
          <span className={badgeClass}>2</span>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-slate-800">Create an experiment</h3>
            <p className="text-sm text-slate-500 mt-0.5">
              Give it a name, a control and a treatment variant, and at least one metric.
              Creating it opens the experiment&apos;s page, which shows its key.
            </p>
            <Link
              href="/experiments/new"
              className="inline-flex items-center mt-2 px-3 py-1.5 rounded-md bg-blue-600 text-white text-sm font-medium hover:bg-blue-700"
              data-testid="checklist-create-experiment"
            >
              + New Experiment
            </Link>
          </div>
        </li>

        <li className={stepClass} data-testid="checklist-step-3">
          <span className={badgeClass}>3</span>
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-slate-800">Start it, then send an assignment</h3>
            <p className="text-sm text-slate-500 mt-0.5" data-testid="checklist-start">
              On the experiment&apos;s page, press Start: only an active experiment assigns users.
              Then call the tracking API (or any{' '}
              <Link href="/docs" className="text-blue-600 underline hover:text-blue-700">
                SDK
              </Link>
              ) with the experiment&apos;s key in place of{' '}
              <code className="font-mono">{EXPERIMENT_KEY_PLACEHOLDER}</code> and your API key in{' '}
              <code className="font-mono">API_KEY</code>. The response carries the variant to render.
            </p>
            <div className="relative mt-2">
              <pre
                className="bg-slate-900 text-slate-100 text-xs rounded-md p-3 pr-20 overflow-x-auto"
                data-testid="checklist-curl"
              >
                {curl}
              </pre>
              <button
                type="button"
                onClick={() => void copy()}
                className="absolute top-2 right-2 px-2 py-1 rounded bg-slate-700 text-slate-100 text-xs hover:bg-slate-600"
                data-testid="checklist-copy-curl"
              >
                {copied ? 'Copied' : 'Copy'}
              </button>
              <span className="sr-only" role="status" data-testid="checklist-copy-status">
                {copied ? 'Copied' : ''}
              </span>
            </div>
          </div>
        </li>
      </ol>
    </div>
  );
}

export default FirstRunChecklist;
