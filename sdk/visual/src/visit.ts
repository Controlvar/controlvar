/**
 * The visit, kept in localStorage `controlvar_visit` and shared by every tab
 * of the site: `{started, last_seen, utm?, landing?}`, nothing else. A visit
 * ends after 30 minutes without a page view; the next page view starts a new
 * one. A page view is each page load the script starts on and each path
 * change it follows, whether or not it makes a call.
 *
 * The campaign values (`utm`) and the landing path (`landing`) are the visit's
 * first page's, each kept only in its form, so a later page of the same visit
 * never changes them. localStorage `controlvar_seen` holds the time this
 * browser was first seen, written once, when it is absent, as a visit starts:
 * a visit that started after it is a returning visitor's.
 *
 * Which tag values write what: `controlvar_visit` only with `visitor`, `utm`
 * or `landing`; `controlvar_seen` only with `visitor`. Anything read back is
 * checked again and dropped if it fails, because a page can write these keys.
 * When the browser refuses storage, the visit's details are left out, never
 * filled in from the current page.
 */
import { campaignFrom, landingPathKept, utmKept, UTM_PARAMS, type Campaign, type Collect, type Visitor } from "./details";
import { KEYS } from "./keys";

export const VISIT_MINUTES = 30;
export const VISIT_MS = VISIT_MINUTES * 60 * 1000;

/** The visit's details for one call. */
export interface VisitDetails {
  visitor: Visitor | null;
  campaign: Campaign;
  landing: string | null;
}

interface Visit {
  started: number;
  last_seen: number;
  utm?: Campaign;
  landing?: string;
}

/** The page the visit is touched from. */
export interface VisitPage {
  search: string;
  pathname: string;
}

function isTime(value: unknown, now: number): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 && value <= now;
}

function cleanCampaign(value: unknown): Campaign {
  const campaign: Campaign = {};
  if (typeof value !== "object" || value === null || Array.isArray(value)) return campaign;
  for (const [param] of UTM_PARAMS) {
    const kept = (value as Record<string, unknown>)[param];
    if (utmKept(kept)) campaign[param] = kept;
  }
  return campaign;
}

/** The stored visit if it is still going on (checked again), else null. */
function current(text: string | null, now: number): Visit | null {
  if (text === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  const { started, last_seen: lastSeen, utm, landing } = parsed as Record<string, unknown>;
  if (!isTime(started, now) || !isTime(lastSeen, now) || started > lastSeen) return null;
  if (now - lastSeen > VISIT_MS) return null;
  const visit: Visit = { started, last_seen: lastSeen, utm: cleanCampaign(utm) };
  if (landingPathKept(landing)) visit.landing = landing;
  return visit;
}

function seenTime(text: string | null, now: number): number | null {
  if (text === null || !/^[0-9]{1,16}$/.test(text)) return null;
  const time = Number(text);
  return isTime(time, now) ? time : null;
}

/**
 * One page view of the visit: reads `controlvar_visit`, starts a new visit
 * or moves `last_seen`, writes it back with only checked values, and answers
 * the visit's details. Null when storage is refused or a write fails.
 * `allowed` is asked before every write.
 */
export function touchVisit(
  storage: Storage | null,
  now: number,
  page: VisitPage,
  collect: Collect,
  allowed: () => boolean,
): VisitDetails | null {
  if (!collect.visitor && !collect.utm && !collect.landing) return { visitor: null, campaign: {}, landing: null };
  if (!storage) return null;
  try {
    let visit = current(storage.getItem(KEYS.visit), now);
    const starts = visit === null;
    if (visit === null) {
      visit = { started: now, last_seen: now };
      if (collect.utm) visit.utm = campaignFrom(page.search);
      if (collect.landing && landingPathKept(page.pathname)) visit.landing = page.pathname;
    } else {
      visit.last_seen = now;
    }
    const stored: Visit = { started: visit.started, last_seen: visit.last_seen };
    const campaign = collect.utm && visit.utm ? visit.utm : {};
    if (collect.utm && Object.keys(campaign).length > 0) stored.utm = campaign;
    const landing = collect.landing && visit.landing !== undefined ? visit.landing : null;
    if (landing !== null) stored.landing = landing;
    if (!allowed()) return null;
    storage.setItem(KEYS.visit, JSON.stringify(stored));

    let visitor: Visitor | null = null;
    if (collect.visitor) {
      let seen = seenTime(storage.getItem(KEYS.seen), now);
      if (seen === null && starts) {
        if (!allowed()) return null;
        storage.setItem(KEYS.seen, String(visit.started));
        seen = visit.started;
      }
      if (seen !== null) visitor = seen < visit.started ? "returning" : "new";
    }
    return { visitor, campaign, landing };
  } catch {
    // Storage refused or full: the visit's details are left out.
    return null;
  }
}
