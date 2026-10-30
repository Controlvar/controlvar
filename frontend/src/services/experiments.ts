import {
  Experiment,
  ExperimentDesignUpdate,
  ExperimentListResponse,
  CreateExperimentRequest,
  ExperimentStatus,
} from '@/types/experiments';
import { apiFetch } from '@/services/api';

/**
 * Query parameters accepted by `GET /api/v1/experiments/`
 * (`status_filter`, `skip`, `limit`, `search`, `sort_by`, `sort_order`).
 */
export interface ExperimentListParams {
  status?: ExperimentStatus;
  skip?: number;
  limit?: number;
  search?: string;
  sort_by?: 'created_at' | 'updated_at' | 'name' | 'status';
  sort_order?: 'asc' | 'desc';
}

/**
 * Query for `GET /api/v1/experiments/analysis/sample-size`. Rates are
 * fractions (0.12, not 12) and `minimum_detectable_effect` is RELATIVE: 0.05
 * compares 12% with 12.6%, not with 17%.
 */
export interface SampleSizeQuery {
  baseline_rate: number;
  minimum_detectable_effect: number;
  statistical_power: number;
  significance_level: number;
  variant_count: number;
  /** Users per day; the duration is estimated only when this and `traffic_allocation` are both sent. */
  daily_traffic?: number;
  /** Fraction (0, 1] of `daily_traffic` that enters the experiment. */
  traffic_allocation?: number;
}

export interface SampleSizeEstimate {
  baseline_rate: number;
  minimum_detectable_effect: number;
  statistical_power: number;
  significance_level: number;
  is_one_sided: boolean;
  samples_per_variant: number;
  total_samples: number;
  estimated_duration_days: { days: number; weeks: number; months: number } | null;
  notes: string | null;
}

/**
 * The body of a details edit: the changed subset of these three fields and
 * nothing else. The update route replaces variants and metrics wholesale when
 * a request carries them, so this type has no room for either.
 */
export interface ExperimentDetailsUpdate {
  name?: string;
  description?: string | null;
  hypothesis?: string | null;
}

/** Options for `ExperimentsService.create`. */
export interface CreateOptions {
  /** Default true: a 401 sends the browser to the login page. */
  redirectOn401?: boolean;
}

/**
 * The body of a draft's schedule change (#1454): only the dates that changed.
 * Each is an instant with a `Z` (`Date.toISOString()`), or null to clear it.
 * A date the body leaves out is kept as stored. `time_zone` is never sent:
 * an instant with an offset does not need it, and the API refuses a zone
 * name it does not know.
 */
export interface ExperimentScheduleUpdate {
  start_date?: string | null;
  end_date?: string | null;
}

const BASE = '/api/v1/experiments';

export const ExperimentsService = {
  async list(params?: ExperimentListParams): Promise<ExperimentListResponse> {
    return apiFetch<ExperimentListResponse>(BASE, {
      query: {
        status_filter: params?.status,
        skip: params?.skip,
        limit: params?.limit,
        search: params?.search,
        sort_by: params?.sort_by,
        sort_order: params?.sort_order,
      },
    });
  },

  async get(id: string): Promise<Experiment> {
    return apiFetch<Experiment>(`${BASE}/${id}`);
  },

  /**
   * `POST /api/v1/experiments`. Pass `redirectOn401: false` to get the 401 as
   * an error instead of being sent to the login page; `/experiments/new` does,
   * so an expired session never costs the answers on screen.
   */
  async create(data: CreateExperimentRequest, options: CreateOptions = {}): Promise<Experiment> {
    return apiFetch<Experiment>(BASE, { method: 'POST', json: data, ...options });
  },

  /**
   * The users needed per variant, and the days that takes when daily traffic
   * is given. Advisory: nothing is stored. A 401 here does not send the user
   * to the login page, so an estimate never costs them the answers on screen.
   */
  async estimateSampleSize(query: SampleSizeQuery): Promise<SampleSizeEstimate> {
    return apiFetch<SampleSizeEstimate>(`${BASE}/analysis/sample-size`, {
      query: {
        baseline_rate: query.baseline_rate,
        minimum_detectable_effect: query.minimum_detectable_effect,
        statistical_power: query.statistical_power,
        significance_level: query.significance_level,
        variant_count: query.variant_count,
        daily_traffic: query.daily_traffic,
        traffic_allocation: query.traffic_allocation,
      },
      redirectOn401: false,
    });
  },

  async update(id: string, data: Partial<Experiment>): Promise<Experiment> {
    return apiFetch<Experiment>(`${BASE}/${id}`, { method: 'PUT', json: data });
  },

  /**
   * `PUT /api/v1/experiments/{id}` with only name, description and hypothesis.
   * The API accepts it on a DRAFT experiment; outside DRAFT it answers 400
   * to everyone but a superuser.
   */
  async updateDetails(id: string, body: ExperimentDetailsUpdate): Promise<Experiment> {
    return apiFetch<Experiment>(`${BASE}/${id}`, { method: 'PUT', json: body });
  },

  /**
   * `PUT /api/v1/experiments/{id}` with exactly `{variants, metrics}`, both
   * complete. The API replaces both lists in one commit, on a DRAFT
   * experiment only (400 otherwise, for every role); `buildDesignPayload`
   * builds the body.
   */
  async updateDesign(id: string, body: ExperimentDesignUpdate): Promise<Experiment> {
    return apiFetch<Experiment>(`${BASE}/${id}`, { method: 'PUT', json: body });
  },

  /**
   * `POST /api/v1/experiments/{id}/clone`, with no body. The answer is the new
   * experiment: a DRAFT named "Copy of …", with a key of its own, owned by the
   * caller. It needs READ on the source and CREATE (ADMIN or DEVELOPER).
   */
  async clone(id: string): Promise<Experiment> {
    return apiFetch<Experiment>(`${BASE}/${id}/clone`, { method: 'POST' });
  },

  // The route requires `experiment_key`, which must equal the experiment id
  // (a confirmation that the caller means this experiment). Only a DRAFT
  // experiment can be deleted.
  async delete(id: string): Promise<void> {
    await apiFetch<void>(`${BASE}/${id}`, {
      method: 'DELETE',
      query: { experiment_key: id },
    });
  },

  // Lifecycle transitions — each returns the updated experiment.
  // start: draft|paused → active; pause: active → paused;
  // complete: active|paused → completed; archive: any non-archived → archived.
  async start(id: string): Promise<Experiment> {
    return apiFetch<Experiment>(`${BASE}/${id}/start`, { method: 'POST' });
  },

  async pause(id: string): Promise<Experiment> {
    return apiFetch<Experiment>(`${BASE}/${id}/pause`, { method: 'POST' });
  },

  async complete(id: string): Promise<Experiment> {
    return apiFetch<Experiment>(`${BASE}/${id}/complete`, { method: 'POST' });
  },

  async archive(id: string): Promise<Experiment> {
    return apiFetch<Experiment>(`${BASE}/${id}/archive`, { method: 'POST' });
  },

  /**
   * `PUT /api/v1/experiments/{id}/schedule`. On a DRAFT, `start_date` and
   * `end_date` are when the scheduler starts and completes it; a date the
   * body leaves out is kept, and null clears it. The answer is the
   * experiment. On a PAUSED experiment the API reads `start_date` as a
   * resume time instead; the dashboard offers this on a draft only.
   */
  async schedule(id: string, body: ExperimentScheduleUpdate): Promise<Experiment> {
    return apiFetch<Experiment>(`${BASE}/${id}/schedule`, { method: 'PUT', json: body });
  },
};
