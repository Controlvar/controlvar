import { spawnSync } from "node:child_process";
import { join } from "node:path";
import type { Locator, Page } from "@playwright/test";
import { test, expect } from "./persona.fixture";
import { decide, OracleError, type OracleKind } from "../oracles/oracle";
import {
  MATRIX_ROLES,
  makeAdminAccount,
  openAccount,
  seededAccount,
  type AccountSession,
  type MatrixRole,
} from "./accounts";

/**
 * U-QA's day (tests/personas/personas/u-qa.yaml): the role matrix of
 * tests/personas/role-matrix.yaml, checked in the dashboard and through the
 * API for all five accounts. Step 1 makes the Admin-role account that is not
 * a superuser; each later step is one row of the matrix, in the file's order.
 *
 * Per row and role, the page is opened and its data must render first (or
 * the refusal view must show); only then is the control's presence or absence
 * read, so a page that failed to load is never taken for "not offered". Then
 * the row's call is made as that role, with a valid body and a real id, and
 * the state is read back as the superuser: a 2xx cell's change is there, a
 * 403 cell's is not. A 404 or 422 is "not reached" and matches no cell. Each
 * row's three oracles (ui, allowed, refused) are all decided before the step
 * fails, so a red row names every cell that differs.
 *
 * The matrix is read through tests/personas/catalogue.py (the Python on PATH,
 * or PERSONAS_PYTHON), so the spec checks the same validated rows the unit
 * tests do, and fails if a row has no step here.
 */

type Ui = "offered" | "not_offered" | "refusal_page";
type ApiCell = "2xx" | "403";

interface Row {
  id: string;
  method: "GET" | "POST" | "PUT" | "DELETE";
  path: string;
  query: string | null;
  profile: "core" | "full";
  cells: Record<MatrixRole, { ui: Ui; api: ApiCell }>;
}

/** What a role's page showed: a ui cell, or why it is none. */
type Seen = Ui | string;

/** A role's call: its status, and what reading the state back found. */
interface Answer {
  status: number;
  /** A write: was the change there afterwards? A read: did it hold the day's data? */
  found: boolean;
}

/** One row's check: the page each role opens, and the call each role makes. */
interface RowCheck {
  /** The page, as a red line names it. */
  page: string;
  /** What the row's calls need, made before any role's page is read. */
  setup?: () => Promise<void>;
  see: (session: AccountSession) => Promise<Seen>;
  call: (role: MatrixRole, session: AccountSession) => Promise<Answer>;
}

/** The refusal view's sentence on every page the matrix says needs a superuser. */
const SUPERUSER_REFUSAL = "This page requires a superuser account.";
const NOT_RENDERED = "neither the data nor the refusal rendered, so no absence is evidence";
const REPO_ROOT = join(__dirname, "..", "..", "..", "..");

function loadMatrix(): Row[] {
  const script = [
    "import json, sys",
    "from pathlib import Path",
    "sys.path.insert(0, 'tests/personas')",
    "import catalogue",
    "matrix = catalogue.load_matrix(Path('tests/personas/role-matrix.yaml'), Path('.'))",
    "print(json.dumps([row.model_dump() for row in matrix.rows]))",
  ].join("\n");
  const run = spawnSync(process.env.PERSONAS_PYTHON ?? "python3", ["-c", script], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  if (run.status !== 0) throw new Error(`the role matrix did not load: ${run.stderr}`);
  return JSON.parse(run.stdout) as Row[];
}

/** 2xx, 403, or why the call never reached the permission check. */
function answerClass(status: number): string {
  if (status >= 200 && status < 300) return "2xx";
  if (status === 403) return "403";
  if (status === 404 || status === 422) return `not reached (${status})`;
  return String(status);
}

/**
 * Open `path` and wait until either `data` or the refusal view is visible.
 * Returns "data", "refusal_page", or why neither is evidence of a cell.
 */
async function open(page: Page, path: string, data: Locator): Promise<"data" | Seen> {
  await page.goto(path);
  const refusal = page.getByTestId("require-auth-forbidden");
  try {
    await expect(refusal.or(data).first()).toBeVisible({ timeout: 20_000 });
  } catch {
    return NOT_RENDERED;
  }
  if (await refusal.isVisible()) {
    const text = await refusal.innerText();
    return text.includes(SUPERUSER_REFUSAL) ? "refusal_page" : "a refusal that does not ask for a superuser account";
  }
  return "data";
}

/** With the data rendered: is the control there, once the page has settled? */
async function offered(page: Page, control: Locator): Promise<Ui> {
  await page.waitForLoadState("networkidle");
  return (await control.count()) > 0 && (await control.first().isVisible()) ? "offered" : "not_offered";
}

/** Wait for `locator`; false (never a throw) when it does not show. */
async function shows(locator: Locator, timeout = 15_000): Promise<boolean> {
  try {
    await expect(locator).toBeVisible({ timeout });
    return true;
  } catch {
    return false;
  }
}

/**
 * Decide the row's three oracles, all of them, then fail with every red
 * line. The ui oracle names each role whose page differs from its ui cell;
 * the allowed and refused oracles each name the role and call that differ.
 */
function decideRow(
  n: number,
  row: Row,
  page: string,
  seen: Record<MatrixRole, Seen>,
  answers: Record<MatrixRole, Answer>,
): void {
  const write = row.method !== "GET";
  const call = `${row.method} ${row.path}${row.query ? `?${row.query}` : ""}`;
  const shown = (role: MatrixRole): string => {
    const { status, found } = answers[role];
    const answer = answerClass(status);
    if (write) return `${answer}, ${found ? "changed" : "unchanged"}`;
    return answer === "2xx" ? `2xx, ${found ? "holds the day's data" : "without the day's data"}` : answer;
  };
  const expected = (cell: ApiCell): string => {
    if (cell === "2xx") return write ? "2xx, changed" : "2xx, holds the day's data";
    return write ? "403, unchanged" : "403";
  };
  const failures: string[] = [];
  const oracle = (
    id: string,
    kind: OracleKind,
    roles: MatrixRole[],
    want: (r: MatrixRole) => string,
    got: (r: MatrixRole) => string,
    line: (r: MatrixRole) => string,
  ) => {
    if (roles.length === 0) return;
    const differ = roles.filter((r) => want(r) !== got(r)).map(line);
    try {
      decide(
        id,
        kind,
        Object.fromEntries(roles.map((r) => [r, want(r)])),
        Object.fromEntries(roles.map((r) => [r, got(r)])),
        differ.join("; "),
      );
    } catch (error) {
      if (!(error instanceof OracleError)) throw error;
      failures.push(error.message);
    }
  };
  const allowed = MATRIX_ROLES.filter((r) => row.cells[r].api === "2xx");
  const refused = MATRIX_ROLES.filter((r) => row.cells[r].api === "403");
  oracle(
    `u-qa.${n}.${row.id}.ui`,
    "ui_matches_api",
    [...MATRIX_ROLES],
    (r) => row.cells[r].ui,
    (r) => seen[r],
    (r) => `${r} on ${page}: ${seen[r]}, ui cell ${row.cells[r].ui} (api cell ${row.cells[r].api})`,
  );
  oracle(`u-qa.${n}.${row.id}.allowed`, "api_state", allowed, () => expected("2xx"), shown, (r) => {
    return `${r} ${call}: ${shown(r)}, api cell 2xx`;
  });
  oracle(`u-qa.${n}.${row.id}.refused`, "api_refused", refused, () => expected("403"), shown, (r) => {
    return `${r} ${call}: ${shown(r)}, api cell 403`;
  });
  if (failures.length > 0) throw new Error(failures.join("\n"));
}

/** Every session the day opened, closed after it whatever happened. */
const opened: AccountSession[] = [];
test.afterEach(async () => {
  for (const session of opened.splice(0)) await session.close();
});

test("U-QA: the role matrix, in the dashboard and through the API, for all five accounts", async ({ browser }) => {
  // Five sign-ins and ten rows of five pages each: 15 to 20 seconds on a local stack.
  test.setTimeout(3 * 60_000);
  const rows = loadMatrix();
  const stamp = Date.now();
  const sessions = {} as Record<MatrixRole, AccountSession>;
  for (const role of ["OWNER", "DEVELOPER", "ANALYST", "VIEWER"] as const) {
    sessions[role] = await openAccount(browser, seededAccount(role));
    opened.push(sessions[role]);
  }
  const owner = (): AccountSession => sessions.OWNER;
  const checked: string[] = [];

  /** Every role's page, then every role's call, then the row's oracles and the watchers. */
  const checkRow = async (n: number, id: string, check: RowCheck): Promise<void> => {
    const row = rows.find((r) => r.id === id);
    if (!row) throw new Error(`role-matrix.yaml has no row ${id}`);
    checked.push(id);
    if (check.setup) await check.setup();
    const seen = {} as Record<MatrixRole, Seen>;
    for (const role of MATRIX_ROLES) seen[role] = await check.see(sessions[role]);
    const answers = {} as Record<MatrixRole, Answer>;
    for (const role of MATRIX_ROLES) answers[role] = await check.call(role, sessions[role]);
    decideRow(n, row, check.page, seen, answers);
    for (const role of MATRIX_ROLES) await sessions[role].watch.step();
  };

  // ------------------------------------------------------------ the day's data
  interface Listed<T> {
    items: T[];
  }
  interface Experiment {
    id: string;
    key: string;
    name: string;
  }
  interface Flag {
    id: string;
    key: string;
    description: string | null;
  }
  interface Key {
    id: string;
    name: string;
  }
  const experimentBody = (name: string, key: string) => ({
    name,
    key,
    experiment_type: "a_b",
    variants: [
      { name: "Control", is_control: true, traffic_allocation: 50 },
      { name: "Treatment", is_control: false, traffic_allocation: 50 },
    ],
    metrics: [{ name: "Purchase", event_name: "purchase", metric_type: "conversion", is_primary: true }],
  });
  const experimentByKey = async (name: string, key: string): Promise<boolean> =>
    (
      await owner().api.json<Listed<Experiment>>(
        `/api/v1/experiments/?search=${encodeURIComponent(name)}&skip_cache=true&limit=100`,
      )
    ).items.some((e) => e.key === key);
  const flagByKey = async (key: string): Promise<boolean> =>
    (await owner().api.json<Listed<Flag>>(`/api/v1/feature-flags/?search=${encodeURIComponent(key)}&limit=100`)).items.some(
      (f) => f.key === key,
    );
  const makeFlag = async (key: string, description: string): Promise<Flag> => {
    const made = await owner().api.send("POST", "/api/v1/feature-flags/", { key, name: key, description });
    expect(made.status(), `the day's flag ${key}`).toBe(201);
    return (await made.json()) as Flag;
  };
  const makeKey = async (session: AccountSession, name: string): Promise<Key> => {
    const made = await session.api.send("POST", "/api/v1/api-keys", { name });
    expect(made.status(), `the day's key ${name}`).toBe(201);
    return (await made.json()) as Key;
  };
  const ownKeys = async (session: AccountSession): Promise<Key[]> =>
    session.api.json<Key[]>("/api/v1/api-keys?include_inactive=true");
  const lower = (role: MatrixRole) => role.toLowerCase();

  // -------------------------------------------------------------- the day

  let adminId = "";
  await test.step("U-QA day 1: make the Admin-role account that is not a superuser", async () => {
    const made = await makeAdminAccount(owner().api, stamp);
    adminId = made.id;
    sessions.ADMIN = await openAccount(browser, made.account);
    opened.push(sessions.ADMIN);
    const me = await sessions.ADMIN.api.json<{ role: string; is_superuser: boolean }>("/api/v1/auth/me");
    const listed = await owner().api.json<Listed<{ id: string; role: string; is_superuser: boolean }>>(
      `/api/v1/admin/users?search=${encodeURIComponent(made.account.email)}`,
    );
    const row = listed.items.find((u) => u.id === made.id);
    decide(
      "u-qa.1.admin-account",
      "api_state",
      { status: 201, me: { role: "ADMIN", superuser: false }, listed: { role: "ADMIN", superuser: false } },
      {
        status: made.status,
        me: { role: me.role, superuser: me.is_superuser },
        listed: row ? { role: row.role, superuser: row.is_superuser } : null,
      },
    );
    for (const role of MATRIX_ROLES) await sessions[role].watch.step();
  });

  const experimentName = (role: MatrixRole) => `Persona QA ${stamp} ${role}`;
  await test.step("U-QA day 2: check the role matrix row experiment-create", async () => {
    await checkRow(2, "experiment-create", {
      page: "/experiments",
      see: async ({ page }) => {
        const state = await open(page, "/experiments", page.getByTestId("experiment-row").first());
        return state === "data" ? offered(page, page.getByTestId("new-experiment-btn")) : state;
      },
      call: async (role, { api }) => {
        const key = `persona_qa_${stamp}_${lower(role)}`;
        const answer = await api.send("POST", "/api/v1/experiments/", experimentBody(experimentName(role), key));
        return { status: answer.status(), found: await experimentByKey(experimentName(role), key) };
      },
    });
  });

  const listedName = `Persona QA ${stamp} listed`;
  await test.step("U-QA day 3: check the role matrix row experiment-list", async () => {
    await checkRow(3, "experiment-list", {
      page: "/experiments",
      setup: async () => {
        const body = experimentBody(listedName, `persona_qa_${stamp}_listed`);
        const made = await owner().api.send("POST", "/api/v1/experiments/", body);
        expect(made.status(), "the day's experiment").toBe(201);
      },
      see: async ({ page }) => {
        const state = await open(page, "/experiments", page.getByTestId("experiment-row").filter({ hasText: listedName }));
        return state === "data" ? "offered" : state;
      },
      call: async (role, { api }) => {
        const search = encodeURIComponent(listedName);
        const answer = await api.send("GET", `/api/v1/experiments/?search=${search}&skip_cache=true`);
        const found = answer.ok() && ((await answer.json()) as Listed<Experiment>).items.some((e) => e.name === listedName);
        return { status: answer.status(), found };
      },
    });
  });

  await test.step("U-QA day 4: check the role matrix row flag-create", async () => {
    await checkRow(4, "flag-create", {
      page: "/feature-flags",
      see: async ({ page }) => {
        const state = await open(page, "/feature-flags", page.getByTestId("flag-row").first());
        return state === "data" ? offered(page, page.getByTestId("new-flag-btn")) : state;
      },
      call: async (role, { api }) => {
        const key = `persona-qa-${stamp}-${lower(role)}`;
        const answer = await api.send("POST", "/api/v1/feature-flags/", { key, name: key });
        return { status: answer.status(), found: await flagByKey(key) };
      },
    });
  });

  const listedFlag = `persona-qa-${stamp}-listed`;
  await test.step("U-QA day 5: check the role matrix row flag-list", async () => {
    await checkRow(5, "flag-list", {
      page: "/feature-flags",
      setup: async () => {
        await makeFlag(listedFlag, "listed for every role");
      },
      see: async ({ page }) => {
        // The page lists the first 100 flags, so on a stack that has run many
        // days the day's flag may be past them: any row is the list's data.
        const state = await open(page, "/feature-flags", page.getByTestId("flag-row").first());
        return state === "data" ? "offered" : state;
      },
      call: async (role, { api }) => {
        const answer = await api.send("GET", `/api/v1/feature-flags/?search=${encodeURIComponent(listedFlag)}`);
        const found = answer.ok() && ((await answer.json()) as Listed<Flag>).items.some((f) => f.key === listedFlag);
        return { status: answer.status(), found };
      },
    });
  });

  let changed: Flag | null = null;
  await test.step("U-QA day 6: check the role matrix row flag-change", async () => {
    await checkRow(6, "flag-change", {
      page: "/feature-flags/{id}",
      setup: async () => {
        changed = await makeFlag(`persona-qa-${stamp}-changed`, "set by the day");
      },
      see: async ({ page }) => {
        const flag = changed as Flag;
        const key = page.getByTestId("flag-key").filter({ hasText: flag.key });
        const state = await open(page, `/feature-flags/${flag.id}`, key);
        if (state !== "data") return state;
        // The page decides: the switch and Save for a role that may change
        // the flag, its role note otherwise. The two must agree.
        const save = page.getByTestId("save-flag");
        if (!(await shows(save.or(page.getByTestId("flag-role-note")).first()))) return NOT_RENDERED;
        const saving = await offered(page, save);
        const switching = await offered(page, page.getByTestId("flag-toggle"));
        return saving === switching ? saving : `the switch is ${switching} but Save is ${saving}`;
      },
      call: async (role, { api }) => {
        const flag = changed as Flag;
        const description = `changed by ${role} ${stamp}`;
        const answer = await api.send("PUT", `/api/v1/feature-flags/${flag.id}`, { description });
        const stored = await owner().api.json<Flag>(`/api/v1/feature-flags/${flag.id}?skip_cache=true`);
        return { status: answer.status(), found: stored.description === description };
      },
    });
  });

  await test.step("U-QA day 7: check the role matrix row users-list", async () => {
    await checkRow(7, "users-list", {
      page: "/admin/users",
      see: async ({ page }) => {
        const state = await open(page, "/admin/users", page.getByTestId(`superuser-${adminId}`));
        return state === "data" ? "offered" : state;
      },
      call: async (_role, { api }) => {
        const email = sessions.ADMIN.account.email;
        const answer = await api.send("GET", `/api/v1/admin/users?search=${encodeURIComponent(email)}`);
        const found =
          answer.ok() &&
          ((await answer.json()) as Listed<{ id: string; role: string }>).items.some(
            (u) => u.id === adminId && u.role === "ADMIN",
          );
        return { status: answer.status(), found };
      },
    });
  });

  interface Settings {
    enable_automatic_rollbacks: boolean;
    default_metrics: Record<string, unknown> | null;
  }
  const settingsNow = async (): Promise<Settings> => {
    const s = await owner().api.json<Settings>("/api/v1/safety/settings");
    return { enable_automatic_rollbacks: s.enable_automatic_rollbacks, default_metrics: s.default_metrics };
  };
  await test.step("U-QA day 8: check the role matrix row safety-settings-change", async () => {
    let before: Settings | null = null;
    try {
      await checkRow(8, "safety-settings-change", {
        page: "/admin/safety",
        setup: async () => {
          before = await settingsNow();
        },
        see: async ({ page }) => {
          const state = await open(page, "/admin/safety", page.getByTestId("safety-settings-form"));
          return state === "data" ? offered(page, page.getByTestId("save-settings-button")) : state;
        },
        call: async (role, { api }) => {
          // A threshold no flag is measured by, named for the role: an
          // unchanged read-back is unambiguous.
          const metric = `persona_qa_${stamp}_${lower(role)}`;
          const base = before as Settings;
          const answer = await api.send("POST", "/api/v1/safety/settings", {
            enable_automatic_rollbacks: base.enable_automatic_rollbacks,
            default_metrics: {
              ...(base.default_metrics ?? {}),
              [metric]: { warning_threshold: null, critical_threshold: null, comparison_type: "greater_than" },
            },
          });
          const found = Object.prototype.hasOwnProperty.call((await settingsNow()).default_metrics ?? {}, metric);
          // Put the settings back before the next role's call.
          if (found) {
            const restored = await owner().api.send("POST", "/api/v1/safety/settings", base);
            expect(restored.status(), "the settings put back").toBe(200);
          }
          return { status: answer.status(), found };
        },
      });
    } finally {
      if (before) {
        const now = await settingsNow();
        expect(now, "the global safety settings are as the day found them").toEqual(before);
      }
    }
  });

  let listedKey: Key | null = null;
  const showAllKeys = async (page: Page): Promise<void> => {
    const all = page.getByTestId("show-all-keys");
    if (!(await all.isChecked())) await all.check();
  };
  await test.step("U-QA day 9: check the role matrix row api-keys-list-all", async () => {
    await checkRow(9, "api-keys-list-all", {
      page: "/admin/api-keys",
      setup: async () => {
        listedKey = await makeKey(sessions.VIEWER, `persona-qa-${stamp}-listed`);
      },
      see: async ({ page }) => {
        const state = await open(page, "/admin/api-keys", page.getByTestId("show-all-keys"));
        if (state !== "data") return state;
        await showAllKeys(page);
        return (await shows(page.getByTestId(`api-key-row-${(listedKey as Key).id}`))) ? "offered" : NOT_RENDERED;
      },
      call: async (role, { api }) => {
        const answer = await api.send("GET", "/api/v1/api-keys?all=true");
        const found = answer.ok() && ((await answer.json()) as Key[]).some((k) => k.id === (listedKey as Key).id);
        return { status: answer.status(), found };
      },
    });
  });

  // Each role deletes a key it does not own: the viewer's, or for the viewer the developer's.
  const holder = (role: MatrixRole): MatrixRole => (role === "VIEWER" ? "DEVELOPER" : "VIEWER");
  const targets = {} as Record<MatrixRole, Key>;
  await test.step("U-QA day 10: check the role matrix row api-key-delete-others", async () => {
    await checkRow(10, "api-key-delete-others", {
      page: "/admin/api-keys",
      setup: async () => {
        for (const role of MATRIX_ROLES) {
          targets[role] = await makeKey(sessions[holder(role)], `persona-qa-${stamp}-for-${lower(role)}`);
        }
      },
      see: async ({ page }) => {
        const state = await open(page, "/admin/api-keys", page.getByTestId("show-all-keys"));
        if (state !== "data") return state;
        await showAllKeys(page);
        const target = targets.OWNER.id;
        if (!(await shows(page.getByTestId(`api-key-row-${target}`)))) return NOT_RENDERED;
        return offered(page, page.getByTestId(`delete-button-${target}`));
      },
      call: async (role, { api }) => {
        const answer = await api.send("DELETE", `/api/v1/api-keys/${targets[role].id}`);
        const kept = (await ownKeys(sessions[holder(role)])).some((k) => k.id === targets[role].id);
        return { status: answer.status(), found: !kept };
      },
    });
  });

  const roleName = (role: MatrixRole) => `persona-qa-${stamp}-${lower(role)}`;
  await test.step("U-QA day 11: check the role matrix row custom-role-create", async () => {
    await checkRow(11, "custom-role-create", {
      page: "/admin/roles",
      see: async ({ page }) => {
        const state = await open(page, "/admin/roles", page.getByTestId("role-table"));
        if (state !== "data") return state;
        // The table has answered: rows, or its empty state (an error is neither).
        const answered = page.getByTestId("role-table").locator("tbody tr").or(page.getByTestId("role-table-empty"));
        if (!(await shows(answered.first()))) return NOT_RENDERED;
        return offered(page, page.getByTestId("create-role-button"));
      },
      call: async (role, { api }) => {
        const name = roleName(role);
        const answer = await api.send("POST", "/api/v1/rbac/roles", {
          name,
          description: "made by the role matrix",
          permissions: [],
        });
        const listed = await owner().api.json<Array<{ name: string }>>("/api/v1/rbac/roles?include_system=false");
        return { status: answer.status(), found: listed.some((r) => r.name === name) };
      },
    });
  });

  // Every row of the matrix was a step, in the file's order.
  expect(checked, "the rows this day checked").toEqual(rows.map((r) => r.id));
});
