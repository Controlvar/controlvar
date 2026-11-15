/**
 * When a key made on "Your API keys" stops working.
 *
 * - A key with the `mcp:read` scope: 30 days, 90 days (the default) or 1 year.
 *   "No expiry" is not offered.
 * - Any other key: the same three, plus "No expiry", which is the default, as
 *   on every other way of creating a key.
 *
 * The page sends the chosen time as UTC ISO 8601 ending in `Z`
 * (`Date.prototype.toISOString()`), and sends nothing for "No expiry".
 */

export type ExpiryChoice = '30' | '90' | '365' | 'never';

export interface ExpiryOption {
  value: ExpiryChoice;
  label: string;
  /** Days from now; `null` for "No expiry". */
  days: number | null;
}

const DAY_MS = 24 * 60 * 60 * 1000;

const DAYS_30: ExpiryOption = { value: '30', label: '30 days', days: 30 };
const DAYS_90: ExpiryOption = { value: '90', label: '90 days', days: 90 };
const DAYS_365: ExpiryOption = { value: '365', label: '1 year (365 days)', days: 365 };
const NEVER: ExpiryOption = { value: 'never', label: 'No expiry', days: null };

export const EXPIRY_OPTIONS_WITH_MCP_READ: readonly ExpiryOption[] = [DAYS_30, DAYS_90, DAYS_365];
export const EXPIRY_OPTIONS_WITHOUT_MCP_READ: readonly ExpiryOption[] = [DAYS_30, DAYS_90, DAYS_365, NEVER];

export const DEFAULT_EXPIRY_WITH_MCP_READ: ExpiryChoice = '90';
export const DEFAULT_EXPIRY_WITHOUT_MCP_READ: ExpiryChoice = 'never';

/** The choices offered, given whether `mcp:read` is ticked. */
export function expiryOptions(mcpRead: boolean): readonly ExpiryOption[] {
  return mcpRead ? EXPIRY_OPTIONS_WITH_MCP_READ : EXPIRY_OPTIONS_WITHOUT_MCP_READ;
}

export function defaultExpiry(mcpRead: boolean): ExpiryChoice {
  return mcpRead ? DEFAULT_EXPIRY_WITH_MCP_READ : DEFAULT_EXPIRY_WITHOUT_MCP_READ;
}

/**
 * The choice in effect: what the user picked, if it is offered for this key,
 * otherwise the default. `null` means the user has not picked one.
 */
export function effectiveExpiry(picked: ExpiryChoice | null, mcpRead: boolean): ExpiryChoice {
  if (picked !== null && expiryOptions(mcpRead).some((option) => option.value === picked)) return picked;
  return defaultExpiry(mcpRead);
}

/** What the page says under the control: the choices and the default. */
export function expiryHelp(mcpRead: boolean): string {
  return mcpRead
    ? 'A key with mcp:read expires after 30 days, 90 days or 1 year. The default is 90 days.'
    : 'The default is No expiry. You can choose 30 days, 90 days or 1 year instead.';
}

/**
 * The `expires_at` to send: now plus the chosen days, in UTC ending in `Z`;
 * `undefined` for "No expiry", so the field is left out of the request.
 */
export function expiresAtFor(choice: ExpiryChoice, now: Date = new Date()): string | undefined {
  const option = EXPIRY_OPTIONS_WITHOUT_MCP_READ.find((o) => o.value === choice);
  if (!option || option.days === null) return undefined;
  return new Date(now.getTime() + option.days * DAY_MS).toISOString();
}
