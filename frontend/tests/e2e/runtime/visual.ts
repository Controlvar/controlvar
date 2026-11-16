import type { Page } from "@playwright/test";
import { html, snippet, type ApiAnswer, type ApiCall, type Site } from "./editor";

/**
 * Helpers for the page-runtime specs of an ordinary page view (#1545): the
 * answer of POST /api/v1/tracking/visual, the site's pages, and what the
 * script sent.
 */
export const VISUAL_PATH = "/api/v1/tracking/visual";
export const ERRORS_PATH = "/api/v1/tracking/errors";
export const BATCH_PATH = "/api/v1/tracking/batch";

export interface Experiment {
  experiment_key: string;
  variant_id: string;
  variant_name: string;
  assigned: boolean;
  reason: string;
  changes: unknown;
  goals: unknown[];
}

export function experiment(key: string, changes: unknown[], extra: Partial<Experiment> = {}): Experiment {
  return {
    experiment_key: key,
    variant_id: "v1",
    variant_name: "B",
    assigned: true,
    reason: "assigned",
    changes: { version: 1, changes },
    goals: [],
    ...extra,
  };
}

export interface Answers {
  experiments: Experiment[];
  rules?: { match: "exact" | "prefix"; path: string }[];
  /** The S3 answer's status (200 by default). */
  status?: number;
  /** Wait for this before answering S3. */
  hold?: Promise<void>;
}

/** An answer function for routeSite: S3 per `answers`, error reports 201, batch calls 200. */
export function answering(answers: Answers): (call: ApiCall) => Promise<ApiAnswer> {
  return async (call) => {
    if (call.path === VISUAL_PATH) {
      if (answers.hold) await answers.hold;
      const status = answers.status ?? 200;
      if (status !== 200) return { status, body: { detail: "refused" } };
      const rules = answers.rules ?? [{ match: "prefix", path: "/" }];
      return { status, body: { experiments: answers.experiments, pages: { host: "localhost", rules, max_age: 60 } } };
    }
    if (call.path === ERRORS_PATH) return { status: 201, body: { id: "e" } };
    if (call.path === BATCH_PATH) return { status: 200, body: { success_count: 1, failure_count: 0, duplicate_count: 0 } };
    return { status: 404 };
  };
}

/** A page of the site with the snippet (no editor origin) first in <head>. */
export function sitePage(body: string, attributes: Record<string, string | true | null> = {}, head = ""): string {
  return html(snippet({ "data-editor-origin": null, ...attributes }) + head, body);
}

export interface Report {
  error_type: string;
  experiment_key: string;
  message: string;
  metadata: { index: number | null; reason: string };
}

export function reports(site: Site): Report[] {
  return site.api.filter((call) => call.path === ERRORS_PATH).map((call) => call.body as Report);
}

export function calls(site: Site, path: string): ApiCall[] {
  return site.api.filter((call) => call.path === path);
}

/** Whether <html> is shown (its computed visibility is not hidden). */
export async function shown(page: Page): Promise<boolean> {
  return page.evaluate(() => getComputedStyle(document.documentElement).visibility !== "hidden");
}
