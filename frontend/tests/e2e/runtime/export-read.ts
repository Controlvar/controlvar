import { basename } from "node:path";
import type { APIRequestContext } from "@playwright/test";
import { expect, test } from "./runtime.fixture";

/**
 * The one export read of a real-API spec file (#1545).
 *
 * Every /api/v1/export/ route shares one limit of 10 reads a minute per client
 * address (EXPORT_RATE_LIMIT in backend/app/middleware/rate_limiter.py), and
 * Browser E2E's API keeps that count in Redis for the whole job. So each
 * real-API spec file reads the export once, after all its page views: the list
 * of experiments, GET /api/v1/export/experiments?format=json, whose rows carry
 * the same totals as an experiment's report (ExportService._experiments_to_rows
 * builds both), for the experiments created since the file started. A
 * page-runtime run makes one read per real-API spec file.
 *
 * Each read prints one line, `page-runtime export read: <spec file> (<n>
 * experiments)`, which the list reporter writes to the job's log, so the reads
 * of a run can be counted there. A second read from the same spec file in one
 * run (and repeat) fails the test that makes it.
 */

/** How long before the spec file's start the window opens. The API and the specs read the same host clock. */
export const EXPORT_WINDOW_MARGIN_MS = 60_000;

export interface Counts {
  /** The experiment's assignment rows. */
  assignments: number;
  /** The experiment's event rows. */
  events: number;
}

interface Row {
  experiment_id: string;
  total_assignments: number;
  total_events: number;
}

/** The spec files (and repeats) that have read the export in this process. */
const done = new Set<string>();

/**
 * Where a spec file's window opens: now, less the margin, in UTC with no
 * offset. `created_at` is stored in UTC with no offset, and the database
 * would compare a date with an offset in its session's time zone.
 */
export function exportWindowStart(): string {
  return new Date(Date.now() - EXPORT_WINDOW_MARGIN_MS).toISOString().replace(/Z$/, "");
}

/**
 * The spec file's one export read: the totals of each of *experiments* (arm
 * name to experiment id), from the export's list of the experiments created
 * since *since* ({@link exportWindowStart}). Each experiment must be in the
 * list exactly once. The answer is keyed by experiment id.
 */
export async function readExport(
  api: APIRequestContext,
  headers: Record<string, string>,
  since: string,
  experiments: Record<string, string>,
): Promise<Record<string, Counts>> {
  const info = test.info();
  const file = basename(info.file);
  const key = `${info.file}#${info.repeatEachIndex}`;
  if (done.has(key)) {
    throw new Error(`a second export read in ${file}: each real-API spec file reads the export once, after its page views`);
  }
  done.add(key);
  const response = await api.get("/api/v1/export/experiments", {
    headers,
    params: { format: "json", start_date: since },
  });
  console.log(`page-runtime export read: ${file} (${Object.keys(experiments).length} experiments)`);
  expect(response.status(), `the export's list of experiments: ${await response.text()}`).toBe(200);
  const rows = (await response.json()) as Row[];
  expect(Array.isArray(rows), "the export's list of experiments is an array").toBe(true);
  const counts: Record<string, Counts> = {};
  for (const [arm, id] of Object.entries(experiments)) {
    const mine = rows.filter((row) => row.experiment_id === id);
    expect(mine.length, `${arm}: its experiment's rows in the export`).toBe(1);
    counts[id] = { assignments: mine[0].total_assignments, events: mine[0].total_events };
  }
  return counts;
}
