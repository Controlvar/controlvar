import React from 'react';
import { PageTitle } from '@/components/PageTitle';
import { Walkthroughs } from '@/components/Walkthroughs';
import { marketingOnly } from '@/utils/marketing-only';

/** The marketing build only: a platform build writes no /walkthroughs page (`utils/marketing-only.ts`). */
export const getStaticProps = marketingOnly;

/**
 * The walkthrough videos (#1290), a page of their own since the site design
 * of 2026-11-11: the players, captions and transcripts the homepage carried,
 * unchanged. The homepage keeps a teaser of posters that link to each row
 * here by its anchor.
 *
 * `/walkthroughs` is an `open` route (`utils/routes.ts`), so it sits in the
 * shell: the public site's header and footer.
 */
export default function WalkthroughsPage() {
  return (
    <>
      <PageTitle
        title="Walkthroughs"
        path="/walkthroughs"
        description="Videos of Controlvar at work, from getting started to a flag rollout, each with captions and a transcript."
      />
      <div className="flex-1 bg-white">
        <Walkthroughs />
      </div>
    </>
  );
}
