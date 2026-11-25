import { SchedulerRunStatus } from '@/types/admin';

export const RUN_STATUS_BADGE_CLASSES: Record<SchedulerRunStatus, string> = {
  success: 'bg-green-100 text-green-800',
  partial: 'bg-amber-100 text-amber-800',
  failed: 'bg-red-100 text-red-800',
  skipped: 'bg-slate-100 text-slate-600',
};
