import React from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AnalysisNotice } from '@/components/results/shared/AnalysisNotice';

// The interaction analysis's notice, as the API sends it (#219).
const INTERACTIONS_NOTICE =
  "Beta: tests whether each treatment's lift, in percentage points, on its " +
  "experiment's primary conversion metric differs across the other " +
  "experiment's variants, among the users in both. p_value is uncorrected; " +
  "corrected_p_value applies the experiment's stored correction across its " +
  "own treatments, and is_significant uses it at the experiment's " +
  'confidence level. Pairs are tested one at a time, with no correction ' +
  'across pairs. Secondary and non-conversion metrics are not tested. ' +
  'https://github.com/Controlvar/controlvar/issues/219';

describe('AnalysisNotice', () => {
  it('renders a Beta label, the notice text and the issue link when beta', () => {
    render(<AnalysisNotice status="beta" notice={INTERACTIONS_NOTICE} />);

    const region = screen.getByRole('region', { name: /beta/i });
    // The label is text, not colour alone.
    expect(within(region).getByTestId('analysis-notice-label')).toHaveTextContent(/^Beta$/);
    expect(region).toHaveTextContent(/tests whether each treatment's lift, in percentage points/i);
    // The "Beta:" prefix is not repeated after the label.
    expect(screen.getByTestId('analysis-notice-text').textContent).not.toMatch(/^Beta:/);

    const link = within(region).getByRole('link', { name: 'issue #219' });
    expect(link).toHaveAttribute(
      'href',
      'https://github.com/Controlvar/controlvar/issues/219'
    );
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));
  });

  it('is not an alert, and its link is reachable by keyboard', async () => {
    render(<AnalysisNotice status="beta" notice={INTERACTIONS_NOTICE} />);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByTestId('analysis-notice')).not.toHaveAttribute('aria-live');

    await userEvent.tab();
    expect(screen.getByRole('link', { name: 'issue #219' })).toHaveFocus();
  });

  it('keeps text that follows the issue link', () => {
    render(
      <AnalysisNotice
        status="beta"
        notice={
          'Beta: the stop/continue decision is mSPRT alone, at the significance ' +
          'level shown by the boundary (1/alpha). alpha_spending is always empty: ' +
          'the planned-looks (alpha-spending) table is not computed yet. ' +
          'https://github.com/Controlvar/controlvar/issues/232 ' +
          "The configured method 'always_valid' is an alias of 'msprt'; this is the " +
          'mSPRT analysis.'
        }
      />
    );
    expect(screen.getByTestId('analysis-notice-text')).toHaveTextContent(
      'the stop/continue decision is mSPRT alone, at the significance level shown by ' +
        'the boundary (1/alpha). alpha_spending is always empty: the planned-looks ' +
        '(alpha-spending) table is not computed yet. issue #232 ' +
        "The configured method 'always_valid' is an alias of 'msprt'; this is the mSPRT analysis."
    );
  });

  it('renders a notice with no issue URL as plain text, without a link', () => {
    render(<AnalysisNotice status="beta" notice="Beta: not computed yet." />);
    expect(screen.getByTestId('analysis-notice-text')).toHaveTextContent('not computed yet.');
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('does not link a URL that is not an issue link', () => {
    render(
      <AnalysisNotice status="beta" notice="Beta: see https://example.com/issues/1 for more." />
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('renders nothing when the status is ga', () => {
    const { container } = render(<AnalysisNotice status="ga" notice={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the fields are absent', () => {
    const { container } = render(<AnalysisNotice />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when beta arrives without a notice', () => {
    const { container } = render(<AnalysisNotice status="beta" notice={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
