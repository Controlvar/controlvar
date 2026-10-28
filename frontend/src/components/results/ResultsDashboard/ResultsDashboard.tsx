import React, { useState, useEffect, useCallback, useRef } from 'react';
import { ResultsService } from '@/services/results';
import { ExperimentsService } from '@/services/experiments';
import {
  CorrectionMethod,
  ExperimentResultsResponse,
  DailyResultsResponse,
  SampleSizeOverrides,
  SampleSizeResult,
  DimensionalBreakdownResponse,
  MetricResult,
} from '@/types/results';
import {
  analysedAsMean,
  meanDescription,
  rateDescription,
} from '@/components/results/shared/resultFormat';
import { SequentialTestingResponse } from '@/types/sequential';
import { ExperimentSummary } from './ExperimentSummary';
import { SrmNotice } from './SrmNotice';
import { BayesianPanel } from '@/components/results/Bayesian/BayesianPanel';
import { SampleSizeMeter } from './SampleSizeMeter';
import { ConversionChart } from '@/components/results/Visualizations/ConversionChart';
import { TrendChart } from '@/components/results/Visualizations/TrendChart';
import { MetricComparisonTable } from '@/components/results/MetricComparison/MetricComparisonTable';
import { SequentialMonitor } from '@/components/results/Sequential/SequentialMonitor';
import { BreakdownSelector } from '@/components/results/Breakdowns/BreakdownSelector';
import { SegmentComparisonTable } from '@/components/results/Breakdowns/SegmentComparisonTable';
// EP-058: Real-time WebSocket Streaming Results
import { LiveResultsPanel } from '@/components/experiments/LiveResultsPanel';
import { ApiError } from '@/services/api';

interface ResultsDashboardProps {
  experimentId: string;
}

/** The experiment's stored settings, as `GET /results/{id}` takes them. */
export interface StoredAnalysisSettings {
  correction_method: CorrectionMethod;
  confidence_level: number;
}

/** What the results page reads from the experiment itself. */
interface ExperimentReading {
  settings: StoredAnalysisSettings | null;
  /** Undefined when the experiment could not be read. */
  bayesianEnabled: boolean | undefined;
  /** Undefined when the experiment could not be read. */
  sequentialEnabled: boolean | undefined;
  /**
   * Whether a lower value is better for the primary metric, the one the
   * sequential analysis runs on. Undefined when the experiment could not be
   * read, has no metric, or the metric is not a conversion (the analysis then
   * compares the share of users who sent the event, not the metric's value).
   */
  lowerIsBetter: boolean | undefined;
  /**
   * Each metric's event name by metric id (a result's metric_id is the
   * metric's id), so a note can name the event. Empty when the experiment
   * could not be read.
   */
  eventNames: Record<string, string>;
}

/** Each metric's event name by its id; metrics without one are left out. */
function eventNamesOf(metrics: { id?: string; event_name?: string }[]): Record<string, string> {
  const names: Record<string, string> = {};
  for (const m of metrics) {
    if (typeof m.id === 'string' && typeof m.event_name === 'string' && m.event_name) {
      names[m.id] = m.event_name;
    }
  }
  return names;
}

/**
 * The experiment's stored settings (null when the experiment cannot be read
 * or does not carry them) and its Bayesian setting. Never throws: without
 * them the results are still asked for, and the server uses the stored
 * settings itself.
 */
async function readExperiment(experimentId: string): Promise<ExperimentReading> {
  try {
    const experiment = await ExperimentsService.get(experimentId);
    const bayesianEnabled =
      typeof experiment?.bayesian_enabled === 'boolean' ? experiment.bayesian_enabled : undefined;
    const sequentialEnabled =
      typeof experiment?.sequential_testing_enabled === 'boolean'
        ? experiment.sequential_testing_enabled
        : undefined;
    // The same metric the server picks for the sequential analysis: the
    // primary one, else the first. The API always sends `lower_is_better`; a
    // missing value is its default, false.
    //
    // The analysis counts the users who sent the metric's event, whatever the
    // metric_type, so its interval is a difference in the share of users. Only
    // for a conversion metric is that share the metric itself; for a duration,
    // count, revenue or custom metric a drop in it can mean the event stopped
    // firing, so the direction is left unknown and the banner does not judge.
    const metrics = experiment?.metrics ?? [];
    const sequentialMetric = metrics.find((m) => m.is_primary) ?? metrics[0];
    const lowerIsBetter =
      sequentialMetric && sequentialMetric.metric_type === 'conversion'
        ? (sequentialMetric.lower_is_better ?? false)
        : undefined;
    const eventNames = eventNamesOf(metrics);
    if (experiment?.correction_method && typeof experiment.confidence_level === 'number') {
      return {
        settings: {
          correction_method: experiment.correction_method,
          confidence_level: experiment.confidence_level,
        },
        bayesianEnabled,
        sequentialEnabled,
        lowerIsBetter,
        eventNames,
      };
    }
    return { settings: null, bayesianEnabled, sequentialEnabled, lowerIsBetter, eventNames };
  } catch {
    // Fall through: the results request goes without settings.
  }
  return {
    settings: null,
    bayesianEnabled: undefined,
    sequentialEnabled: undefined,
    lowerIsBetter: undefined,
    eventNames: {},
  };
}

export const RESULTS_LOAD_FAILED = 'The results could not be loaded.';
export const RESULTS_NOT_FOUND = 'No results were found for this experiment.';
export const RESULTS_FORBIDDEN = 'You do not have access to these results.';
export const RESULTS_SERVER_ERROR =
  'The server could not compute the results. Try again; if it keeps failing, check the API logs.';

/**
 * Fixed copy for a failed results load. The server's text is never shown: a
 * 5xx body can carry a stack trace, and a 4xx detail can be out of date. An
 * unreachable API keeps its own message, which the dashboard writes itself.
 */
export function resultsErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 0) return e.message;
    if (e.status === 404) return RESULTS_NOT_FOUND;
    if (e.status === 403) return RESULTS_FORBIDDEN;
    if (e.status >= 500) return RESULTS_SERVER_ERROR;
  }
  return RESULTS_LOAD_FAILED;
}

type Tab = 'overview' | 'trends' | 'sample-size' | 'sequential' | 'breakdowns' | 'live';

/**
 * The chart heading says what the bars are. A revenue metric analysed as a
 * mean is headed "revenue per user"; a count, duration or custom metric is
 * analysed as a share of users with an event, so it is headed as that, never
 * as a "conversion rate" of its values.
 */
function primaryMetricHeading(metric: MetricResult): string {
  if (analysedAsMean(metric)) return `${metric.metric_name} — ${meanDescription(metric)}`;
  if (metric.metric_type === 'conversion') return `Conversion Rates — ${metric.metric_name}`;
  return `${metric.metric_name} — ${rateDescription(metric)}`;
}

/**
 * In the Bayesian panel, under its heading, when the primary metric is
 * analysed as a mean: the Bayesian analysis stays on the share of users who
 * sent the metric's event.
 */
function bayesianShareNote(metric: MetricResult, eventName?: string): React.ReactNode {
  return (
    <>
      The Bayesian analysis compares the share of users who sent{' '}
      {eventName ? <code>{eventName}</code> : `${metric.metric_name}'s event`}, not{' '}
      {meanDescription(metric)}.
    </>
  );
}

function LoadingSkeleton() {
  return (
    <div className="space-y-6 animate-pulse" data-testid="loading-skeleton">
      <div className="h-36 bg-slate-200 rounded-xl" />
      <div className="h-8 bg-slate-200 rounded w-64" />
      <div className="h-72 bg-slate-200 rounded-xl" />
      <div className="h-48 bg-slate-200 rounded-xl" />
    </div>
  );
}

export function ResultsDashboard({ experimentId }: ResultsDashboardProps) {
  const [results, setResults] = useState<ExperimentResultsResponse | null>(null);
  const [daily, setDaily] = useState<DailyResultsResponse | null>(null);
  // The sample size loads on its own, outside the page's Promise.all (#666):
  // when it fails, the Sample Size tab says so and the rest of the page renders.
  const [sampleSize, setSampleSize] = useState<SampleSizeResult | null>(null);
  const [sampleSizeLoading, setSampleSizeLoading] = useState(true);
  const [sampleSizeError, setSampleSizeError] = useState<string | null>(null);
  // What the user changed on the tab. Nothing is saved, and nothing goes in
  // the URL (#666): without overrides the server decides every input.
  const [sampleSizeOverrides, setSampleSizeOverrides] = useState<SampleSizeOverrides>({});
  const sampleSizeRequest = useRef(0);
  // The plan with the server's own inputs, for the Planned sample card
  // (#1244): what the user tries on the tab never changes the card. Undefined
  // while it loads, null when it could not be loaded.
  const [plannedSample, setPlannedSample] = useState<SampleSizeResult | null | undefined>(
    undefined,
  );
  const plannedSampleRequest = useRef(0);
  const [sequential, setSequential] = useState<SequentialTestingResponse | null>(null);
  const [lowerIsBetter, setLowerIsBetter] = useState<boolean | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>('overview');
  // Issue #28: Breakdown state
  const [selectedBreakdown, setSelectedBreakdown] = useState<string | null>(null);
  const [breakdown, setBreakdown] = useState<DimensionalBreakdownResponse | null>(null);
  const [breakdownLoading, setBreakdownLoading] = useState(false);
  // The settings every results request sends (D49), once the experiment is read.
  const [stored, setStored] = useState<StoredAnalysisSettings | null>(null);
  const [bayesianEnabled, setBayesianEnabled] = useState<boolean | undefined>(undefined);
  const [eventNames, setEventNames] = useState<Record<string, string>>({});

  const fetchSampleSize = useCallback(async (overrides: SampleSizeOverrides = {}) => {
    // Only the latest request may write state, so a slow answer for an
    // earlier experiment never lands on this one.
    const request = ++sampleSizeRequest.current;
    const planned = Object.keys(overrides).length === 0;
    const plannedRequest = planned ? ++plannedSampleRequest.current : 0;
    setSampleSizeOverrides(overrides);
    setSampleSizeLoading(true);
    setSampleSizeError(null);
    try {
      // Only what the user changed is sent; with nothing changed, nothing is.
      const s = planned
        ? await ResultsService.getSampleSize(experimentId)
        : await ResultsService.getSampleSize(experimentId, overrides);
      if (planned && plannedRequest === plannedSampleRequest.current) setPlannedSample(s);
      if (request !== sampleSizeRequest.current) return;
      setSampleSize(s);
    } catch (e) {
      if (planned && plannedRequest === plannedSampleRequest.current) setPlannedSample(null);
      if (request !== sampleSizeRequest.current) return;
      setSampleSize(null);
      setSampleSizeError(e instanceof Error ? e.message : 'Request failed');
    } finally {
      if (request === sampleSizeRequest.current) setSampleSizeLoading(false);
    }
  }, [experimentId]);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    // Started alongside the results, but never awaited with them. A reload
    // starts from the server's inputs again: nothing typed on the tab is kept.
    setSampleSize(null);
    setPlannedSample(undefined);
    void fetchSampleSize();
    try {
      // The dashboard sends the experiment's stored correction and confidence
      // level. If the experiment cannot be read, the results are still asked
      // for, with no settings, and the server uses the stored ones.
      const experimentRequest = readExperiment(experimentId);
      const resultsRequest = experimentRequest.then((reading) => {
        const { settings } = reading;
        setStored(settings);
        setBayesianEnabled(reading.bayesianEnabled);
        setEventNames(reading.eventNames);
        return settings
          ? ResultsService.getResults(experimentId, settings)
          : ResultsService.getResults(experimentId);
      });
      const [r, d] = await Promise.all([
        resultsRequest,
        ResultsService.getDailyResults(experimentId),
      ]);
      setResults(r);
      setDaily(d);

      // The results carry the sequential block whenever sequential testing is
      // on. The dedicated route is asked only when the experiment says it is
      // on and the block is still missing (an API that does not embed it, or
      // an analysis that failed). When it is off, that route answers 404, and
      // asking it anyway logged a failed request on every results page (#919).
      // `readExperiment` never rejects, so this await is immediate.
      const { sequentialEnabled, lowerIsBetter: lower } = await experimentRequest;
      setLowerIsBetter(lower);
      if (r.sequential_testing) {
        setSequential(r.sequential_testing);
      } else if (sequentialEnabled === true) {
        try {
          setSequential(await ResultsService.getSequentialResults(experimentId));
        } catch {
          setSequential(null);
        }
      } else {
        setSequential(null);
      }
    } catch (e) {
      setError(resultsErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [experimentId, fetchSampleSize]);

  // Issue #28: Fetch breakdown when dimension changes
  const fetchBreakdown = useCallback(async (dim: string | null) => {
    if (!dim) {
      setBreakdown(null);
      return;
    }
    setBreakdownLoading(true);
    try {
      const r = await ResultsService.getResults(
        experimentId,
        stored ? { breakdown: dim, ...stored } : { breakdown: dim }
      );
      setBreakdown(r.breakdown ?? null);
    } catch {
      setBreakdown(null);
    } finally {
      setBreakdownLoading(false);
    }
  }, [experimentId, stored]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // Issue #28: fetch breakdown when selectedBreakdown changes
  useEffect(() => {
    fetchBreakdown(selectedBreakdown);
  }, [selectedBreakdown, fetchBreakdown]);

  if (loading) return <LoadingSkeleton />;

  if (error) {
    return (
      <div
        className="text-center py-16 space-y-4"
        data-testid="error-state"
        role="alert"
      >
        <p className="text-red-600 font-medium">{error}</p>
        <button
          onClick={fetchAll}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          Retry
        </button>
      </div>
    );
  }

  if (!results) return null;

  const primaryMetric = results.metrics.find((m) => m.is_primary) ?? results.metrics[0];
  const primaryAsMean = primaryMetric ? analysedAsMean(primaryMetric) : false;

  const TABS: { id: Tab; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'live', label: '⚡ Live' },
    { id: 'trends', label: 'Trends' },
    { id: 'sample-size', label: 'Sample Size' },
    ...(sequential ? [{ id: 'sequential' as Tab, label: 'Sequential' }] : []),
    { id: 'breakdowns', label: 'Breakdowns' },
  ];

  return (
    <div className="space-y-6" data-testid="results-dashboard">
      <ExperimentSummary
        experiment={results}
        stored={stored}
        onOpenSampleSize={() => setActiveTab('sample-size')}
        plannedSample={plannedSample}
      />
      <SrmNotice srm={results.srm} metrics={results.metrics} />

      {/* Tab navigation */}
      <nav
        className="flex gap-1 border-b border-slate-200"
        aria-label="Results sections"
        role="tablist"
      >
        {TABS.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            aria-controls={`panel-${tab.id}`}
            onClick={() => setActiveTab(tab.id)}
            className={`px-4 py-2 text-sm font-medium rounded-t border-b-2 transition-colors ${
              activeTab === tab.id
                ? 'border-blue-600 text-blue-600'
                : 'border-transparent text-slate-600 hover:text-slate-900 hover:border-slate-300'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {/* Tab panels */}
      <div id={`panel-${activeTab}`} role="tabpanel">
        {/* EP-058: Live streaming results panel */}
        {activeTab === 'live' && (
          <section aria-label="Live streaming results" data-testid="live-tab-panel">
            <LiveResultsPanel experimentId={experimentId} />
          </section>
        )}

        {activeTab === 'overview' && (
          <div className="space-y-8">
            {/* No data guard */}
            {!results.metrics.length ? (
              <p className="text-slate-500 text-sm">No metrics available</p>
            ) : (
              <>
                {primaryMetric && (
                  <section aria-labelledby="primary-metric-heading">
                    <h3
                      id="primary-metric-heading"
                      className="text-base font-semibold text-slate-800 mb-4"
                    >
                      {primaryMetricHeading(primaryMetric)}
                    </h3>
                    <ConversionChart
                      variants={primaryMetric.variants}
                      analysis={primaryAsMean ? 'mean' : 'proportion'}
                      metricType={primaryMetric.metric_type}
                    />
                  </section>
                )}
                <section aria-label="Metric comparison">
                  <h3 className="text-base font-semibold text-slate-800 mb-4">
                    All Metrics
                  </h3>
                  <MetricComparisonTable
                    metrics={results.metrics}
                    confidenceLevel={results.confidence_level}
                    correctionMethod={results.correction_method}
                    eventNames={eventNames}
                  />
                </section>
              </>
            )}
            <BayesianPanel
              bayesian={results.bayesian_results}
              bayesianEnabled={bayesianEnabled}
              srmWarning={results.srm?.warning === true}
              note={
                primaryMetric && primaryAsMean
                  ? bayesianShareNote(primaryMetric, eventNames[primaryMetric.metric_id])
                  : undefined
              }
            />
          </div>
        )}

        {activeTab === 'trends' && (
          <section aria-label="Trends over time">
            <h3 className="text-base font-semibold text-slate-800 mb-4">
              Daily Trends
            </h3>
            <TrendChart series={daily?.series ?? []} />
          </section>
        )}

        {activeTab === 'sample-size' && (
          <section
            className="max-w-lg"
            aria-label="Sample size adequacy"
          >
            <h3 className="text-base font-semibold text-slate-800 mb-4">
              Sample Size Analysis
            </h3>
            {sampleSizeLoading && !sampleSize ? (
              <div
                className="h-24 bg-slate-200 rounded-xl animate-pulse"
                data-testid="sample-size-loading"
                aria-label="Loading the sample size"
              />
            ) : sampleSizeError || !sampleSize ? (
              <div
                className="space-y-3 text-sm"
                data-testid="sample-size-error"
                role="alert"
              >
                <p className="text-red-700 font-medium">
                  The sample size could not be loaded. The other tabs are not affected.
                </p>
                {sampleSizeError && (
                  <p className="text-slate-600">{sampleSizeError}</p>
                )}
                <button
                  type="button"
                  onClick={() => void fetchSampleSize(sampleSizeOverrides)}
                  className="px-3 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
                >
                  Try again
                </button>
              </div>
            ) : (
              <SampleSizeMeter
                data={sampleSize}
                eventName={sampleSize.metric_id ? eventNames[sampleSize.metric_id] : undefined}
                overrides={sampleSizeOverrides}
                onRecalculate={(o) => void fetchSampleSize(o)}
                recalculating={sampleSizeLoading}
              />
            )}
          </section>
        )}

        {activeTab === 'sequential' && sequential && (
          <section aria-label="Sequential testing">
            <h3 className="text-base font-semibold text-slate-800 mb-4">
              Sequential Testing Monitor
            </h3>
            <SequentialMonitor data={sequential} lowerIsBetter={lowerIsBetter} />
          </section>
        )}

        {/* Issue #28: Breakdowns tab */}
        {activeTab === 'breakdowns' && (
          <section aria-label="Segment breakdowns">
            <div className="space-y-6">
              <div>
                <h3 className="text-base font-semibold text-slate-800 mb-4">
                  Segment Breakdowns
                </h3>
                <BreakdownSelector
                  value={selectedBreakdown}
                  onChange={setSelectedBreakdown}
                />
              </div>

              {breakdownLoading && (
                <div className="animate-pulse space-y-2">
                  <div className="h-8 bg-slate-200 rounded w-full" />
                  <div className="h-48 bg-slate-200 rounded w-full" />
                </div>
              )}

              {!breakdownLoading && breakdown && (
                <SegmentComparisonTable breakdown={breakdown} />
              )}

              {!breakdownLoading && !breakdown && selectedBreakdown && (
                <p className="text-sm text-slate-500">
                  No breakdown data available for the selected dimension.
                </p>
              )}

              {!selectedBreakdown && (
                <p className="text-sm text-slate-500">
                  Select a dimension above to view segment-level results.
                </p>
              )}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
