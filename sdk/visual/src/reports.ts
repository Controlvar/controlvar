/**
 * Error reports (tests/sdk-contract/visual-error-reports.json lists them).
 * Each is one POST /api/v1/tracking/errors with exactly error_type,
 * experiment_key, message and metadata: never a feature flag key, the
 * visitor's id, the page's address or its text. At most 5 per page view,
 * all types together, and none while the script is silent (send's gates).
 */
import type { Send } from "./send";

export const ERRORS_PATH = "/api/v1/tracking/errors";
export const MAX_REPORTS = 5;

export type NotApplied =
  | "invalid"
  | "unsupported_operation"
  | "not_found"
  | "ambiguous"
  | "not_allowed"
  | "reapply_limit"
  | "replaced_after_bound";

/** One fixed message per reason: nothing from the page or the change goes in it. */
export const MESSAGES: Record<NotApplied, string> = {
  invalid: "A visual change was not applied: it failed the script's check.",
  unsupported_operation: "A visual change was not applied: this version of the script does not know its operation.",
  not_found: "A visual change was not applied: its selector matched no element.",
  ambiguous: "A visual change was not applied: its selector matched more than one element.",
  not_allowed: "A visual change was not applied: the script does not change that element this way.",
  reapply_limit: "A visual change stopped being applied: the page kept undoing it.",
  replaced_after_bound: "A visual change was lost: the page replaced the element after the script stopped watching.",
};

export interface Reporter {
  notApplied(experimentKey: string, index: number | null, reason: NotApplied): void;
}

export function createReporter(send: Send): Reporter {
  let sent = 0;
  return {
    notApplied(experimentKey, index, reason) {
      if (sent >= MAX_REPORTS) return;
      const request = send(
        ERRORS_PATH,
        {
          error_type: "visual_change_not_applied",
          experiment_key: experimentKey,
          message: MESSAGES[reason],
          metadata: { index, reason },
        },
        { apiKey: true },
      );
      if (request === null) return;
      sent += 1;
      request.then(
        () => undefined,
        () => undefined,
      );
    },
  };
}
