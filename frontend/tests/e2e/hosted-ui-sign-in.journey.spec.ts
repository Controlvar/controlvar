import { spawn, type ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { join } from "node:path";
import { test, expect, type BrowserContext, type Page, type Route } from "@playwright/test";
import { TOKEN_STORAGE_KEY } from "./env";

/**
 * Journey: sign in through the Cognito Hosted UI (#708), in the built
 * dashboard, under the Content-Security-Policy the image ships.
 *
 * The spec starts its own two servers and needs nothing else running:
 *   - tests/e2e/static-server.mjs over `frontend/out` (the export
 *     `npm run build` writes, with NEXT_PUBLIC_API_URL=""), with
 *     `--nginx-headers`: every page carries frontend/nginx.conf's headers,
 *     the CSP among them, read from that file;
 *   - an API stub that plays the API's side: GET /api/v1/auth/config, the
 *     code exchange (POST /api/v1/auth/cognito/exchange, which asks the
 *     fake Hosted UI's token logic in-process, as the real API asks Cognito
 *     from the server), GET /api/v1/users/me, POST /api/v1/auth/logout, and
 *     the local sign-in for the case of an API with no /auth/config.
 * The Hosted UI itself is `context.route` on https://hosted-ui.cognito.test
 * (a name that never resolves): /oauth2/authorize answers with a redirect to
 * the callback, /logout with a redirect to its logout_uri, and each counts
 * its visits. Nothing reaches AWS.
 *
 * Ports: HOSTED_UI_E2E_PORT (dashboard, default 55549) and
 * HOSTED_UI_E2E_API_PORT (the stub, default 55550).
 */

const PORT = Number(process.env.HOSTED_UI_E2E_PORT ?? 55549);
const API_PORT = Number(process.env.HOSTED_UI_E2E_API_PORT ?? 55550);
const DASHBOARD = `http://127.0.0.1:${PORT}`;
const HOSTED_UI = "hosted-ui.cognito.test";
const CLIENT_ID = "e2eclient";
const FRONTEND = join(__dirname, "..", "..");

type Mode = "cognito" | "refuse" | "no-config";

const fake = {
  mode: "cognito" as Mode,
  codes: new Map<string, { challenge: string; redirectUri: string; used: boolean }>(),
  issued: new Set<string>(),
  revoked: new Set<string>(),
  authorizeHits: 0,
  logoutHits: [] as string[],
  tokenCallsFromBrowser: 0,
  tokenCallsFromApi: 0,
  signOuts: [] as Array<{ authorization: string | undefined; completed: boolean }>,
};

function resetFake(mode: Mode): void {
  fake.mode = mode;
  fake.codes.clear();
  fake.issued.clear();
  fake.revoked.clear();
  fake.authorizeHits = 0;
  fake.logoutHits = [];
  fake.tokenCallsFromBrowser = 0;
  fake.tokenCallsFromApi = 0;
  fake.signOuts = [];
}

const ACCOUNT = {
  id: "8c4c7b2e-0000-4000-8000-000000000001",
  email: "pat@example.com",
  username: "pat",
  full_name: "Pat Example",
  role: "ANALYST",
  is_superuser: false,
  is_active: true,
};

/** The fake Hosted UI's token endpoint, as the API reaches it from the server. */
function hostedUiToken(body: Record<string, unknown>): string | null {
  fake.tokenCallsFromApi += 1;
  const entry = fake.codes.get(String(body.code));
  if (!entry || entry.used || entry.redirectUri !== body.redirect_uri) return null;
  const challenge = createHash("sha256").update(String(body.code_verifier)).digest("base64url");
  if (challenge !== entry.challenge) return null;
  entry.used = true;
  const token = `e2e-access-${fake.issued.size + 1}`;
  fake.issued.add(token);
  return token;
}

function send(res: ServerResponse, status: number, body?: unknown): void {
  if (body === undefined) {
    res.writeHead(status);
    res.end();
    return;
  }
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

function bearer(req: IncomingMessage): string | null {
  const header = req.headers.authorization ?? "";
  return header.startsWith("Bearer ") ? header.slice(7) : null;
}

async function readJson(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    return {};
  }
}

/** How long the stub's sign-out takes: long enough that a page that leaves at once would cut it off. */
const SIGN_OUT_MS = 1500;

async function api(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const path = new URL(req.url ?? "/", "http://stub").pathname;
  const token = bearer(req);
  if (req.method === "GET" && path === "/api/v1/auth/config") {
    if (fake.mode === "no-config") return send(res, 404, { detail: "Not Found" });
    return send(res, 200, {
      provider: "cognito",
      hosted_ui_domain: HOSTED_UI,
      client_id: CLIENT_ID,
      scopes: ["openid", "email", "profile", "aws.cognito.signin.user.admin"],
    });
  }
  if (req.method === "POST" && path === "/api/v1/auth/cognito/exchange") {
    const body = await readJson(req);
    const issued = hostedUiToken(body);
    if (!issued) return send(res, 400, { detail: "Cognito did not accept the sign-in code. Start the sign-in again." });
    return send(res, 200, { access_token: issued, expires_in: 3600 });
  }
  if (req.method === "GET" && path === "/api/v1/users/me") {
    if (fake.mode === "refuse" || !token || !fake.issued.has(token) || fake.revoked.has(token)) {
      return send(res, 401, { detail: "Could not validate credentials" });
    }
    return send(res, 200, ACCOUNT);
  }
  if (req.method === "POST" && path === "/api/v1/auth/logout") {
    const record = { authorization: req.headers.authorization, completed: false };
    fake.signOuts.push(record);
    let gone = false;
    res.on("close", () => {
      if (!res.writableFinished) gone = true;
    });
    await new Promise((resolve) => setTimeout(resolve, SIGN_OUT_MS));
    if (gone) return;
    if (token) fake.revoked.add(token);
    record.completed = true;
    return send(res, 204);
  }
  if (req.method === "POST" && path === "/api/v1/auth/login" && fake.mode === "no-config") {
    return send(res, 200, {
      access_token: "e2e-local-1",
      token_type: "bearer",
      expires_in: 3600,
      user: { ...ACCOUNT, auth_provider: "local", role: "ADMIN", is_superuser: true },
    });
  }
  if (req.method === "GET" && path === "/api/v1/auth/me" && fake.mode === "no-config" && token === "e2e-local-1") {
    return send(res, 200, { ...ACCOUNT, auth_provider: "local", role: "ADMIN", is_superuser: true });
  }
  return send(res, 404, { detail: "Not Found" });
}

/** The fake Hosted UI, answering the browser's navigations. */
async function hostedUi(route: Route): Promise<void> {
  const url = new URL(route.request().url());
  if (url.pathname === "/oauth2/authorize") {
    fake.authorizeHits += 1;
    const ok =
      url.searchParams.get("response_type") === "code" &&
      url.searchParams.get("client_id") === CLIENT_ID &&
      url.searchParams.get("code_challenge_method") === "S256" &&
      url.searchParams.get("redirect_uri") === `${DASHBOARD}/auth/callback`;
    if (!ok) return route.fulfill({ status: 400, contentType: "text/plain", body: "bad authorize request" });
    const code = `code-${fake.authorizeHits}`;
    fake.codes.set(code, {
      challenge: url.searchParams.get("code_challenge") ?? "",
      redirectUri: url.searchParams.get("redirect_uri") ?? "",
      used: false,
    });
    const back = `${DASHBOARD}/auth/callback?code=${code}&state=${encodeURIComponent(url.searchParams.get("state") ?? "")}`;
    return route.fulfill({ status: 302, headers: { location: back } });
  }
  if (url.pathname === "/logout") {
    const target = url.searchParams.get("logout_uri") ?? "";
    fake.logoutHits.push(target);
    if (url.searchParams.get("client_id") !== CLIENT_ID || target !== `${DASHBOARD}/login`) {
      return route.fulfill({ status: 400, contentType: "text/plain", body: "bad logout request" });
    }
    return route.fulfill({ status: 302, headers: { location: target } });
  }
  if (url.pathname === "/oauth2/token") {
    fake.tokenCallsFromBrowser += 1;
    return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  }
  return route.fulfill({ status: 404, contentType: "text/plain", body: "not found" });
}

/** The policy the image sends: nginx.conf's, with the entrypoint's default connect-src. */
function shippedCsp(): string {
  const conf = readFileSync(join(FRONTEND, "nginx.conf"), "utf8");
  const entrypoint = readFileSync(join(FRONTEND, "docker-entrypoint.sh"), "utf8");
  const line = /add_header Content-Security-Policy "([^"]*)" always;/.exec(conf)?.[1] ?? "";
  const connectSrc = /: "\$\{CSP_CONNECT_SRC:=([^}"]*)\}"/.exec(entrypoint)?.[1] ?? "";
  return line.replace("${CSP_CONNECT_SRC}", connectSrc);
}

let stub: Server;
let dashboard: ChildProcess;
const ran: string[] = [];

async function waitFor(url: string): Promise<void> {
  for (let i = 0; i < 100; i += 1) {
    try {
      const response = await fetch(url);
      if (response.status < 500) return;
    } catch {
      /* not up yet */
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`${url} did not answer`);
}

test.describe("Journey: sign in through the Cognito Hosted UI (built dashboard, shipped CSP)", () => {
  test.describe.configure({ mode: "serial" });

  test.beforeAll(async () => {
    expect(existsSync(join(FRONTEND, "out", "auth", "callback.html")), "run `npm run build` first").toBe(true);
    stub = createServer((req, res) => void api(req, res));
    await new Promise<void>((resolve) => stub.listen(API_PORT, "127.0.0.1", resolve));
    dashboard = spawn(
      process.execPath,
      [join(FRONTEND, "tests", "e2e", "static-server.mjs"), "--port", String(PORT), "--api", `http://127.0.0.1:${API_PORT}`, "--nginx-headers"],
      { stdio: "inherit" },
    );
    await waitFor(`${DASHBOARD}/login`);
  });

  test.afterAll(async () => {
    dashboard?.kill("SIGTERM");
    await new Promise<void>((resolve) => (stub ? stub.close(() => resolve()) : resolve()));
  });

  /** A fresh browser context with the fake Hosted UI, and every CSP report the page logs. */
  async function open(context: BrowserContext): Promise<{ page: Page; violations: string[] }> {
    await context.route(`https://${HOSTED_UI}/**`, hostedUi);
    const page = await context.newPage();
    const violations: string[] = [];
    page.on("console", (message) => {
      if (/Content Security Policy|Refused to (connect|load|execute)/i.test(message.text())) {
        violations.push(message.text());
      }
    });
    return { page, violations };
  }

  async function signIn(page: Page): Promise<void> {
    await page.goto(`${DASHBOARD}/login`);
    await page.getByRole("button", { name: "Sign in" }).click();
  }

  test("a user signs in: the browser never calls the token endpoint, and nothing breaks the CSP", async ({ browser }) => {
    resetFake("cognito");
    const context = await browser.newContext();
    const { page, violations } = await open(context);
    const response = await page.goto(`${DASHBOARD}/login`);
    expect(response?.headers()["content-security-policy"]).toBe(shippedCsp());
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
    expect(fake.authorizeHits).toBe(0);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL(`${DASHBOARD}/experiments`, { timeout: 20_000 });
    await expect(page.getByTestId("user-menu-role")).toContainText(/analyst/i);
    expect(await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY)).toBe("e2e-access-1");
    expect(fake.authorizeHits).toBe(1);
    expect(fake.tokenCallsFromApi).toBe(1);
    expect(fake.tokenCallsFromBrowser).toBe(0);
    expect(violations).toEqual([]);
    await context.close();
    ran.push("sign-in");
  });

  test("/auth/callback is served from the export, with the shipped CSP and no referrer", async ({ request }) => {
    const response = await request.get(`${DASHBOARD}/auth/callback?code=x&state=y`);
    expect(response.status()).toBe(200);
    expect(response.headers()["content-security-policy"]).toBe(shippedCsp());
    expect(shippedCsp()).toContain("connect-src 'self' ws: wss:");
    const html = await response.text();
    expect(html).toMatch(/<meta name="referrer" content="no-referrer"/);
    expect(html).toMatch(/role="status"[^>]*>Signing you in…</);
    // No-referrer is the page's own <meta>: an nginx `location` for it would
    // lose the server block's headers, the CSP with them.
    const conf = readFileSync(join(FRONTEND, "nginx.conf"), "utf8");
    expect(conf).not.toMatch(/location[^{]*\/auth/);
    expect(conf).toMatch(/location \/ \{\s*try_files \$uri \$uri\.html/);
    ran.push("callback-served");
  });

  test("sign-out: the API gets the POST even though the page leaves at once, then Cognito's /logout, then /login", async ({ browser, request }) => {
    resetFake("cognito");
    const context = await browser.newContext();
    const { page } = await open(context);
    await signIn(page);
    await page.waitForURL(`${DASHBOARD}/experiments`, { timeout: 20_000 });
    await page.getByTestId("logout-button").click();
    await page.waitForURL(`${DASHBOARD}/login`, { timeout: 15_000 });
    await expect(page.getByRole("status")).toHaveText("You have signed out.");
    expect(await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY)).toBeNull();
    expect(fake.logoutHits).toEqual([`${DASHBOARD}/login`]);
    await expect.poll(() => fake.signOuts.length, { timeout: 5_000 }).toBe(1);
    await expect
      .poll(() => fake.signOuts[0].completed, { timeout: SIGN_OUT_MS * 3, message: "the API never finished the sign-out POST" })
      .toBe(true);
    expect(fake.signOuts[0].authorization).toBe("Bearer e2e-access-1");
    const after = await request.get(`http://127.0.0.1:${API_PORT}/api/v1/users/me`, {
      headers: { Authorization: "Bearer e2e-access-1" },
    });
    expect(after.status()).toBe(401);
    await context.close();
    ran.push("sign-out");
  });

  test("an account the API refuses: the click, one silent re-sign-in, then the copy and the button", async ({ browser }) => {
    resetFake("refuse");
    const context = await browser.newContext();
    const { page } = await open(context);
    await signIn(page);
    // getByTestId: Next's route announcer is a second role="alert" on every page.
    const alert = page.getByTestId("login-error");
    await expect(alert).toHaveText("Your account could not be signed in. Ask an administrator.", {
      timeout: 20_000,
    });
    await expect(alert).toHaveAttribute("role", "alert");
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(page.url()).toBe(`${DASHBOARD}/login`);
    expect(fake.authorizeHits).toBe(2);
    expect(fake.logoutHits).toEqual([`${DASHBOARD}/login`]);
    expect(await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY)).toBeNull();
    await context.close();
    ran.push("loop-guard");
  });

  test("an API with no /auth/config: the local form, and a local user signs in", async ({ browser }) => {
    resetFake("no-config");
    const context = await browser.newContext();
    const { page } = await open(context);
    await page.goto(`${DASHBOARD}/login`);
    await page.locator('input[name="email"]').fill("pat@example.com");
    await page.locator('input[name="password"]').fill("pw-1");
    await page.getByTestId("login-submit").click();
    await page.waitForURL(`${DASHBOARD}/experiments`, { timeout: 20_000 });
    expect(await page.evaluate((key) => window.localStorage.getItem(key), TOKEN_STORAGE_KEY)).toBe("e2e-local-1");
    expect(fake.authorizeHits).toBe(0);
    await context.close();
    ran.push("no-config");
  });

  test("accessibility: axe on /login and on a failed callback, a keyboard walk, no navigation without a click", async ({ browser }) => {
    resetFake("cognito");
    const { default: AxeBuilder } = await import("@axe-core/playwright");
    const context = await browser.newContext();
    const { page } = await open(context);
    await page.goto(`${DASHBOARD}/login`);
    const button = page.getByRole("button", { name: "Sign in" });
    await expect(button).toBeVisible();
    expect(await button.evaluate((el) => el.tagName)).toBe("BUTTON");
    const login = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(login.violations.map((v) => `${v.id}: ${v.nodes.length}`)).toEqual([]);
    await page.waitForTimeout(500);
    expect(fake.authorizeHits).toBe(0);

    // A return this tab did not start: /login says so, as an alert that has the focus.
    await page.goto(`${DASHBOARD}/auth/callback?code=x&state=y`);
    await page.waitForURL(`${DASHBOARD}/login`);
    const alert = page.getByTestId("login-error");
    await expect(alert).toHaveText("That sign-in link has expired or was already used. Start again.");
    await expect(alert).toHaveAttribute("role", "alert");
    await expect(alert).toBeFocused();
    const failed = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(failed.violations.map((v) => `${v.id}: ${v.nodes.length}`)).toEqual([]);

    // The keyboard reaches "Try again" and starts the sign-in with Enter.
    let focused = "";
    for (let i = 0; i < 10 && focused !== "Try again"; i += 1) {
      await page.keyboard.press("Tab");
      focused = await page.evaluate(() => (document.activeElement as HTMLElement | null)?.innerText ?? "");
    }
    expect(focused).toBe("Try again");
    await page.keyboard.press("Enter");
    await page.waitForURL(`${DASHBOARD}/experiments`, { timeout: 20_000 });
    expect(fake.authorizeHits).toBe(1);
    await context.close();
    ran.push("a11y");
  });

  test("every journey above ran", () => {
    expect(ran).toEqual(["sign-in", "callback-served", "sign-out", "loop-guard", "no-config", "a11y"]);
  });
});
