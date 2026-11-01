/**
 * The metrics editor says, under each metric's type, what the results report
 * for that type (#439, UX C13).
 *
 * What is pinned:
 *  - each type's hint, word for word, including the duration trap: a
 *    duration is time per assigned user, not the time one event takes;
 *  - the hint is tied to its select by `aria-describedby`, so a screen reader
 *    reads it on focus, and it follows the select when the type changes;
 *  - each row's hint has its own id.
 */
import React, { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import axe from 'axe-core';
import { MetricsEditor, METRIC_TYPE_HINTS } from '@/components/experiments/new/MetricsEditor';
import { ExperimentFormAction, MetricFormData } from '@/components/experiments/new/formState';
import { MetricType } from '@/types/experiments';

function metric(overrides: Partial<MetricFormData> = {}): MetricFormData {
  return {
    name: 'Watch time',
    event_name: 'watch_session',
    metric_type: 'conversion',
    is_primary: true,
    ...overrides,
  } as MetricFormData;
}

/** The editor with its own state, so a change of type re-renders it. */
function Editor({ initial }: { initial: MetricFormData[] }) {
  const [metrics, setMetrics] = useState(initial);
  const dispatch = (action: ExperimentFormAction) => {
    if (action.type === 'changeMetric') {
      setMetrics((ms) =>
        ms.map((m, i) => (i === action.index ? { ...m, [action.field]: action.value } : m)),
      );
    }
  };
  return <MetricsEditor metrics={metrics} dispatch={dispatch as React.Dispatch<ExperimentFormAction>} />;
}

function describedText(select: HTMLElement): string {
  const id = select.getAttribute('aria-describedby');
  expect(id).toBeTruthy();
  return document.getElementById(id as string)?.textContent ?? '';
}

describe('the metric type hint', () => {
  it.each([
    ['conversion', 'The share of users who send the event.'],
    [
      'revenue',
      "Revenue per user: each user's event values added up, 0 for a user with none. No currency is read.",
    ],
    [
      'count',
      "Events per user: how many times each user sends the event, 0 for a user with none. The event's value is not read.",
    ],
    [
      'duration',
      "Duration per user: each user's event values added up (time spent, say), 0 for a user with none. " +
        'Not the time one event takes, such as page load: a variant whose users send the event less often reads as less time.',
    ],
    ['custom', "Value per user: each user's event values added up, 0 for a user with none."],
  ] as const)('says what a %s metric reports, tied to its select', (type, text) => {
    render(<Editor initial={[metric({ metric_type: type as MetricType })]} />);
    expect(screen.getByTestId('metric-type-hint-0')).toHaveTextContent(text);
    expect(METRIC_TYPE_HINTS[type as MetricType]).toBe(text);
    expect(describedText(screen.getByTestId('metric-type-0'))).toBe(text);
  });

  it('follows the select when the type changes', () => {
    render(<Editor initial={[metric()]} />);
    const select = screen.getByTestId('metric-type-0');
    expect(describedText(select)).toBe(METRIC_TYPE_HINTS.conversion);
    fireEvent.change(select, { target: { value: 'duration' } });
    expect(describedText(screen.getByTestId('metric-type-0'))).toBe(METRIC_TYPE_HINTS.duration);
    expect(screen.getByTestId('metric-type-hint-0')).toHaveTextContent(/page load/);
  });

  it('gives each row its own hint', () => {
    render(
      <Editor
        initial={[
          metric({ metric_type: 'count' }),
          metric({ name: 'Load', metric_type: 'duration', is_primary: false }),
        ]}
      />,
    );
    const first = screen.getByTestId('metric-type-0').getAttribute('aria-describedby');
    const second = screen.getByTestId('metric-type-1').getAttribute('aria-describedby');
    expect(first).not.toBe(second);
    expect(describedText(screen.getByTestId('metric-type-0'))).toBe(METRIC_TYPE_HINTS.count);
    expect(describedText(screen.getByTestId('metric-type-1'))).toBe(METRIC_TYPE_HINTS.duration);
  });

  it('has no axe violations', async () => {
    const { container } = render(<Editor initial={[metric({ metric_type: 'duration' })]} />);
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations).toEqual([]);
  });
});
