import {
  BalancedGroupsResponse,
  CorrectionMethod,
  ExperimentResultsResponse,
  DailyResultsResponse,
  SampleSizeOverrides,
  SampleSizeResult,
} from '@/types/results';
import { SequentialTestingResponse } from '@/types/sequential';
import { LONG_REQUEST_TIMEOUT_MS, apiDownload, apiFetch, type ApiDownload } from '@/services/api';

// Every results call waits LONG_REQUEST_TIMEOUT_MS: computing results on a
// large experiment can take longer than the default, and each is a read or
// safe to repeat.

/** Query for `GET /results/{id}`. A setting left out is the experiment's stored one. */
export interface ResultsQuery {
  breakdown?: string;
  correction_method?: CorrectionMethod;
  confidence_level?: number;
}

export class ResultsService {
  static async getResults(
    experimentId: string,
    params?: ResultsQuery
  ): Promise<ExperimentResultsResponse> {
    return apiFetch<ExperimentResultsResponse>(`/api/v1/results/${experimentId}`, {
      query: {
        breakdown: params?.breakdown || undefined,
        correction_method: params?.correction_method,
        confidence_level: params?.confidence_level,
      },
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    });
  }

  static async getDailyResults(
    experimentId: string,
    metricId?: string
  ): Promise<DailyResultsResponse> {
    return apiFetch<DailyResultsResponse>(`/api/v1/results/${experimentId}/daily`, {
      query: { metric_id: metricId || undefined },
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    });
  }

  /**
   * The planned sample size. Send only what the user changed: anything left
   * out is decided by the server (the observed control rate, the effect saved
   * with the experiment as its primary metric's `expected_effect`, or a 5%
   * relative MDE when none is saved, 80% power, and the experiment's own
   * confidence level and correction).
   */
  static async getSampleSize(
    experimentId: string,
    overrides?: SampleSizeOverrides
  ): Promise<SampleSizeResult> {
    return apiFetch<SampleSizeResult>(`/api/v1/results/${experimentId}/sample-size`, {
      query: {
        baseline_conversion_rate: overrides?.baseline_conversion_rate,
        mde: overrides?.mde,
        power_target: overrides?.power_target,
        confidence_level: overrides?.confidence_level,
        correction_method: overrides?.correction_method,
      },
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    });
  }

  /**
   * The results of an experiment that balances its split within groups, by
   * the group each user was balanced in (beta, #448). The server uses the
   * experiment's stored confidence level and correction; an experiment that
   * does not balance answers 409.
   */
  static async getBalancedGroups(experimentId: string): Promise<BalancedGroupsResponse> {
    return apiFetch<BalancedGroupsResponse>(`/api/v1/results/${experimentId}/balanced-groups`, {
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    });
  }

  static async getSequentialResults(
    experimentId: string
  ): Promise<SequentialTestingResponse> {
    return apiFetch<SequentialTestingResponse>(`/api/v1/results/${experimentId}/sequential`, {
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    });
  }

  /**
   * The experiment's variant rows as a CSV file (`GET
   * /export/reports/experiments/{id}?format=csv`). `format` must be sent: with
   * none the API answers JSON. One call is one request against the export
   * limit, which every export path shares.
   */
  static async exportCsv(experimentId: string): Promise<ApiDownload> {
    return apiDownload(`/api/v1/export/reports/experiments/${experimentId}`, { format: 'csv' });
  }

  static async invalidateCache(
    experimentId: string
  ): Promise<{ status: string; experiment_id: string }> {
    return apiFetch<{ status: string; experiment_id: string }>(
      `/api/v1/results/${experimentId}/invalidate-cache`,
      { method: 'POST', timeoutMs: LONG_REQUEST_TIMEOUT_MS }
    );
  }
}
