import { assignmentPill, assignmentReasonLabel, flagReasonLabel, NOT_ASSIGNED_PILL, notAssignedLabel } from '@/lib/labels';

const base = { loading: false, error: null };

describe('flagReasonLabel', () => {
  it('explains each server reason for on and off', () => {
    expect(flagReasonLabel({ ...base, isEnabled: true, reason: 'targeting_rule' })).toBe('on: matched a targeting rule');
    expect(flagReasonLabel({ ...base, isEnabled: false, reason: 'targeting_rule' })).toBe('off: matched a rule but outside its rollout %');
    expect(flagReasonLabel({ ...base, isEnabled: true, reason: 'rollout' })).toBe('on: inside the rollout %');
    expect(flagReasonLabel({ ...base, isEnabled: false, reason: 'rollout' })).toBe('off: outside the rollout %');
    expect(flagReasonLabel({ ...base, isEnabled: false, reason: 'inactive' })).toBe('off: flag is inactive');
    expect(flagReasonLabel({ ...base, isEnabled: false, reason: 'error' })).toBe('off: server evaluation error');
  });

  it('copes with older servers (no reason), loading and errors', () => {
    expect(flagReasonLabel({ ...base, isEnabled: true })).toBe('on');
    expect(flagReasonLabel({ ...base, isEnabled: false, reason: 'something_new' })).toBe('off: something_new');
    expect(flagReasonLabel({ isEnabled: false, loading: true, error: null })).toBe('evaluating…');
    expect(flagReasonLabel({ isEnabled: false, loading: false, error: new Error('API error: 401') })).toBe('error: API error: 401');
  });
});

describe('assignmentReasonLabel / assignmentPill', () => {
  it('names the group / holdout for every assigned:false reason', () => {
    expect(assignmentReasonLabel({ ...base, assigned: false, reason: 'holdout', variantName: 'control' })).toBe(
      'in global holdout streampulse-holdout (control shown)',
    );
    expect(assignmentReasonLabel({ ...base, assigned: false, reason: 'mutual_exclusion', variantName: 'control' })).toBe(
      'excluded: mutual exclusion group streampulse-profile (control shown)',
    );
    expect(assignmentReasonLabel({ ...base, assigned: false, reason: 'targeting', variantName: 'control' })).toBe(
      'excluded: targeting rules (control shown)',
    );
    expect(assignmentReasonLabel({ ...base, assigned: false, reason: 'quota', variantName: 'control' })).toBe('not enrolled: quota (control shown)');
    expect(assignmentPill({ ...base, assigned: false, variantName: 'control' })).toBe('not enrolled');
  });

  it('reports real assignments (including older servers without the field), loading and errors', () => {
    expect(assignmentReasonLabel({ ...base, assigned: true, reason: 'assigned', variantName: 'three_step' })).toBe('assigned to three_step');
    expect(assignmentReasonLabel({ ...base, variantName: 'daily' })).toBe('assigned to daily');
    expect(assignmentPill({ ...base, assigned: true, variantName: 'daily' })).toBe('daily');
    expect(assignmentReasonLabel({ loading: true, error: null, variantName: 'control' })).toBe('assigning…');
    expect(assignmentPill({ loading: true, error: null, variantName: 'control' })).toBe('loading…');
    expect(assignmentReasonLabel({ loading: false, error: new Error('API error: 404'), variantName: 'control' })).toBe('error: API error: 404');
    expect(assignmentPill({ loading: false, error: new Error('x'), variantName: 'control' })).toBe('error');
  });

  it('says a paused experiment is not enrolling and a completed one is over (both show control)', () => {
    expect(assignmentReasonLabel({ ...base, assigned: false, reason: 'paused', variantName: 'control' })).toBe(
      'not enrolled: paused (control shown)',
    );
    expect(assignmentPill({ ...base, assigned: false, reason: 'paused', variantName: 'control' })).toBe('not enrolled');
    expect(assignmentReasonLabel({ ...base, assigned: false, reason: 'completed', variantName: 'control' })).toBe(
      'experiment completed (control shown)',
    );
    expect(assignmentPill({ ...base, assigned: false, reason: 'completed', variantName: 'control' })).toBe('completed');
  });

  it('names the screen that assigns an experiment no screen has assigned yet', () => {
    expect(NOT_ASSIGNED_PILL).toBe('not assigned yet');
    expect(notAssignedLabel('Profile')).toBe('assigned when the Profile screen opens');
  });
});
