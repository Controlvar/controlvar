import React from 'react';
import { SDK_GUIDES, SDKS } from '@/content/sdks';
import { FOCUS, WIDTH } from '@/components/homepage/styles';

/** The two SDKs on a package registry, both as betas (the install sentence below). */
const BETA: ReadonlySet<string> = new Set(['js', 'python']);

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
          The JavaScript SDK is on npm (npm install @controlvar/js-sdk) and the Python SDK is on PyPI (pip install
          controlvar), both as betas; the others are not on npm or the other package registries yet, and each of their
          guides shows how to install it from the repository.
        </p>
      </div>
    </section>
  );
}
