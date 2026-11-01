/**
 * Single HTTP client for the dashboard.
 *
 * - `apiBase()` prefixes every request with `NEXT_PUBLIC_API_URL` (default `""`,
 *   i.e. same-origin so the nginx `/api/` proxy works in the container image).
 * - `apiFetch()` adds `Authorization: Bearer <token>` from
 *   `localStorage["experimently.token"]`, sends/receives JSON and throws
 *   `ApiError { status, detail, code? }` for non-2xx responses.
 * - A 401 clears the stored token and redirects to `/login?next=<path>`
 *   (skipped on `/login` itself and for requests sent without auth).
 *
 * `apiFetch` always calls the global `fetch` at call time so tests can keep
 * mocking `global.fetch`.
 */

export const TOKEN_STORAGE_KEY = 'experimently.token';
export const LOGIN_PATH = '/login';

export type Role = 'ADMIN' | 'DEVELOPER' | 'ANALYST' | 'VIEWER';

/** Shape returned by `GET /api/v1/auth/me` and inside the login response. */
export interface UserMe {
  id: string;
  email: string;
  username: string;
  full_name: string | null;
  role: Role;
  is_superuser: boolean;
  is_active: boolean;
  auth_provider: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: UserMe;
}

// ---------------------------------------------------------------------------
// Base URL
// ---------------------------------------------------------------------------

/** Where `next dev` finds a locally started backend when nothing is configured. */
export const DEV_API_URL = 'http://localhost:8000';

/**
 * API origin without a trailing slash. Empty string means same-origin (the
 * container image's nginx proxies `/api/` to the backend). `next dev` with no
 * `NEXT_PUBLIC_API_URL` falls back to `http://localhost:8000` so a fresh clone
 * works against `uvicorn --port 8000`; production builds never do.
 */
export function apiBase(): string {
  const raw = process.env.NEXT_PUBLIC_API_URL;
  if (raw === undefined || raw === null) {
    return process.env.NODE_ENV === 'development' ? DEV_API_URL : '';
  }
  return raw.replace(/\/+$/, '');
}

/**
 * WebSocket origin (`ws://host` / `wss://host`). Uses `NEXT_PUBLIC_WS_URL` when
 * set, otherwise derives it from `apiBase()` or, for same-origin, from
 * `window.location`.
 */
export function wsBase(): string {
  const explicit = process.env.NEXT_PUBLIC_WS_URL;
  if (explicit) return explicit.replace(/\/+$/, '');
  const base = apiBase();
  if (base) return base.replace(/^http/i, 'ws');
  if (typeof window !== 'undefined' && window.location) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}`;
  }
  return 'ws://localhost:8000';
}

// ---------------------------------------------------------------------------
// Token storage
// ---------------------------------------------------------------------------

export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    /* storage unavailable (private mode, quota) — session only */
  }
}

export function clearToken(): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export interface ApiErrorInit {
  status: number;
  detail?: unknown;
  message?: string;
  cause?: unknown;
  /** The response's `X-Request-ID`, when the API sent one. */
  requestId?: string;
  /** The response's `Retry-After` in whole seconds, when it sent one (a 429). */
  retryAfter?: number;
}

/**
 * Error thrown for every failed request. `detail` is the parsed `{"detail": …}`
 * body from FastAPI (string, object or validation list). When the backend
 * returns a typed error such as `{"detail": {"code": "workspace_role_required"}}`
 * the code is exposed as `code`. `status === 0` means the API was unreachable.
 * `requestId` is the response's `X-Request-ID` header (the API lists it in
 * CORS `expose_headers`, so a cross-origin dashboard can read it too); an
 * administrator finds the request in the API log by it.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly code?: string;
  readonly requestId?: string;
  readonly retryAfter?: number;

  constructor(init: ApiErrorInit) {
    super(init.message ?? messageForDetail(init.status, init.detail));
    this.name = 'ApiError';
    this.status = init.status;
    this.detail = init.detail;
    const code = detailCode(init.detail);
    if (code) this.code = code;
    if (init.requestId) this.requestId = init.requestId;
    if (init.retryAfter !== undefined) this.retryAfter = init.retryAfter;
    if (init.cause !== undefined) {
      (this as { cause?: unknown }).cause = init.cause;
    }
    // Keep `instanceof` working when the TS target is ES5.
    Object.setPrototypeOf(this, ApiError.prototype);
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isNetworkError(): boolean {
    return this.status === 0;
  }

  get isServerError(): boolean {
    return this.status >= 500;
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError;
}

function detailCode(detail: unknown): string | undefined {
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const code = (detail as Record<string, unknown>).code;
    if (typeof code === 'string') return code;
  }
  return undefined;
}

/** Human-readable message for an error body. */
export function messageForDetail(status: number, detail: unknown): string {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    // FastAPI/pydantic validation errors: [{loc, msg, type}, …]
    const parts = detail
      .map((item) => {
        if (item && typeof item === 'object') {
          const rec = item as Record<string, unknown>;
          const loc = Array.isArray(rec.loc)
            ? rec.loc.filter((p) => p !== 'body').join('.')
            : '';
          const msg = typeof rec.msg === 'string' ? rec.msg : '';
          return loc && msg ? `${loc}: ${msg}` : msg || loc;
        }
        return typeof item === 'string' ? item : '';
      })
      .filter(Boolean);
    if (parts.length) return parts.join('; ');
  }
  if (detail && typeof detail === 'object') {
    const rec = detail as Record<string, unknown>;
    if (typeof rec.message === 'string' && rec.message) return rec.message;
    if (typeof rec.msg === 'string' && rec.msg) return rec.msg;
    if (typeof rec.code === 'string' && rec.code) return rec.code;
  }
  return status ? `Request failed with status ${status}` : 'Request failed';
}

// ---------------------------------------------------------------------------
// Request helpers
// ---------------------------------------------------------------------------

export type QueryValue = string | number | boolean | null | undefined;

export interface ApiFetchOptions extends Omit<RequestInit, 'body' | 'headers'> {
  /** Query-string parameters; `undefined`/`null` values are omitted. */
  query?: Record<string, QueryValue>;
  /** JSON body (serialised with `JSON.stringify`, sets `Content-Type`). */
  json?: unknown;
  /** Raw body for non-JSON payloads. */
  body?: BodyInit | null;
  headers?: Record<string, string>;
  /** Attach the bearer token (default `true`). */
  auth?: boolean;
  /** Redirect to `/login?next=` on 401 (default `true`). */
  redirectOn401?: boolean;
}

export function buildQuery(query?: Record<string, QueryValue>): string {
  if (!query) return '';
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : '';
}

/** Absolute (or same-origin) URL for an API path plus optional query. */
export function apiUrl(path: string, query?: Record<string, QueryValue>): string {
  const normalised = path.startsWith('/') ? path : `/${path}`;
  return `${apiBase()}${normalised}${buildQuery(query)}`;
}

/**
 * Thin indirection over `window.location` so tests can observe the 401
 * redirect without replacing jsdom's location object.
 */
export const navigation = {
  assign(url: string): void {
    if (typeof window === 'undefined' || !window.location) return;
    window.location.assign(url);
  },
  currentPath(): string {
    if (typeof window === 'undefined' || !window.location) return '/';
    return `${window.location.pathname}${window.location.search}`;
  },
};

function isOnLoginPage(): boolean {
  if (typeof window === 'undefined' || !window.location) return false;
  return window.location.pathname.replace(/\/+$/, '') === LOGIN_PATH;
}

/** Only allow same-origin, absolute-path redirect targets (no `//evil.com`). */
export function safeNextPath(next: string | null | undefined, fallback = '/'): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) {
    return fallback;
  }
  return next;
}

/**
 * The sign-in page, carrying `next` when it is a safe path other than `/` and
 * the sign-in page itself: `/login` or `/login?next=<next>`.
 */
export function loginPathWithNext(next: string | null | undefined): string {
  const target = safeNextPath(next);
  return target === '/' || target === LOGIN_PATH || target.startsWith(`${LOGIN_PATH}?`)
    ? LOGIN_PATH
    : `${LOGIN_PATH}?next=${encodeURIComponent(target)}`;
}

/** Full-page navigation to `/login?next=<current path>`. */
export function redirectToLogin(next?: string): void {
  navigation.assign(loginPathWithNext(next ?? navigation.currentPath()));
}

type LooseResponse = Partial<Response> & { ok: boolean; status?: number; statusText?: string };

interface ErrorBody {
  detail: unknown;
  /** False for a body that is not JSON: a plain-text 500, a proxy's HTML page. */
  json: boolean;
}

async function readErrorDetail(response: LooseResponse): Promise<ErrorBody> {
  const unwrap = (parsed: unknown): unknown =>
    parsed && typeof parsed === 'object' && 'detail' in (parsed as object)
      ? (parsed as { detail: unknown }).detail
      : parsed;
  try {
    if (typeof response.text === 'function') {
      const text = await response.text();
      if (!text) return { detail: undefined, json: false };
      try {
        return { detail: unwrap(JSON.parse(text) as unknown), json: true };
      } catch {
        return { detail: text, json: false };
      }
    }
    if (typeof response.json === 'function') {
      return { detail: unwrap((await response.json()) as unknown), json: true };
    }
  } catch {
    /* unreadable body */
  }
  return { detail: undefined, json: false };
}

/** `X-Request-ID`, if present and shaped like an id (it is shown to the user). */
function readRequestId(response: LooseResponse): string | undefined {
  const headers = response.headers;
  if (!headers || typeof headers.get !== 'function') return undefined;
  const value = headers.get('x-request-id');
  return value && /^[A-Za-z0-9._:-]{1,128}$/.test(value) ? value : undefined;
}

/**
 * `Retry-After` in whole seconds. Only the delta-seconds form is read (what
 * the API's rate limiter sends); an HTTP date or anything else is ignored.
 */
function readRetryAfter(response: LooseResponse): number | undefined {
  const headers = response.headers;
  if (!headers || typeof headers.get !== 'function') return undefined;
  const value = headers.get('retry-after');
  return value && /^\d{1,6}$/.test(value.trim()) ? Number(value.trim()) : undefined;
}

/**
 * Copy for a 5xx whose body says nothing useful (the API's plain-text
 * "Internal Server Error", or a proxy's page). The server answered, so this is
 * deliberately not the "can't reach the API" message.
 */
export function serverErrorMessage(status: number, requestId?: string): string {
  const head = `Something went wrong on the server (HTTP ${status}).`;
  return requestId
    ? `${head} This is not a connection problem. Request ID: ${requestId}. ` +
        'Your administrator can find the details by searching the API log for this ID.'
    : `${head} Your administrator can find the details in the API log.`;
}

async function readSuccessBody<T>(response: LooseResponse, path: string): Promise<T> {
  const status = response.status ?? 200;
  if (status === 204 || status === 205) return undefined as T;

  const headers = response.headers;
  const contentType =
    headers && typeof headers.get === 'function' ? headers.get('content-type') : null;
  if (contentType && !/json/i.test(contentType)) {
    // A non-JSON 2xx usually means a proxy served the SPA shell instead of the API.
    const text = typeof response.text === 'function' ? await response.text().catch(() => '') : '';
    if (!text.trim()) return undefined as T;
    throw new ApiError({
      status,
      detail: `Expected a JSON response from ${path} but received ${contentType}`,
    });
  }

  if (typeof response.json !== 'function') return undefined as T;
  try {
    return (await response.json()) as T;
  } catch {
    return undefined as T;
  }
}

/**
 * Copy for a request that got no response the browser would show us. That is
 * a backend that is down, or one that answered without CORS headers for this
 * origin -- the browser reports both the same way.
 */
export function unreachableMessage(): string {
  const base = apiBase();
  const where =
    base || (typeof window !== 'undefined' && window.location ? window.location.origin : 'the API');
  const message =
    `Can't reach the API at ${where}. Check that the backend is running and that ` +
    "this dashboard's origin is listed in CORS_ORIGINS.";
  return process.env.NODE_ENV === 'development' ? `${message} See docs → Quick start.` : message;
}

// ---------------------------------------------------------------------------
// apiFetch
// ---------------------------------------------------------------------------

/**
 * Perform an authenticated JSON request against the API.
 *
 * @param path  API path, e.g. `/api/v1/experiments`
 * @param options `fetch` init plus `query`, `json`, `auth`, `redirectOn401`
 * @returns the parsed JSON body (`undefined` for 204 / empty bodies)
 * @throws {ApiError} for non-2xx responses and network failures
 */
export async function apiFetch<T = unknown>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const {
    query,
    json,
    auth = true,
    redirectOn401 = true,
    headers: extraHeaders,
    body: rawBody,
    ...rest
  } = options;

  const url = apiUrl(path, query);
  const headers: Record<string, string> = { Accept: 'application/json', ...(extraHeaders ?? {}) };

  let body: BodyInit | null | undefined = rawBody;
  if (json !== undefined) {
    body = JSON.stringify(json);
    if (!hasHeader(headers, 'content-type')) headers['Content-Type'] = 'application/json';
  } else if (typeof rawBody === 'string' && !hasHeader(headers, 'content-type')) {
    headers['Content-Type'] = 'application/json';
  }

  const token = auth ? getToken() : null;
  if (token && !hasHeader(headers, 'authorization')) {
    headers.Authorization = `Bearer ${token}`;
  }

  const init: RequestInit = { ...rest, headers };
  if (body !== undefined) init.body = body;

  let response: LooseResponse;
  try {
    response = (await fetch(url, init)) as LooseResponse;
  } catch (err) {
    throw new ApiError({ status: 0, detail: unreachableMessage(), cause: err });
  }

  if (response.ok) {
    return readSuccessBody<T>(response, path);
  }

  throw await failureFrom(response, auth, redirectOn401);
}

/** The `ApiError` for a non-2xx response; a 401 also clears the token. */
async function failureFrom(
  response: LooseResponse,
  auth: boolean,
  redirectOn401: boolean,
): Promise<ApiError> {
  const status = response.status ?? 0;
  const { detail, json: bodyIsJson } = await readErrorDetail(response);
  const requestId = readRequestId(response);

  if (status === 401 && auth) {
    clearToken();
    if (redirectOn401 && !isOnLoginPage()) {
      redirectToLogin();
    }
  }

  let message: string;
  if (status >= 500 && !bodyIsJson) {
    message = serverErrorMessage(status, requestId);
  } else {
    message =
      detail !== undefined
        ? messageForDetail(status, detail)
        : response.statusText || messageForDetail(status, undefined);
    // Some API messages already carry this request's id (the experiment-create
    // failure message does, #385); appending it again would show it twice (#401).
    if (status >= 500 && requestId && !message.toLowerCase().includes(requestId.toLowerCase())) {
      message = `${message} (Request ID: ${requestId})`;
    }
  }

  return new ApiError({ status, detail, message, requestId, retryAfter: readRetryAfter(response) });
}

/** A downloaded file: its text and the response headers. */
export interface ApiDownload {
  text: string;
  headers: Headers;
}

/**
 * Download a file from the API with the signed-in user's token. Unlike
 * `apiFetch` it does not parse the body, and it returns the headers.
 *
 * @throws {ApiError} for non-2xx responses and network failures
 */
export async function apiDownload(
  path: string,
  query?: Record<string, QueryValue>,
): Promise<ApiDownload> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  let text: string;
  try {
    response = await fetch(apiUrl(path, query), { headers });
    if (!response.ok) throw await failureFrom(response as LooseResponse, true, true);
    text = await response.text();
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError({ status: 0, detail: unreachableMessage(), cause: err });
  }
  return { text, headers: response.headers };
}

function hasHeader(headers: Record<string, string>, name: string): boolean {
  const lower = name.toLowerCase();
  return Object.keys(headers).some((key) => key.toLowerCase() === lower);
}
