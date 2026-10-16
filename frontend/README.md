# Controlvar dashboard

The web dashboard for Controlvar: experiments, feature flags, results and administration.
It is a Next.js 16 application exported as static HTML (`output: 'export'`) and served by
nginx in the `experimently-web` image, which proxies `/api/`, `/health*` and `/ws/` to
the API container.

## Develop

```bash
npm ci
npm run dev
```

- `npm run dev`: http://localhost:3100, API at http://localhost:8000 by default

The API must be running (`docker compose up -d --wait api` from the repository root, or
`ENVIRONMENT=development uvicorn backend.app.main:app --reload`). Sign in with the seeded administrator
(`admin@demo.com` / `Demo1234!`).

## Test and build

```bash
npm test
npx tsc --noEmit
npm run lint
npm run build
npm run test:visual
```

- `npm test`: jest + testing-library
- `npx tsc --noEmit`: type check
- `npm run build`: static export to out/ (what the container serves)
- `npm run test:visual`: Playwright visual regression (needs the stack running)

The `Frontend Tests` CI check runs the first four. Playwright journeys live in
`tests/e2e/` and log in through the real `/login` page.

## Structure

```
src/
├── pages/            # one file per route: experiments/, feature-flags/, results/, admin/, login.tsx
├── components/       # AppShell (nav, user menu), RequireAuth, PageTitle, feature components
├── contexts/         # AuthContext (current user, login, logout)
├── services/         # api.ts (apiFetch: bearer token, 401 handling) and per-resource clients
├── types/
├── utils/
└── tests/            # jest tests mirroring src/
tests/e2e/            # Playwright journeys and page objects
scripts/nginx-routes.mjs   # generates nginx try_files for dynamic routes from the export
```

Every request to the backend goes through `apiFetch` in `src/services/api.ts`; there are
no bare `fetch` calls in `src/`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `""` (same origin) | Origin of the backend API. Leave empty in the container image (nginx proxies `/api/` to the backend). `npm run dev` falls back to `http://localhost:8000` when unset; set it in `.env.local` when the backend runs elsewhere. |
| `NEXT_PUBLIC_WS_URL` | derived from `NEXT_PUBLIC_API_URL` | Optional explicit WebSocket origin for live results. |

Values are inlined at build time (`next build`), so rebuild the static export after changing them. See `.env.example`.

## Authentication

The dashboard signs in through `POST /api/v1/auth/login` and keeps only the bearer token in
`localStorage["experimently.token"]`. `apiFetch` attaches the token to every request and
redirects to `/login?next=…` on a 401. There is no public sign-up: accounts are created by
an administrator. `<RequireAuth roles={[...]}>` guards pages by role; the `AppShell` hides
navigation the current role cannot use.

## Container image

From the repository root (the build context): the core profile by default, `--build-arg EXPERIMENTLY_PROFILE=full` for the full profile:

```bash
docker build -f frontend/Dockerfile -t experimently-web:core .
docker build -f frontend/Dockerfile -t experimently-web:full --build-arg EXPERIMENTLY_PROFILE=full .
```

`frontend/Dockerfile` runs `npm ci`, `npm run build`, generates the nginx route map, and
serves `out/` with `nginxinc/nginx-unprivileged` on port 8080. `docker-compose.yml` at the
repository root wires it to the API. See `docs/getting-started/modules.md` for what the two
profiles are.
