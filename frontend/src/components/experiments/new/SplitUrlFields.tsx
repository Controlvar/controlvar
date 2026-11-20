import React from 'react';
import { docsUrl } from '@/services/docs';
import { ExperimentFormAction, VariantFormData } from './formState';
import { inputClass } from './fieldStyles';

/** The type's one-line description, on guided setup's card. */
export const SPLIT_URL_DESCRIPTION =
  'Give each variant its own page. Your server or edge sends each visitor to theirs.';

export const SPLIT_URL_PAGES_HEADING = 'Pages';

/** Under the Pages heading, while Split URL is the type. */
export const SPLIT_URL_PAGES_LINE_START =
  "Visitors who ask for the control's page are split. Nothing sends them anywhere until your server or edge asks Controlvar for each visitor (";
export const SPLIT_URL_PAGES_LINE_LINK = 'Split URL testing';
export const SPLIT_URL_PAGES_LINE_END = ').';

/** The label of variant `index`'s page: by its trimmed name, or its number while it has none. */
export function pageLabel(variant: VariantFormData, index: number): string {
  const name = variant.name.trim();
  const base = name ? `Page for ${name}` : `Page for variant ${index + 1}`;
  return variant.is_control ? `${base} (the page visitors ask for)` : base;
}

interface SplitUrlFieldsProps {
  variants: VariantFormData[];
  dispatch: React.Dispatch<ExperimentFormAction>;
}

/**
 * The Pages block of a split URL experiment (#1961): one address per variant,
 * in variant order, kept on the variant's row (`VariantFormData.url`) so it
 * moves with the row. Rendered only while Split URL is the type, which is
 * offered only when the API lists the split_url module, so its container
 * (`split-url-config`) is there only then.
 */
export function SplitUrlFields({ variants, dispatch }: SplitUrlFieldsProps) {
  return (
    <section className="space-y-3" data-testid="split-url-config" aria-labelledby="split-url-pages-heading">
      <h3 id="split-url-pages-heading" className="text-sm font-semibold text-slate-800">
        {SPLIT_URL_PAGES_HEADING}
      </h3>
      <p className="text-sm text-slate-700" data-testid="split-url-pages-line">
        {SPLIT_URL_PAGES_LINE_START}
        <a href={docsUrl('api/split-url')} className="text-blue-700 underline hover:text-blue-900">
          {SPLIT_URL_PAGES_LINE_LINK}
        </a>
        {SPLIT_URL_PAGES_LINE_END}
      </p>
      {variants.map((v, i) => (
        <div key={i}>
          <label htmlFor={`split-url-variant-url-${i}`} className="block text-sm font-medium text-slate-700 mb-1">
            {pageLabel(v, i)}
          </label>
          <input
            id={`split-url-variant-url-${i}`}
            type="url"
            value={v.url ?? ''}
            onChange={(e) => dispatch({ type: 'changeVariant', index: i, field: 'url', value: e.target.value })}
            placeholder="https://example.com/page"
            autoComplete="off"
            spellCheck={false}
            className={inputClass}
            data-testid={`split-url-variant-url-${i}`}
          />
        </div>
      ))}
    </section>
  );
}
