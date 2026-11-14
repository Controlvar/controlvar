import React from 'react';
import { useRouter } from 'next/router';
import { ResultsDashboard } from '@/components/results/ResultsDashboard/ResultsDashboard';
import { ResultsCsvDownload } from '@/components/results/ResultsCsvDownload';
import { PageTitle } from '@/components/PageTitle';

export default function ResultDetailPage() {
  const { query } = useRouter();
  const id = query.id as string | undefined;
  // `?tab=breakdowns` (the experiment page's "See each group's split") opens
  // on Breakdowns; any other value is ignored.
  const initialTab = query.tab === 'breakdowns' ? 'breakdowns' : undefined;

  if (!id) {
    return (
      <div className="flex-1 flex items-center justify-center bg-slate-50">
        <PageTitle title="Results" />
        <div className="animate-pulse text-slate-500">Loading...</div>
      </div>
    );
  }

  return (
    <div className="flex-1 bg-slate-50">
      <PageTitle title="Results" />
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="flex justify-end mb-4">
          <ResultsCsvDownload experimentId={id} />
        </div>
        <ResultsDashboard experimentId={id} initialTab={initialTab} />
      </div>
    </div>
  );
}
