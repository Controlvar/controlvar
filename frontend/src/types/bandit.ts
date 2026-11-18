/**
 * `GET /api/v1/bandit/{experiment_id}` (`BanditStatusResponse` in
 * `backend/app/schemas/bandit.py`). Only the fields the dashboard reads are
 * typed here.
 */

/** One variant's current share of new traffic, and the counts behind it. */
export interface BanditVariantWeight {
  variant_id: string;
  variant_name: string;
  /** Share of new users in [0, 1]. */
  current_weight: number;
  successes: number;
  pulls: number;
  /** successes / pulls, in [0, 1]. */
  conversion_rate: number;
}

/**
 * One entry of `GET /api/v1/bandit/{experiment_id}/history` (beta;
 * `BanditHistoryTick` in `backend/app/schemas/bandit.py`): one weight update by
 * the bandit. The route answers a bare array, newest first.
 */
export interface BanditHistoryTick {
  /** When the update ran (ISO-8601, UTC). */
  tick_at: string;
  /** Each variant's weight in [0, 1], keyed by variant id. */
  weights: Record<string, number>;
}

export interface BanditStatus {
  experiment_id: string;
  algorithm: string;
  current_weights: BanditVariantWeight[];
  total_pulls: number;
  recommendation: string;
  /**
   * When the weights were last computed (ISO-8601), or `null` before the
   * first update. Before then the server fills `current_weights` with an even
   * split it is not using: new users follow each variant's traffic allocation.
   */
  last_updated: string | null;
}
