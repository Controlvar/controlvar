# Development Guidelines

The rules a change to this repository follows. How to set up, run and test the
stack is in
[CONTRIBUTING.md](https://github.com/Controlvar/controlvar/blob/main/CONTRIBUTING.md),
and the conventions, with the reasons for them, are in
[CLAUDE.md](https://github.com/Controlvar/controlvar/blob/main/CLAUDE.md).

## Python

- Write for Python 3.11, with type annotations.
- Use Pydantic v2: `field_validator`, `model_validator`, `ConfigDict`, and
  `BaseSettings` from `pydantic_settings`.
- Import application code by its full path, rooted at `backend.app`:
  `from backend.app.models.metrics.metric import RawMetric`. Never root an import
  at `app`: Python loads `app.models.metrics.metric` as a different module from
  `backend.app.models.metrics.metric`, with different classes. This search finds
  such an import, and prints nothing when there is none:

    ```{.bash skip reason="dev: searches this repository's Python files"}
    grep -rnE '^\s*(from|import) app\.' backend/ modules/ --include='*.py'
    ```

- A new core model's module goes in `CORE_MODEL_MODULES` in
  `backend/app/models/__init__.py`, which `register_core_models()` imports.

## Lint and format

- Ruff formats and lints the Python and sorts its imports; its configuration is
  `[tool.ruff]` in `pyproject.toml`. Do not add black, isort or flake8 back.
- `make format` formats and fixes the Python in place. `make lint` runs what the
  `lint` check runs.
- CI, the pre-commit hook and the virtual environment install the same ruff
  version.

## Dialogs

- A modal dialog in the dashboard is `Dialog`, from
  `frontend/src/components/ui/Dialog.tsx`. Do not build an overlay by hand:
  `frontend/src/tests/a11y/dialog-sweep.test.ts` refuses `aria-modal`, a
  `<dialog>` element, and `fixed` with `inset-0` in one class string anywhere
  else.
- While a `Dialog` is open, Tab and Shift+Tab stay inside it, the page behind
  it is `inert`, and the page does not scroll. Escape calls `onClose`, unless
  `dismissible={false}` or `busy` is set.
- Focus opens on `initialFocusRef`, else on the first element marked
  `data-autofocus`, else on the first control. Give a confirmation that
  deletes something its Cancel button as `initialFocusRef`.
- On close, focus goes to `returnFocus` if you pass one, else back to the
  element that opened the dialog.
- Set a live region's text as the dialog closes with the third value of
  `useDeferredAnnouncement`: it writes the text after the page behind the
  dialog is no longer inert.
- In a Jest test, render with `renderInAppRoot` from
  `frontend/src/tests/helpers/renderInAppRoot.tsx`. Under Jest, a `Dialog`
  rendered without it throws.

```tsx
import { useRef, useState } from 'react';
import { Dialog, useDeferredAnnouncement } from '@/components/ui/Dialog';

export function RenameFlag({ rename }: { rename: (name: string) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [status, , announceAfterClose] = useDeferredAnnouncement();
  const nameField = useRef<HTMLInputElement>(null);

  const save = async () => {
    await rename(name);
    setOpen(false);
    announceAfterClose(`Renamed to ${name}.`);
  };

  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Rename
      </button>
      <p role="status">{status}</p>
      <Dialog open={open} onClose={() => setOpen(false)} title="Rename flag" initialFocusRef={nameField}>
        <label htmlFor="flag-name">Name</label>
        <input id="flag-name" ref={nameField} value={name} onChange={(e) => setName(e.target.value)} />
        <button type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
        <button type="button" onClick={save}>
          Save
        </button>
      </Dialog>
    </>
  );
}
```

## Pull requests

- Write the title as a conventional commit, `type(optional scope): description`,
  with `type` one of `feat fix perf deps docs build ci refactor test chore revert
  style`. The `conventional-title` check refuses any other title.
- Sign off every commit and add a regression test to every bug fix:
  `CONTRIBUTING.md` and the [Testing Guide](testing-guide.md) say how.
