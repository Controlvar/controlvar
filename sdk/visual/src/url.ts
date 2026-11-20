/**
 * The server's URL rules, as the script needs them: which links and image
 * addresses a change may carry, and how a page rule's address splits into a
 * host and a path. Each function follows its server counterpart
 * (split_url_config.url_problem and Python's urlsplit) so the two give the
 * same answer on every vector; where they could differ, this side refuses.
 */

/** Characters Python's str.isspace() accepts, as a regular expression class. */
export const SPACE = "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";

/** A URL never holds these: `#`, a backslash, whitespace or a control character. */
const URL_FORBIDDEN = new RegExp(`[#\\\\${SPACE}\\x00-\\x1f\\x7f-\\x9f]`);
const SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*$/;
const DOT_SEGMENTS = [".", "..", "%2e", ".%2e", "%2e.", "%2e%2e"];
const PATH_ENCODED = /[^\x00-\x7f"<>^`{|}]/;
const MAX_URL_LENGTH = 2048;
const MAX_LINK_LENGTH = 2048;

/** How many code points *value* holds (the server counts characters this way). */
export function codePoints(value: string): number {
  let count = 0;
  for (const _ of value) count += 1;
  return count;
}

export interface SplitUrl {
  scheme: string;
  netloc: string;
  path: string;
}

/** Python's urlsplit, for input that holds no whitespace or control character. */
export function splitUrl(url: string): SplitUrl {
  let rest = url;
  let scheme = "";
  const colon = rest.indexOf(":");
  if (colon > 0 && SCHEME.test(rest.slice(0, colon))) {
    scheme = rest.slice(0, colon).toLowerCase();
    rest = rest.slice(colon + 1);
  }
  let netloc = "";
  if (rest.slice(0, 2) === "//") {
    let end = rest.length;
    for (const delimiter of ["/", "?", "#"]) {
      const at = rest.indexOf(delimiter, 2);
      if (at >= 0 && at < end) end = at;
    }
    netloc = rest.slice(2, end);
    rest = rest.slice(end);
  }
  const hash = rest.indexOf("#");
  if (hash >= 0) rest = rest.slice(0, hash);
  const query = rest.indexOf("?");
  if (query >= 0) rest = rest.slice(0, query);
  return { scheme, netloc, path: rest };
}

/** The host part of a netloc (after any user information, before any port). */
function hostAndPort(netloc: string): { host: string; port: string | null } {
  const at = netloc.lastIndexOf("@");
  const info = at >= 0 ? netloc.slice(at + 1) : netloc;
  const colon = info.indexOf(":");
  if (colon < 0) return { host: info, port: null };
  const port = info.slice(colon + 1);
  return { host: info.slice(0, colon), port: port === "" ? null : port };
}

/** Whether the server's url_problem refuses *url*. */
export function urlProblem(url: string): boolean {
  if (codePoints(url) > MAX_URL_LENGTH) return true;
  if (URL_FORBIDDEN.test(url)) return true;
  const parts = splitUrl(url);
  // Brackets (IPv6 hosts) are refused outright; the server accepts some.
  if (/[[\]]/.test(parts.netloc)) return true;
  const { host, port } = hostAndPort(parts.netloc);
  if (port !== null && !(/^[0-9]+$/.test(port) && Number(port) <= 65535)) return true;
  if ((parts.scheme !== "http" && parts.scheme !== "https") || host === "") return true;
  if (parts.netloc.indexOf("@") >= 0) return true;
  if (/[^\x00-\x7f]/.test(host) || host.indexOf("%") >= 0) return true;
  if (parts.path.split("/").some((segment) => DOT_SEGMENTS.indexOf(segment.toLowerCase()) >= 0)) return true;
  return PATH_ENCODED.test(parts.path);
}

/**
 * Whether *value* cannot be a link or an image address: it must be a full
 * http(s) URL that url_problem accepts, or a path on this site that starts
 * with a single `/`.
 */
export function linkProblem(value: string): boolean {
  if (codePoints(value) > MAX_LINK_LENGTH) return true;
  if (value.charAt(0) === "/") {
    const second = value.charAt(1);
    if (second === "/" || second === "\\") return true;
    return urlProblem(`https://site.invalid${value}`);
  }
  if (urlProblem(value)) return true;
  const scheme = splitUrl(value).scheme;
  return scheme !== "http" && scheme !== "https";
}

/** A host as page rules compare it: lower case, without a port. */
export function normalizeHost(host: string): string {
  const value = host.toLowerCase();
  if (value.charAt(0) === "[") {
    const end = value.indexOf("]");
    return end >= 0 ? value.slice(0, end + 1) : value;
  }
  const colon = value.indexOf(":");
  return colon >= 0 ? value.slice(0, colon) : value;
}

/** A path as page rules compare it: one trailing `/` dropped, except at the root; empty is `/`. */
export function normalizePath(path: string): string {
  if (!path) return "/";
  if (path.length > 1 && path.charAt(path.length - 1) === "/") return path.slice(0, -1);
  return path;
}
