import React from 'react';
import { SDK_GUIDES, SDKS } from '@/content/sdks';
import { FOCUS, WIDTH } from '@/components/homepage/styles';

/**
 * The SDKs with a release, all betas (the install sentence below): the ones on
 * a package registry, and Go, whose release is its module's tag.
 */
const BETA: ReadonlySet<string> = new Set([
  'js',
  'python',
  'react',
  'react-native',
  'openfeature',
  'openfeature-python',
  'dotnet',
  'go',
]);

export interface Chip {
  name: string;
  href: string;
  beta: boolean;
}

/**
 * One chip per display name in `SDKS`, linking that SDK's guide through
 * `SDK_GUIDES` (a map of literal `docsUrl` calls, the only kind
 * `docs-links.test.ts` checks). The betas first, then `SDKS`'s order. The two
 * OpenFeature providers share a name and a guide, so they are one chip.
 */
export function sdkChips(): Chip[] {
  const chips: Chip[] = [];
  const keys = Object.keys(SDKS);
  for (const key of [...keys.filter((k) => BETA.has(k)), ...keys.filter((k) => !BETA.has(k))]) {
    if (chips.some((c) => c.name === SDKS[key])) continue;
    chips.push({ name: SDKS[key], href: SDK_GUIDES[key], beta: BETA.has(key) });
  }
  return chips;
}

/**
 * The SDKs: every one `sdk/` holds, by name (`tests/content/sdks.test.tsx`
 * holds the names to `sdk/` and the page), with no count.
 */
export function SdkChips() {
  return (
    <section aria-labelledby="sdks-heading" className={`${WIDTH} py-12 lg:py-16`}>
      <div className="rounded-2xl border border-slate-200 bg-slate-50 p-6 sm:p-10">
        <h2 id="sdks-heading" className="text-2xl font-semibold tracking-tight">
          SDKs for the stack you have
        </h2>
        <p className="mt-3 max-w-3xl leading-relaxed text-slate-600">
          All MIT-licensed. By default, flag evaluation and experiment assignment happen on the server.
        </p>
        <ul role="list" className="mt-6 flex flex-wrap gap-2.5">
          {sdkChips().map((chip) => (
            <li key={chip.name}>
              <a
                href={chip.href}
                className={`inline-flex min-h-9 items-center gap-2 rounded-full border bg-white px-3.5 py-1.5 text-sm font-medium text-slate-900 hover:border-blue-400 ${
                  chip.beta ? 'border-blue-200' : 'border-slate-200'
                } ${FOCUS}`}
              >
                {chip.name}
                {chip.beta && (
                  <>
                    {' '}
                    <span className="rounded-full bg-blue-50 px-2 py-0.5 text-xs text-blue-700">beta</span>
                  </>
                )}
              </a>
            </li>
          ))}
        </ul>
        <p className="mt-6 max-w-3xl text-sm leading-relaxed text-slate-600">
          The SDKs marked beta have a release: the JavaScript, React, React Native, Python and .NET SDKs and the
          OpenFeature providers are on npm, PyPI or NuGet, and the Go SDK installs by module path. The others install
          from the repository. Each guide shows how to install its SDK.
        </p>
      </div>
    </section>
  );
}
