/**
 * The website details the script reads about a visitor, for the experiments'
 * audiences: the device type, the browser, the language, new or returning,
 * the referring site, the visit's campaign (utm_) values and, only when the
 * tag asks for it, its landing page. Each is a flat `web_*` key in the
 * context of the call for the page's changes, and only in that call.
 *
 * Every value takes one closed form (tests/sdk-contract/visitor-attributes.json
 * lists them; the server keeps a key only in that form). A detail that cannot
 * be read in its form is left out, never sent as null or "".
 *
 * The device type comes from the user agent string (cut to 512 characters),
 * `navigator.userAgentData.mobile` and `navigator.maxTouchPoints`. The script
 * never asks the browser for its detailed model or any other high-entropy
 * user-agent value (a unit test counts the calls in the sources: none).
 */
import type { VisitDetails } from "./visit";

/** The `data-collect` values that turn a detail on. */
export const COLLECT_TOKENS = ["device", "browser", "language", "visitor", "referrer", "utm", "landing"] as const;
export type CollectToken = (typeof COLLECT_TOKENS)[number];
/** What a tag without `data-collect` collects: everything but the landing page. */
export const DEFAULT_COLLECT: readonly CollectToken[] = ["device", "browser", "language", "visitor", "referrer", "utm"];

/** Which details the tag turns on. */
export type Collect = Readonly<Record<CollectToken, boolean>>;

export const DEVICES = ["phone", "tablet", "desktop"] as const;
export type Device = (typeof DEVICES)[number];
export const BROWSERS = ["chrome", "edge", "firefox", "safari", "samsung", "opera", "other"] as const;
export type Browser = (typeof BROWSERS)[number];
export const VISITORS = ["new", "returning"] as const;
export type Visitor = (typeof VISITORS)[number];

export const MAX_USER_AGENT_LENGTH = 512;
export const MAX_REFERRER_LENGTH = 253;
export const MAX_UTM_LENGTH = 100;
export const MAX_LANDING_PATH_LENGTH = 255;

const LANGUAGE = /^[a-z]{2,3}$/;
const REFERRER = /^[a-z0-9.-]{1,253}$/;
/** A campaign value: letters, digits and . _ ~ - only, 1 to 100 of them. Never cut. */
const UTM = /^[A-Za-z0-9._~-]{1,100}$/;
/** What a landing path may not hold: a query or fragment mark, @, whitespace or a control character. */
const LANDING_FORBIDDEN = /[?#@\s\u0000-\u001f\u007f-\u009f\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/;
const LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?:^|[^\ud800-\udbff])[\udc00-\udfff]/;

/** The campaign parameters read from the visit's first page, and the key each is sent as. */
export const UTM_PARAMS = [
  ["utm_source", "web_utm_source"],
  ["utm_medium", "web_utm_medium"],
  ["utm_campaign", "web_utm_campaign"],
] as const;
export type UtmParam = (typeof UTM_PARAMS)[number][0];
export type Campaign = Partial<Record<UtmParam, string>>;

/** What the device type is read from. */
export interface DeviceSignals {
  userAgent: string;
  /** `navigator.userAgentData.mobile`; null when the browser has no userAgentData. */
  mobile: boolean | null;
  maxTouchPoints: number;
}

/**
 * The device type, by the first rule that holds, in this order: phone when
 * userAgentData says mobile; tablet for iPad (an older iPad's user agent
 * also says Mobile); phone for iPhone or iPod; phone for Android then
 * Mobile; tablet for Tablet; phone for Mobi; tablet for Android (without
 * Mobile); tablet for a Mac with more than one touch point (an iPad that
 * reports a Mac); desktop for anything else.
 */
export function deviceFrom(signals: DeviceSignals): Device {
  const ua = signals.userAgent.slice(0, MAX_USER_AGENT_LENGTH);
  if (signals.mobile === true) return "phone";
  if (ua.indexOf("iPad") >= 0) return "tablet";
  if (ua.indexOf("iPhone") >= 0 || ua.indexOf("iPod") >= 0) return "phone";
  if (/Android.*Mobile/.test(ua)) return "phone";
  if (ua.indexOf("Tablet") >= 0) return "tablet";
  if (ua.indexOf("Mobi") >= 0) return "phone";
  if (ua.indexOf("Android") >= 0) return "tablet";
  if (ua.indexOf("Macintosh") >= 0 && signals.maxTouchPoints > 1) return "tablet";
  return "desktop";
}

/**
 * The browser, by the first of these tokens the user agent holds, in this
 * order (Edge, Opera and Samsung Internet also name Chrome; Chrome and the
 * iOS browsers also name Safari): Edg/, OPR/, SamsungBrowser/, CriOS/
 * (Chrome on iOS), FxiOS/ (Firefox on iOS), Chrome/, Firefox/, Safari/;
 * other when it holds none of them.
 */
export function browserFrom(userAgent: string): Browser {
  const ua = userAgent.slice(0, MAX_USER_AGENT_LENGTH);
  const tokens: [string, Browser][] = [
    ["Edg/", "edge"],
    ["OPR/", "opera"],
    ["SamsungBrowser/", "samsung"],
    ["CriOS/", "chrome"],
    ["FxiOS/", "firefox"],
    ["Chrome/", "chrome"],
    ["Firefox/", "firefox"],
    ["Safari/", "safari"],
  ];
  for (const [token, browser] of tokens) if (ua.indexOf(token) >= 0) return browser;
  return "other";
}

/** The primary language subtag of `navigator.language`, lower case, if it is 2 or 3 letters a-z. */
export function languageFrom(language: unknown): string | null {
  if (typeof language !== "string") return null;
  const primary = language.split("-")[0].toLowerCase();
  return LANGUAGE.test(primary) ? primary : null;
}

/**
 * The referring site: the lower-case host of `document.referrer`, never its
 * path or query. None when the referrer is empty, not an http(s) address, or
 * this page's own host, or when the host is not 1 to 253 of a-z, 0-9, . and -.
 */
export function referrerFrom(referrer: unknown, pageHostname: string): string | null {
  if (typeof referrer !== "string" || referrer === "") return null;
  let url: URL;
  try {
    url = new URL(referrer);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  const host = url.hostname.toLowerCase();
  if (host === pageHostname.toLowerCase()) return null;
  return REFERRER.test(host) ? host : null;
}

/** Whether a campaign value is kept: 1 to 100 of A-Z, a-z, 0-9, ., _, ~ and -. */
export function utmKept(value: unknown): value is string {
  return typeof value === "string" && UTM.test(value);
}

/** The campaign values of a page's query string (`location.search`), each kept only in its form. */
export function campaignFrom(search: string): Campaign {
  const params = new URLSearchParams(search);
  const campaign: Campaign = {};
  for (const [param] of UTM_PARAMS) {
    const value = params.get(param);
    if (utmKept(value)) campaign[param] = value;
  }
  return campaign;
}

/**
 * Whether a landing path is kept: it starts with /, is at most 255
 * characters, and holds no ?, #, @, %40, whitespace or control character.
 * A longer path is left out, never cut.
 */
export function landingPathKept(path: unknown): path is string {
  return (
    typeof path === "string" &&
    path.length >= 1 &&
    path.length <= MAX_LANDING_PATH_LENGTH &&
    path.charAt(0) === "/" &&
    path.indexOf("%40") < 0 &&
    !LANDING_FORBIDDEN.test(path) &&
    !LONE_SURROGATE.test(path)
  );
}

/** Every key the script sends a detail as, in the order it sends them. */
export const DETAIL_KEYS: readonly string[] = [
  "web_device",
  "web_browser",
  "web_language",
  "web_visitor",
  "web_referrer",
  ...UTM_PARAMS.map(([, key]) => key),
  "web_landing_path",
];

/** Whether *value* is a detail the script sends as *key*, in that detail's form (a value kept and read back is checked with this). */
export function detailKept(key: string, value: unknown): boolean {
  if (typeof value !== "string") return false;
  switch (key) {
    case "web_device":
      return (DEVICES as readonly string[]).indexOf(value) >= 0;
    case "web_browser":
      return (BROWSERS as readonly string[]).indexOf(value) >= 0;
    case "web_language":
      return LANGUAGE.test(value);
    case "web_visitor":
      return (VISITORS as readonly string[]).indexOf(value) >= 0;
    case "web_referrer":
      return REFERRER.test(value);
    case "web_landing_path":
      return landingPathKept(value);
    default:
      return UTM_PARAMS.some(([, utmKey]) => utmKey === key) && utmKept(value);
  }
}

/** What the page-load details are read from; each only when its detail is on. */
export interface PageSignals {
  device: DeviceSignals | null;
  userAgent: string | null;
  language: unknown;
  referrer: unknown;
}

function read<T>(get: () => T, fallback: T): T {
  try {
    return get();
  } catch {
    return fallback;
  }
}

/** Reads, from the browser, only what the details turned on need. */
export function readPageSignals(win: Window, collect: Collect): PageSignals {
  const nav = win.navigator;
  const userAgent = collect.device || collect.browser ? read(() => String(nav.userAgent), "") : null;
  let device: DeviceSignals | null = null;
  if (collect.device) {
    const data = read(() => (nav as Navigator & { userAgentData?: { mobile?: unknown } }).userAgentData, undefined);
    const mobile = data && typeof data === "object" ? read(() => data.mobile, undefined) : undefined;
    const touch = read(() => Number(nav.maxTouchPoints), 0);
    device = {
      userAgent: userAgent || "",
      mobile: typeof mobile === "boolean" ? mobile : null,
      maxTouchPoints: Number.isFinite(touch) ? touch : 0,
    };
  }
  return {
    device,
    userAgent,
    language: collect.language ? read(() => nav.language, undefined) : undefined,
    referrer: collect.referrer ? read(() => win.document.referrer, "") : "",
  };
}

/** The page-load details: device, browser, language and referrer, each only when on and readable. */
export function pageDetails(signals: PageSignals, collect: Collect, pageHostname: string): Record<string, string> {
  const details: Record<string, string> = {};
  if (collect.device && signals.device) details.web_device = deviceFrom(signals.device);
  if (collect.browser && signals.userAgent !== null) details.web_browser = browserFrom(signals.userAgent);
  if (collect.language) {
    const language = languageFrom(signals.language);
    if (language !== null) details.web_language = language;
  }
  if (collect.referrer) {
    const referrer = referrerFrom(signals.referrer, pageHostname);
    if (referrer !== null) details.web_referrer = referrer;
  }
  return details;
}

/**
 * Every detail for one call, in a fixed key order: the page-load ones, then
 * the visit's (new or returning, the campaign and the landing page). A visit
 * detail the visit could not keep (storage refused) is left out.
 */
export function detailsFor(page: Record<string, string>, visit: VisitDetails | null, collect: Collect): Record<string, string> {
  const details: Record<string, string> = {};
  for (const key of ["web_device", "web_browser", "web_language"]) if (key in page) details[key] = page[key];
  if (collect.visitor && visit && visit.visitor) details.web_visitor = visit.visitor;
  if ("web_referrer" in page) details.web_referrer = page.web_referrer;
  if (collect.utm && visit) {
    for (const [param, key] of UTM_PARAMS) {
      const value = visit.campaign[param];
      if (utmKept(value)) details[key] = value;
    }
  }
  if (collect.landing && visit && landingPathKept(visit.landing)) details.web_landing_path = visit.landing;
  return details;
}
