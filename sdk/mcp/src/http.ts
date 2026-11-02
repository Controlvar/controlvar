/**
 * The one HTTP client every tool goes through.
 *
 * - Paths come from fixed templates; each path parameter has passed its
 *   tool's input pattern and is then encoded with `encodeURIComponent`. The
 *   query string is built with `URLSearchParams`. Requests go to the parsed
 *   origin plus that path, never to the raw configured string.
 * - Headers: `X-API-Key`, `Accept`, `User-Agent`, a fresh `X-Request-ID`, and
 *   `Content-Type` on the POST. Never `Authorization`, never cookies.
 * - Redirects are never followed (`redirect: "manual"`): a 3xx is an error
 *   naming the status and the `Location` origin, so the key goes only to the
 *   configured address.
 * - 60 seconds per request, no retries: an evaluation is never counted twice.
 * - stderr gets one line per request: the tool, the method, the path
 *   template, the status, the milliseconds and the request id. No query
 *   string, no body, no key, no header.
 */

import { randomUUID } from "node:crypto";

/**
 * The first platform release whose four read operations accept an API key
 * carrying `mcp:read`; checked at that release's tag. Older instances answer
 * the read tools with 401 "Not authenticated", and E12 names this version.
 */
export const MIN_PLATFORM_VERSION = "0.40.0";

export const TIMEOUT_SECONDS = 60;
const MAX_DETAIL_LENGTH = 500;

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

export interface ApiOptions {
  origin: string;
  apiKey: string;
  version: string;
  fetch: FetchLike;
  log: (line: string) => void;
  timeoutMs?: number;
}

export interface ApiRequest {
  /** The tool making the request, for the stderr line. */
  tool: string;
  /** What the request does, in a few words: "listing feature flags". */
  operation: string;
  method: "GET" | "POST";
  /** The path template, for the stderr line: "/api/v1/experiments/{experiment_id}". */
  template: string;
  /** The path itself, already encoded. */
  path: string;
  query?: Record<string, string | number | undefined>;
  body?: unknown;
}

/** A failure whose message is ready for the model and the user. */
export class ApiError extends Error {}

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function truncate(text: string): string {
  return text.length > MAX_DETAIL_LENGTH ? `${text.slice(0, MAX_DETAIL_LENGTH)}...` : text;
}

/** The API's `detail`, as text: a string as is; a 422 list as `field: msg`. */
function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return truncate(detail);
    if (Array.isArray(detail)) {
      const parts = detail.slice(0, 20).map((item) => {
        if (item && typeof item === "object") {
          const entry = item as { loc?: unknown; msg?: unknown };
          const loc = Array.isArray(entry.loc)
            ? entry.loc.filter((part) => !["body", "query", "path"].includes(String(part))).join(".")
            : "";
          const msg = typeof entry.msg === "string" ? entry.msg : JSON.stringify(item);
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(item);
      });
      return truncate(parts.join("; "));
    }
    return truncate(JSON.stringify(detail));
  }
  return fallback;
}

/** Why a request did not reach the API, from the error fetch threw. */
function networkReason(error: unknown, requestId: string): string {
  // Read by property, not `instanceof`: the timeout is a DOMException, which
  // is not an Error of every realm.
  const name = error && typeof error === "object" ? (error as { name?: unknown }).name : undefined;
  if (name === "TimeoutError" || name === "AbortError") {
    return `did not answer within ${TIMEOUT_SECONDS} seconds (request ${requestId})`;
  }
  let cause: unknown = error && typeof error === "object" ? (error as { cause?: unknown }).cause : undefined;
  const codes: string[] = [];
  for (let depth = 0; cause && depth < 5; depth += 1) {
    const code = (cause as { code?: unknown }).code;
    if (typeof code === "string") codes.push(code);
    cause = (cause as { cause?: unknown }).cause;
  }
  const code = codes[0] ?? "";
  if (code === "ECONNREFUSED") return "connection refused";
  if (code === "ENOTFOUND" || code === "EAI_AGAIN") return "the host name was not found";
  if (
    code.startsWith("CERT_") ||
    code.startsWith("UNABLE_TO_") ||
    code.startsWith("ERR_TLS_") ||
    code.includes("SELF_SIGNED") ||
    code === "HOSTNAME_MISMATCH"
  ) {
    return `its TLS certificate was not accepted (${code})`;
  }
  return code ? `the connection failed (${code})` : "the connection failed";
}

export class Api {
  private readonly options: ApiOptions;

  constructor(options: ApiOptions) {
    this.options = options;
  }

  async request(request: ApiRequest): Promise<unknown> {
    const { origin, apiKey, version } = this.options;
    const params = new URLSearchParams();
    for (const [name, value] of Object.entries(request.query ?? {})) {
      if (value !== undefined) params.append(name, String(value));
    }
    const search = params.toString();
    const url = `${origin}${request.path}${search ? `?${search}` : ""}`;
    const requestId = randomUUID();
    const headers: Record<string, string> = {
      "X-API-Key": apiKey,
      Accept: "application/json",
      "User-Agent": `controlvar-mcp/${version}`,
      "X-Request-ID": requestId,
    };
    let body: string | undefined;
    if (request.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(request.body);
    }

    const started = Date.now();
    const log = (outcome: string) =>
      this.options.log(
        `controlvar-mcp: ${request.tool} ${request.method} ${request.template} ${outcome} ` +
          `${Date.now() - started}ms request ${requestId}`,
      );

    let response: Response;
    let text: string;
    try {
      response = await this.options.fetch(url, {
        method: request.method,
        headers,
        body,
        redirect: "manual",
        signal: AbortSignal.timeout(this.options.timeoutMs ?? TIMEOUT_SECONDS * 1000),
      });
      text = await response.text();
    } catch (error) {
      const reason = networkReason(error, requestId);
      log("failed");
      throw new ApiError(
        `Could not reach the Controlvar API at ${origin}: ${reason}. ` +
          "Check that the instance is running and that CONTROLVAR_API_URL is its address.",
      );
    }
    log(String(response.status));
    return this.interpret(request, response, text, requestId);
  }

  private interpret(request: ApiRequest, response: Response, text: string, requestId: string): unknown {
    const { origin } = this.options;
    const status = response.status;
    let parsed: unknown;
    let isJson = false;
    try {
      parsed = JSON.parse(text);
      isJson = true;
    } catch {
      parsed = undefined;
    }

    if (status >= 300 && status < 400) {
      let target = "";
      const location = response.headers.get("location");
      if (location) {
        try {
          const resolved = new URL(location, `${origin}${request.path}`);
          if (resolved.protocol === "http:" || resolved.protocol === "https:") target = resolved.origin;
        } catch {
          target = "";
        }
      }
      const where = target ? `a redirect to ${target}` : `a redirect (${status})`;
      const advice = target ? ` If ${target} is your instance, set CONTROLVAR_API_URL to it.` : "";
      throw new ApiError(
        `${origin} answered with ${where}. The MCP server does not follow redirects, so your key ` +
          `goes only to the address you set.${advice}`,
      );
    }

    if (status >= 200 && status < 300) {
      if (!isJson) {
        const contentType = response.headers.get("content-type") ?? "";
        const what =
          contentType.includes("html") || text.trimStart().startsWith("<")
            ? "it sent a web page"
            : "it did not send JSON";
        throw new ApiError(
          `${origin} answered, but not as the Controlvar API (${what}). Set CONTROLVAR_API_URL to ` +
            "the address that serves /api/v1: for the Quick Start, http://localhost:8000.",
        );
      }
      return parsed;
    }

    const detail = detailOf(parsed, truncate(response.statusText || "no detail"));
    if (status === 400 && detail === "Invalid host header") {
      throw new ApiError(
        `The Controlvar API at ${origin} refused that host name (400: Invalid host header). Use the ` +
          "host name your instance is set up for (its PUBLIC_BASE_URL), for example http://localhost:8080.",
      );
    }
    if (status === 401) {
      throw new ApiError(
        `The Controlvar API at ${origin} did not accept the key in CONTROLVAR_API_KEY (401: ${detail}). ` +
          "It may have been deleted or have expired, or its owner's account may be deactivated; or the " +
          `instance is older than Controlvar ${MIN_PLATFORM_VERSION} and does not accept API keys for ` +
          "this. Create a new key, or upgrade the instance.",
      );
    }
    if (status === 429) {
      const retry = response.headers.get("retry-after");
      const when = retry && /^\d{1,6}$/.test(retry) ? `in ${retry} seconds` : "later";
      throw new ApiError(
        `The Controlvar API is limiting requests from this address (429). Try again ${when}.`,
      );
    }
    if (status >= 500) {
      const id = response.headers.get("x-request-id");
      const shownId = id && /^[\w.-]{1,100}$/.test(id) ? id : requestId;
      throw new ApiError(
        `The Controlvar API at ${origin} failed while ${request.operation} (${status}). ` +
          `Request ID: ${shownId}; your administrator can find it in the API's logs.`,
      );
    }
    throw new ApiError(`${capitalise(request.operation)} failed: ${detail} (${status}).`);
  }
}
