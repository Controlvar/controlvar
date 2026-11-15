import React from 'react';
import { render, screen } from '@testing-library/react';
import axe from 'axe-core';
import { SegmentComparisonTable } from '@/components/results/Breakdowns/SegmentComparisonTable';
import { DimensionalBreakdownResponse } from '@/types/results';

const makeBreakdown = (
  overrides: Partial<DimensionalBreakdownResponse> = {}
): DimensionalBreakdownResponse => ({
  dimension: 'platform',
  is_exploratory: true,
  adjusted_alpha: 0.0167,
  has_heterogeneous_effects: false,
  hte_warning: null,
  segments: [
    {
      segment_value: 'ios',
      sample_size: 500,
      variants: [
        {
          variant_id: 'ctrl',
          variant_name: 'Control',
          is_control: true,
          sample_size: 250,
          conversions: 25,
          mean: 0.1,
          confidence_interval: [0.065, 0.135],
          p_value: null,
          is_significant: false,
        },
        {
          variant_id: 'var1',
          variant_name: 'Variant B',
          is_control: false,
          sample_size: 250,
          conversions: 35,
          mean: 0.14,
          confidence_interval: [0.098, 0.182],
          p_value: 0.0412,
          is_significant: true,
        },
      ],
    },
    {
      segment_value: 'android',
      sample_size: 400,
      variants: [
        {
          variant_id: 'ctrl',
          variant_name: 'Control',
          is_control: true,
          sample_size: 200,
          conversions: 20,
          mean: 0.1,
          confidence_interval: [0.062, 0.138],
          p_value: null,
          is_significant: false,
        },
        {
          variant_id: 'var1',
          variant_name: 'Variant B',
          is_control: false,
          sample_size: 200,
          conversions: 22,
          mean: 0.11,
          confidence_interval: [0.07, 0.15],
          p_value: 0.72,
          is_significant: false,
        },
      ],
    },
  ],
  ...overrides,
});

describe('SegmentComparisonTable', () => {
  it('renders the table container with testid', () => {
    render(<SegmentComparisonTable breakdown={makeBreakdown()} />);
    expect(screen.getByTestId('segment-comparison-table')).toBeInTheDocument();
  });

  it('displays the exploratory warning when is_exploratory is true', () => {
    render(<SegmentComparisonTable breakdown={makeBreakdown({ is_exploratory: true })} />);
    expect(screen.getByRole('note', { name: /exploratory analysis warning/i })).toBeInTheDocument();
    expect(screen.getByText(/exploratory only/i)).toBeInTheDocument();
    // Should show the adjusted alpha
    expect(screen.getByText(/0\.0167/)).toBeInTheDocument();
  });

  it('does not show the exploratory warning when is_exploratory is false', () => {
    render(<SegmentComparisonTable breakdown={makeBreakdown({ is_exploratory: false })} />);
    expect(screen.queryByRole('note', { name: /exploratory analysis warning/i })).not.toBeInTheDocument();
  });

  it('shows HTE warning when has_heterogeneous_effects is true', () => {
    const breakdown = makeBreakdown({
      has_heterogeneous_effects: true,
      hte_warning: 'Significant variation detected across segments.',
    });
    render(<SegmentComparisonTable breakdown={breakdown} />);
    const hteWarning = screen.getByTestId('hte-warning');
    expect(hteWarning).toBeInTheDocument();
    expect(hteWarning).toHaveAttribute('role', 'alert');
    expect(screen.getByText('Significant variation detected across segments.')).toBeInTheDocument();
  });

  it('does not show HTE warning when has_heterogeneous_effects is false', () => {
    render(
      <SegmentComparisonTable
        breakdown={makeBreakdown({ has_heterogeneous_effects: false, hte_warning: null })}
      />
    );
    expect(screen.queryByTestId('hte-warning')).not.toBeInTheDocument();
  });

  it('renders a row for each variant in each segment', () => {
    render(<SegmentComparisonTable breakdown={makeBreakdown()} />);
    // 2 segments × 2 variants = 4 data rows + 1 header row = 5 rows total
    const rows = screen.getAllByRole('row');
    expect(rows).toHaveLength(5);
  });

  it('renders segment values as row headers', () => {
    render(<SegmentComparisonTable breakdown={makeBreakdown()} />);
    expect(screen.getByText('ios')).toBeInTheDocument();
    expect(screen.getByText('android')).toBeInTheDocument();
  });

  it('renders variant means as percentages', () => {
    render(<SegmentComparisonTable breakdown={makeBreakdown()} />);
    // Control mean 0.1 → 10.00%
    const tenPct = screen.getAllByText('10.00%');
    expect(tenPct.length).toBeGreaterThan(0);
    // Variant B ios mean 0.14 → 14.00%
    expect(screen.getByText('14.00%')).toBeInTheDocument();
  });

  it('shows Significant badge for significant non-control variants', () => {
    render(<SegmentComparisonTable breakdown={makeBreakdown()} />);
    expect(screen.getByText('Significant')).toBeInTheDocument();
  });

  it('shows Not significant badge for non-significant non-control variants', () => {
    render(<SegmentComparisonTable breakdown={makeBreakdown()} />);
    expect(screen.getByText('Not significant')).toBeInTheDocument();
  });

  it('does not show a significance badge for control variants', () => {
    // Control rows should have no badge in the significance column
    render(<SegmentComparisonTable breakdown={makeBreakdown()} />);
    // There are 2 control rows (one per segment); neither should have a badge.
    // The badge count should equal the number of non-control, non-null variant rows.
    const significantBadges = screen.getAllByText(/^(Significant|Not significant)$/);
    // 2 segments × 1 non-control variant = 2 badges
    expect(significantBadges).toHaveLength(2);
  });

  // UI5 (#1776): an empty list no longer means "no data for this dimension":
  // every assigned user is in a row (`unknown` at worst), so it means no users.
  it('J6: says no users are assigned when the segments array is empty', () => {
    render(<SegmentComparisonTable breakdown={makeBreakdown({ segments: [] })} />);
    expect(screen.getByText('No users are assigned to this experiment yet.')).toBeInTheDocument();
    expect(screen.queryByText(/no segment data available/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId('segment-attribution-caption')).not.toBeInTheDocument();
    expect(screen.queryByTestId('segment-unknown-only')).not.toBeInTheDocument();
  });
});

const ATTRIBUTION =
  'Each user is counted once, in the segment of the value the user was first assigned with, or else the first value tracked for them; unknown is users with none.';
const OTHER =
  'Values beyond the 20 with the most users are counted together under other.';

/** The first segment of makeBreakdown, renamed. */
function row(value: string) {
  return { ...makeBreakdown().segments[0], segment_value: value };
}

describe('SegmentComparisonTable captions and hints (#1776)', () => {
  it('J4: every table carries the attribution caption, in these words', () => {
    render(<SegmentComparisonTable breakdown={makeBreakdown()} />);
    expect(screen.getByTestId('segment-attribution-caption').textContent).toBe(ATTRIBUTION);
    expect(screen.getByTestId('segment-attribution-caption').textContent).not.toMatch(/seen with/);
  });

  it('J3: the other caption shows when an other row exists, and only then', () => {
    const { rerender } = render(
      <SegmentComparisonTable breakdown={makeBreakdown({ segments: [row('ios'), row('other')] })} />
    );
    expect(screen.getByTestId('segment-other-caption').textContent).toBe(OTHER);
    rerender(
      <SegmentComparisonTable breakdown={makeBreakdown({ segments: [row('ios'), row('unknown')] })} />
    );
    expect(screen.queryByTestId('segment-other-caption')).not.toBeInTheDocument();
    expect(screen.queryByText(/counted together under other/)).not.toBeInTheDocument();
  });

  it('J5: the unknown-only hint names the dimension, links the docs, and shows only when unknown is the only row', () => {
    const { rerender } = render(
      <SegmentComparisonTable breakdown={makeBreakdown({ segments: [row('unknown')] })} />
    );
    const hint = screen.getByTestId('segment-unknown-only');
    expect(hint.textContent).toContain(
      "No user of this experiment was assigned or tracked with platform. Send it in the user's attributes when you assign them."
    );
    expect(hint.querySelector('code')?.textContent).toBe('platform');
    const link = screen.getByRole('link', { name: 'How a user gets a segment' });
    expect(link.getAttribute('href')).toMatch(/api\/dimensional-analysis/);

    // unknown beside a real value: the dimension is carried by someone.
    rerender(
      <SegmentComparisonTable
        breakdown={makeBreakdown({ segments: [row('ios'), row('unknown')] })}
      />
    );
    expect(screen.queryByTestId('segment-unknown-only')).not.toBeInTheDocument();
    // a table with no unknown row at all
    rerender(<SegmentComparisonTable breakdown={makeBreakdown()} />);
    expect(screen.queryByTestId('segment-unknown-only')).not.toBeInTheDocument();
  });

  describe('J8: axe-core in jsdom, colour contrast off (jsdom cannot compute it)', () => {
    const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

    it.each([
      ['a table', makeBreakdown()],
      ['an other row', makeBreakdown({ segments: [row('ios'), row('other')] })],
      ['only unknown', makeBreakdown({ segments: [row('unknown')] })],
      ['no users', makeBreakdown({ segments: [] })],
    ])('has no violations with %s', async (_, breakdown) => {
      const { container } = render(<SegmentComparisonTable breakdown={breakdown} />);
      const result = await axe.run(container, axeOptions);
      expect(result.violations.map((v) => v.id)).toEqual([]);
    });
  });
});
