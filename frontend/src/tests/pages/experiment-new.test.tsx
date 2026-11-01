import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import NewExperimentPage, { generateKey, validateForm } from '@/pages/experiments/new';
import { apiFetch } from '@/services/api';
import { docsUrl } from '@/services/docs';
import { apiError, makeRouter, routedApi } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

// The single-page form is the page's `?advanced` view; guided setup, the
// default view, has its own tests (experiment-new-guided.test.tsx).
const mockRouter = makeRouter({
  pathname: '/experiments/new',
  asPath: '/experiments/new?advanced',
  query: { advanced: '' },
});
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;

const setValue = (testId: string, value: string) =>
  fireEvent.change(screen.getByTestId(testId), { target: { value } });

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockRouter.push.mockClear();
});

describe('generateKey / validateForm', () => {
  it('slugs names into snake_case keys', () => {
    expect(generateKey('Checkout CTA colour!')).toBe('checkout_cta_colour');
  });

  it('mirrors the backend validators', () => {
    const variants = [
      { name: 'Control', description: '', traffic_allocation: 50, is_control: true },
      { name: 'B', description: '', traffic_allocation: 50, is_control: false },
    ];
    const metrics = [{ name: 'Conv', event_name: 'purchase', metric_type: 'conversion' as const, is_primary: true }];
    expect(validateForm('X', variants, metrics)).toBeNull();
    expect(validateForm('', variants, metrics)).toMatch(/name is required/i);
    expect(validateForm('X', [{ ...variants[0], is_control: false }, variants[1]], metrics)).toMatch(/control/i);
    expect(validateForm('X', [variants[0], { ...variants[1], traffic_allocation: 30 }], metrics)).toMatch(/80%/);
    expect(validateForm('X', variants, [])).toMatch(/at least one metric/i);
    expect(validateForm('X', variants, [{ ...metrics[0], event_name: '' }])).toMatch(/event name/i);
    expect(validateForm('X', variants, [{ ...metrics[0], is_primary: false }])).toMatch(/primary/i);
  });
});

describe('NewExperimentPage', () => {
  it('renders default variants and one default metric', () => {
    render(<NewExperimentPage />);
    expect(screen.getByTestId('variant-row-0')).toBeInTheDocument();
    expect(screen.getByTestId('variant-row-1')).toBeInTheDocument();
    expect(screen.getByTestId('variant-control-0')).toBeChecked();
    expect(screen.getByTestId('metrics-section')).toBeInTheDocument();
    expect(screen.getByTestId('metric-row-0')).toBeInTheDocument();
    expect(screen.getByTestId('metric-primary-0')).toBeChecked();
    expect(screen.getByTestId('allocation-total')).toHaveTextContent('Currently 100%');
  });

  it('offers only the experiment types this form can fully configure', () => {
    render(<NewExperimentPage />);
    const select = screen.getByTestId('experiment-type') as HTMLSelectElement;
    const options = Array.from(select.options).map((o) => ({ value: o.value, label: o.textContent }));
    // `split_url` needs `split_url_config` and `bandit` needs a non-`fixed`
    // `optimization_type`; this form sends neither, so they must not be offered.
    expect(options).toEqual([
      { value: 'a_b', label: 'A/B Test' },
      { value: 'mv', label: 'Multivariate' },
    ]);
    expect(select.value).toBe('a_b');
  });

  it('explains where split URL and bandit experiments come from', () => {
    render(<NewExperimentPage />);
    const note = screen.getByTestId('experiment-type-note');
    expect(note).toHaveTextContent(/split url/i);
    expect(note).toHaveTextContent(/bandit/i);
    expect(within(note).getByRole('link', { name: /split url/i })).toHaveAttribute(
      'href',
      docsUrl('api/split-url'),
    );
    expect(within(note).getByRole('link', { name: /bandit/i })).toHaveAttribute(
      'href',
      docsUrl('api/multi-armed-bandit'),
    );
  });

  it('auto-generates the key from the name until the key is edited', () => {
    render(<NewExperimentPage />);
    setValue('experiment-name', 'Pricing Page Test');
    expect(screen.getByTestId('experiment-key')).toHaveValue('pricing_page_test');
    setValue('experiment-key', 'custom-key');
    setValue('experiment-name', 'Another name');
    expect(screen.getByTestId('experiment-key')).toHaveValue('custom-key');
  });

  it('POSTs an ExperimentCreate payload (experiment_type, traffic_allocation, metrics) and redirects', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        { method: 'POST', path: '/api/v1/experiments', handler: () => ({ id: 'exp-9', status: 'draft' }) },
      ]) as unknown as typeof apiFetch,
    );
    render(<NewExperimentPage />);

    setValue('experiment-name', 'Pricing Page Test');
    setValue('experiment-description', 'Does the new layout convert?');
    setValue('experiment-type', 'mv');
    setValue('variant-name-1', 'New layout');
    setValue('metric-name-0', 'Purchase');
    setValue('metric-event-0', 'purchase');

    fireEvent.click(screen.getByTestId('add-metric'));
    setValue('metric-name-1', 'Revenue');
    setValue('metric-event-1', 'purchase');
    setValue('metric-type-1', 'revenue');

    fireEvent.click(screen.getByTestId('submit-experiment'));

    await waitFor(() => expect(mockRouter.push).toHaveBeenCalledWith('/experiments/exp-9'));

    const [path, options] = mockedApiFetch.mock.calls[0];
    expect(path).toBe('/api/v1/experiments');
    expect(options?.method).toBe('POST');
    expect(options?.json).toEqual({
      name: 'Pricing Page Test',
      key: 'pricing_page_test',
      description: 'Does the new layout convert?',
      hypothesis: undefined,
      experiment_type: 'mv',
      targeting_rules: null,
      variants: [
        { name: 'Control', description: undefined, is_control: true, traffic_allocation: 50 },
        { name: 'New layout', description: undefined, is_control: false, traffic_allocation: 50 },
      ],
      metrics: [
        { name: 'Purchase', event_name: 'purchase', metric_type: 'conversion', is_primary: true },
        { name: 'Revenue', event_name: 'purchase', metric_type: 'revenue', is_primary: false },
      ],
      confidence_level: 0.95,
      correction_method: 'benjamini_hochberg',
    });
  });

  it('blocks submission client-side when allocations do not sum to 100', async () => {
    render(<NewExperimentPage />);
    setValue('experiment-name', 'Broken');
    setValue('variant-allocation-1', '30');
    expect(screen.getByTestId('allocation-total')).toHaveTextContent('Currently 80%');

    fireEvent.click(screen.getByTestId('submit-experiment'));
    expect(await screen.findByTestId('form-error')).toHaveTextContent('add up to 100% (currently 80%)');
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it('requires at least one metric with an event name', async () => {
    render(<NewExperimentPage />);
    setValue('experiment-name', 'No metric');
    setValue('metric-event-0', '');
    fireEvent.click(screen.getByTestId('submit-experiment'));
    expect(await screen.findByTestId('form-error')).toHaveTextContent('name and an event name');
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it('adds and removes variants, re-pointing the control radio', () => {
    render(<NewExperimentPage />);
    fireEvent.click(screen.getByTestId('add-variant'));
    expect(screen.getByTestId('variant-row-2')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('variant-control-2'));
    expect(screen.getByTestId('variant-control-2')).toBeChecked();
    expect(screen.getByTestId('variant-control-0')).not.toBeChecked();
    fireEvent.click(screen.getByTestId('remove-variant-2'));
    expect(screen.queryByTestId('variant-row-2')).not.toBeInTheDocument();
  });

  it('flags a version left at 0% of traffic, clears it once given a share, and refuses to create with it (#1424)', async () => {
    render(<NewExperimentPage />);
    const message = 'This version gets 0% of traffic. Give it a share, or remove it.';
    // Control (control) 50 and Treatment 50: no version is at 0%, so nothing is flagged (control case).
    expect(screen.queryByText(message)).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('add-variant'));
    // The total still reads 100% and valid, but the new version has no users.
    expect(screen.getByTestId('allocation-total')).toHaveTextContent('Currently 100%');
    expect(screen.getByTestId('variant-no-share-2')).toHaveTextContent(message);
    setValue('experiment-name', 'Zero share');
    fireEvent.click(screen.getByTestId('submit-experiment'));
    expect(await screen.findByTestId('form-error')).toHaveTextContent(message);
    expect(mockedApiFetch).not.toHaveBeenCalled();
    // Give it a share: 40 / 30 / 30.
    setValue('variant-allocation-0', '40');
    setValue('variant-allocation-1', '30');
    setValue('variant-allocation-2', '30');
    expect(screen.queryByTestId('variant-no-share-2')).not.toBeInTheDocument();
    expect(screen.queryByText(message)).not.toBeInTheDocument();
  });

  it('removing the primary metric promotes the next one', () => {
    render(<NewExperimentPage />);
    fireEvent.click(screen.getByTestId('add-metric'));
    fireEvent.click(screen.getByTestId('remove-metric-0'));
    expect(screen.getByTestId('metric-primary-0')).toBeChecked();
    expect(screen.queryByTestId('remove-metric-0')).not.toBeInTheDocument();
  });

  it('shows the backend validation detail when the API rejects the payload', async () => {
    mockedApiFetch.mockImplementation(
      routedApi([
        {
          method: 'POST',
          path: '/api/v1/experiments',
          handler: () => {
            throw apiError(422, 'metrics: List should have at least 1 item');
          },
        },
      ]) as unknown as typeof apiFetch,
    );
    render(<NewExperimentPage />);
    setValue('experiment-name', 'Rejected');
    fireEvent.click(screen.getByTestId('submit-experiment'));
    expect(await screen.findByTestId('form-error')).toHaveTextContent('metrics: List should have at least 1 item');
    expect(mockRouter.push).not.toHaveBeenCalled();
    expect(screen.getByTestId('submit-experiment')).not.toBeDisabled();
  });
});
