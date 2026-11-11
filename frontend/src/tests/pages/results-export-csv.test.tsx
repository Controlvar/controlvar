/**
 * /results/[id]: "Download CSV" (#442, docs row "Download results as CSV").
 *
 * The page and ResultsCsvDownload run as they do in the browser; the results
 * calls are held (`apiFetch` never answers: the button does not wait for
 * them) and the export goes through the real `apiDownload` to a replaced
 * `fetch`, so the test sees the exact request the browser would send.
 *
 * What is pinned:
 *  - one click is exactly one GET of
 *    `/api/v1/export/reports/experiments/{id}?format=csv`: with no `format`
 *    the API answers JSON, and a second request would spend the shared
 *    10-a-minute export counter twice;
 *  - the saved file is the response body, byte for byte;
 *  - a 429 gets its own sentence, every other failure the generic one, and
 *    neither saves a file nor shows the server's `detail`;
 *  - the sentence under the button says what the file holds.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ResultDetailPage from '@/pages/results/[id]';
import { apiFetch } from '@/services/api';
import { makeRouter } from './helpers/apiMock';

jest.mock('@/services/api', () => ({
  ...jest.requireActual('@/services/api'),
  apiFetch: jest.fn(),
}));

const mockRouter = makeRouter({ pathname: '/results/[id]', query: { id: 'exp-1' } });
jest.mock('next/router', () => ({ useRouter: () => mockRouter }));

jest.mock('next/head', () => {
  const Head = ({ children }: { children: React.ReactNode }) => <>{children}</>;
  Head.displayName = 'MockHead';
  return Head;
});

const mockedApiFetch = apiFetch as jest.MockedFunction<typeof apiFetch>;
const mockFetch = jest.fn();

// What data-export.md documents for /variants and /reports/experiments/{id}?format=csv,
// with a quoted comma and CRLF line ends, which a re-serialisation would change.
const CSV =
  'experiment_id,experiment_name,variant_id,variant_name,is_control,assignments,conversions,conversion_rate,p_value,is_significant,relative_improvement_pct\r\n' +
  'exp-1,"Checkout, new",v-1,control,True,1000,100,0.1,,False,\r\n' +
  'exp-1,"Checkout, new",v-2,better,False,1000,120,0.12,0.03,True,20.0\r\n';

const NOTE =
  "The file covers the primary metric only, with the experiment's saved correction and confidence level, and has no adjusted p-value.";
const FAILED = 'Could not download the results. Try again in a minute.';
const RATE_LIMITED =
  'The export limit for your address (10 a minute, shared by every export) has been reached. Wait a minute and try again.';

function response(status: number, body: string, headers: Record<string, string> = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'STATUS',
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    text: () => Promise.resolve(body),
  } as unknown as Response;
}

const createObjectURL = jest.fn((blob: Blob) => {
  void blob;
  return 'blob:results';
});
const revokeObjectURL = jest.fn();
let saved: string[] = [];

function blobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  saved = [];
  mockedApiFetch.mockReturnValue(new Promise(() => undefined));
  global.fetch = mockFetch as unknown as typeof fetch;
  Object.assign(URL, { createObjectURL, revokeObjectURL });
  jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(this.download);
  });
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe('#442: download an experiment’s results as CSV', () => {
  it('the results page has a Download CSV button with the sentence under it', () => {
    render(<ResultDetailPage />);
    const button = screen.getByTestId('results-export-csv');
    expect(button).toHaveTextContent('Download CSV');
    expect(button.tagName).toBe('BUTTON');
    expect(screen.getByTestId('results-export-csv-note')).toHaveTextContent(NOTE);
    expect(button).toHaveAccessibleDescription(NOTE);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('one click is exactly one GET of /api/v1/export/reports/experiments/{id}?format=csv', async () => {
    mockFetch.mockResolvedValue(response(200, CSV));
    render(<ResultDetailPage />);
    fireEvent.click(screen.getByTestId('results-export-csv'));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe('/api/v1/export/reports/experiments/exp-1?format=csv');
    expect((init as RequestInit | undefined)?.method ?? 'GET').toBe('GET');
  });

  it('saves the response body byte for byte, as results-{id}.csv', async () => {
    mockFetch.mockResolvedValue(response(200, CSV));
    render(<ResultDetailPage />);
    fireEvent.click(screen.getByTestId('results-export-csv'));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    const blob = createObjectURL.mock.calls[0][0] as Blob;
    expect(blob.type).toBe('text/csv;charset=utf-8');
    expect(await blobText(blob)).toBe(CSV);
    expect(saved).toEqual(['results-exp-1.csv']);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:results');
    expect(screen.queryByTestId('results-export-csv-error')).not.toBeInTheDocument();
  });

  it('a 429 reads the export-limit sentence, saves nothing and shows no server text', async () => {
    mockFetch.mockResolvedValue(
      response(429, JSON.stringify({ detail: 'Rate limit exceeded: 10 per 1 minute' }), { 'retry-after': '60' }),
    );
    render(<ResultDetailPage />);
    fireEvent.click(screen.getByTestId('results-export-csv'));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(RATE_LIMITED);
    expect(alert.textContent).toBe(RATE_LIMITED);
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    [403, '{"detail":"Forbidden for you"}'],
    [404, '{"detail":"Experiment not found"}'],
    [422, '{"detail":"bad id"}'],
    [500, 'Internal Server Error'],
    [503, '{"detail":"database down"}'],
  ])('a %s reads the generic sentence, saves nothing and shows no server text', async (status, body) => {
    mockFetch.mockResolvedValue(response(status, body));
    render(<ResultDetailPage />);
    fireEvent.click(screen.getByTestId('results-export-csv'));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe(FAILED);
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('an unreachable API reads the generic sentence', async () => {
    mockFetch.mockRejectedValue(new TypeError('Failed to fetch'));
    render(<ResultDetailPage />);
    fireEvent.click(screen.getByTestId('results-export-csv'));
    expect((await screen.findByRole('alert')).textContent).toBe(FAILED);
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('is busy while it downloads, and a second click sends nothing', async () => {
    let finish: (value: Response) => void = () => undefined;
    mockFetch.mockReturnValue(new Promise<Response>((resolve) => (finish = resolve)));
    render(<ResultDetailPage />);
    const button = screen.getByTestId('results-export-csv');
    fireEvent.click(button);
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent('Downloading…');
    fireEvent.click(button);
    expect(mockFetch).toHaveBeenCalledTimes(1);
    finish(response(200, CSV));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(button).not.toBeDisabled());
    expect(button).toHaveTextContent('Download CSV');
  });

  it('a failure is cleared by the next attempt, which saves the file', async () => {
    mockFetch.mockResolvedValueOnce(response(500, 'Internal Server Error'));
    mockFetch.mockResolvedValueOnce(response(200, CSV));
    render(<ResultDetailPage />);
    fireEvent.click(screen.getByTestId('results-export-csv'));
    await screen.findByRole('alert');
    fireEvent.click(screen.getByTestId('results-export-csv'));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });
});
