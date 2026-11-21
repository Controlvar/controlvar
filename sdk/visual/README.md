# @controlvar/visual

The browser script for Controlvar's visual experiments. It is not published and not yet
usable: it is being built in this directory, and nothing in Controlvar offers it yet.

## Development

From `sdk/visual`, after `npm ci`:

- `npm run build` writes `dist/controlvar-visual.js` (the classic script) and `dist/index.mjs`
  (the ESM entry).
- `npm test` runs the build, then the unit tests.
- `npm run typecheck` checks the sources and the tests.

The browser tests are in `frontend/tests/e2e/runtime`, in the `page-runtime` Playwright project.
They load the script from the package `npm pack` makes, not from `dist/`.

## Licence

MIT, as every package under `sdk/`; see
[LICENSE](https://github.com/Controlvar/controlvar/blob/main/sdk/visual/LICENSE).
