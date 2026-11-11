import { ApiError, messageForDetail } from '@/services/api';
import { BalanceFormState, BalanceIssue, takeBalanceIssue } from './balanceWithin';

/**
 * What a failed create shows, in either view of `/experiments/new`.
 *
 * Mapped by HTTP status only. The API answers a taken key with 409 and a
 * plain message (#395), so nothing here reads the words of an error to decide
 * what it is, and the 409's own text is never shown: the page says it in its
 * own words, with the key it sent.
 */
export interface CreateError {
  message: string;
  /** The problem is the key: offer "Edit details", which goes to the key field. */
  editDetails?: boolean;
  /**
   * The problem is the targeting rules: one line per problem, shown at the
   * targeting rules (not at the foot of the form), with `message` as its heading.
   */
  targeting?: string[];
  /**
   * With `targeting`: what the same 422 said about other fields, shown at the
   * foot of the form as any other error is, so a mixed 422 loses nothing.
   */
  otherMessage?: string;
  /**
   * The problem is the setting that balances the split within groups: the
   * API's own sentence, shown at the control it is about (`message` holds the
   * same sentence). With `targeting` or alone, `otherMessage` holds what the
   * same 422 said about other fields.
   */
  balance?: BalanceIssue;
}

/** The text a view shows at the foot of the form for `error`, or null for none. */
export function footMessage(error: CreateError | null): string | null {
  if (!error) return null;
  if (error.targeting || error.balance) return error.otherMessage ?? null;
  return error.message;
}

/** What `describeCreateError` needs to tell which control a refusal of the balance setting is about. */
export interface BalanceContext {
  balance: BalanceFormState;
  allocations: number[];
}

/** Heading for targeting problems found before the create was sent. */
export const TARGETING_INCOMPLETE = 'Finish or remove these targeting conditions before creating the experiment:';

/** Heading for targeting rules the API refused (422 on `targeting_rules`). */
export const TARGETING_REFUSED = 'The targeting rules were not accepted:';

/**
 * One API targeting problem in the builder's words.
 *
 * The API's message is fixed text keyed by path, e.g.
 * `Value error, groups[0].conditions[1].operator: unknown operator`; the
 * builder numbers from 1, so that reads "Group 1, Condition 2: unknown operator".
 */
export function describeTargetingIssue(msg: string): string {
  const text = msg.replace(/^Value error,\s*/, '').trim();
  const m = /^groups\[(\d{1,4})\](?:\.conditions(?:\[(\d{1,4})\])?)?(?:\.[a-z_]{1,32})?: (.{1,200})$/.exec(text);
  if (!m) return text;
  const [, group, condition, problem] = m;
  const where =
    condition === undefined
      ? `Group ${Number(group) + 1}`
      : `Group ${Number(group) + 1}, Condition ${Number(condition) + 1}`;
  return `${where}: ${problem}`;
}

function isTargetingItem(item: unknown): boolean {
  if (!item || typeof item !== 'object') return false;
  const { loc } = item as { loc?: unknown };
  return Array.isArray(loc) && loc[loc.length - 1] === 'targeting_rules';
}

/**
 * A 422 body split in two: its `targeting_rules` problems in the builder's
 * words, and the message for every other item (undefined when there are none).
 * Null when the body names no targeting problem.
 */
function splitTargetingIssues(detail: unknown): { targeting: string[]; otherMessage?: string } | null {
  if (!Array.isArray(detail)) return null;
  const targeting = detail.filter(isTargetingItem).map((item) => {
    const { msg } = item as { msg?: unknown };
    return describeTargetingIssue(typeof msg === 'string' ? msg : 'not valid');
  });
  if (targeting.length === 0) return null;
  const others = detail.filter((item) => !isTargetingItem(item));
  return others.length > 0 ? { targeting, otherMessage: messageForDetail(422, others) } : { targeting };
}

export const ROLE_CANNOT_CREATE =
  'Your role can view experiments but not create them. Ask an admin to create it or to change your role.';

export const SESSION_EXPIRED_CREATE =
  'Your session has expired. Sign in again in another tab, then press Create Experiment again. Your answers are still here.';

/**
 * A create that failed without an answer from the API (an exception in the
 * browser). The exception's own text is never shown; whether the experiment
 * was saved is unknown, so the list is the place to check.
 */
export const CREATE_FAILED =
  'Something went wrong while creating the experiment. Check the experiments list before trying again.';

export type CreateView = 'guided' | 'advanced';

/** The duplicate-key copy. `key` is the key the page sent. */
export function duplicateKeyMessage(key: string | undefined, view: CreateView): string {
  const head = key
    ? `An experiment with the key “${key}” already exists.`
    : 'An experiment with this key already exists.';
  return view === 'guided'
    ? `${head} Choose a different key on the Details step.`
    : `${head} Choose a different key.`;
}

function sentence(text: string): string {
  const t = text.trim();
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

/**
 * The error a view shows for a create that failed with `err`.
 *
 * - 401: the session copy. The create is sent with `redirectOn401: false`, so
 *   the page stays and the answers with it.
 * - 403: the API's reason, then what the role means and who can help.
 * - 409: the key is taken; the page's own sentence and "Edit details".
 * - 422 naming `targeting_rules`: each problem, shown at the targeting rules;
 *   any other item in the same 422 is kept in `otherMessage`.
 * - any other API answer: the message the API client built (for a server
 *   error it carries the request ID once).
 * - an exception that is not an API answer: `CREATE_FAILED`.
 */
export function describeCreateError(
  err: unknown,
  sentKey: string | undefined,
  view: CreateView,
  balanceContext?: BalanceContext,
): CreateError {
  if (err instanceof ApiError) {
    if (err.status === 401) return { message: SESSION_EXPIRED_CREATE };
    if (err.status === 403) return { message: `${sentence(err.message)} ${ROLE_CANNOT_CREATE}` };
    if (err.status === 409) return { message: duplicateKeyMessage(sentKey, view), editDetails: true };
    if (err.status === 422) {
      // A refusal of the balance setting is shown, in the API's words, at its control;
      // the rest of the body is read as before.
      const balance = balanceContext
        ? takeBalanceIssue(err.detail, balanceContext.balance, balanceContext.allocations)
        : { issue: null, rest: undefined };
      const split = splitTargetingIssues(balance.issue ? balance.rest : err.detail);
      if (balance.issue) {
        const otherMessage =
          split?.otherMessage ??
          (!split && balance.rest && balance.rest.length > 0 ? messageForDetail(422, balance.rest) : undefined);
        return {
          message: split ? TARGETING_REFUSED : balance.issue.message,
          ...(split ? { targeting: split.targeting } : {}),
          balance: balance.issue,
          ...(otherMessage !== undefined ? { otherMessage } : {}),
        };
      }
      if (split) return { message: TARGETING_REFUSED, ...split };
    }
  }
  if (err instanceof ApiError) return { message: err.message };
  return { message: CREATE_FAILED };
}
