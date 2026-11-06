/**
 * The shared Dialog (#369): focus moves in, stays in and goes back to the
 * opener; Escape closes the topmost dialog unless closing is refused; the page
 * behind it is inert and does not scroll.
 *
 * jsdom implements no `inert` (it neither blocks focus nor hides anything for
 * it) and user-event ignores the attribute, so these tests read the attribute
 * and drive the Tab trap, which is the fallback where a browser has no `inert`.
 * The real-browser proof is tests/e2e/dialog-focus.journey.spec.ts.
 *
 * Every render goes through renderInAppRoot: the page sits in `#__next`, as
 * Next mounts it, and the dialog portals to `document.body` beside it.
 */
import React, { useEffect, useRef, useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { Dialog, useDeferredAnnouncement, type DialogProps } from '@/components/ui/Dialog';
import { APP_ROOT_ID, renderInAppRoot } from '@/tests/helpers/renderInAppRoot';
import { watchLiveRegions } from '@/tests/helpers/watchLiveRegions';

type HarnessProps = Omit<Partial<DialogProps>, 'open' | 'onClose' | 'children'> & {
  startOpen?: boolean;
  onClose?: () => void;
  children?: React.ReactNode;
};

/** A page with an opener, a control behind the dialog, and a three-control form. */
function Harness({ startOpen = false, onClose, children, ...props }: HarnessProps) {
  const [open, setOpen] = useState(startOpen);
  const close = () => {
    onClose?.();
    setOpen(false);
  };
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      <button type="button">Background</button>
      <Dialog title="Rename flag" {...props} open={open} onClose={close}>
        {children ?? (
          <>
            <input aria-label="Name" />
            <button type="button" onClick={close}>
              Cancel
            </button>
            <button type="button">Save</button>
          </>
        )}
      </Dialog>
    </>
  );
}

const appRoot = () => document.getElementById(APP_ROOT_ID) as HTMLElement;
const escape = (target: Document | Element = document) => fireEvent.keyDown(target, { key: 'Escape' });

afterEach(() => {
  // Nothing a dialog sets may outlive its test. Unmount first: a test may end
  // with its dialog open, and Testing Library's own cleanup may run after this.
  cleanup();
  expect(document.querySelectorAll('[inert]')).toHaveLength(0);
  expect(document.body.style.overflow).toBe('');
});

describe('Dialog: Tab stays inside', () => {
  it('Tab and Shift+Tab wrap among the controls and never reach the page behind', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Harness />);
    await user.click(screen.getByRole('button', { name: 'Open' }));
    const name = screen.getByRole('textbox', { name: 'Name' });
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    const save = screen.getByRole('button', { name: 'Save' });

    expect(name).toHaveFocus();
    await user.tab();
    expect(cancel).toHaveFocus();
    await user.tab();
    expect(save).toHaveFocus();
    await user.tab();
    expect(name).toHaveFocus();
    await user.tab();
    expect(cancel).toHaveFocus();

    await user.tab({ shift: true });
    expect(name).toHaveFocus();
    await user.tab({ shift: true });
    expect(save).toHaveFocus();
  });

  it('Tab from a focus that escaped the dialog brings it back to the first control', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Harness startOpen />);
    screen.getByRole('button', { name: 'Background' }).focus();
    await user.tab();
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
  });

  it('with nothing to tab to, focus stays on the dialog itself', async () => {
    const user = userEvent.setup();
    renderInAppRoot(
      <Harness startOpen>
        <p>Nothing here can take focus.</p>
      </Harness>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Rename flag' });
    expect(dialog).toHaveFocus();
    await user.tab();
    expect(dialog).toHaveFocus();
    await user.tab({ shift: true });
    expect(dialog).toHaveFocus();
  });
});

describe('Dialog: focus returns', () => {
  it('to the opener after Escape, and after the dialog’s own close control', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Harness />);
    const opener = screen.getByRole('button', { name: 'Open' });

    await user.click(opener);
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();

    await user.click(opener);
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('an explicit returnFocus ref wins over the opener', async () => {
    const user = userEvent.setup();
    function WithTarget() {
      const target = useRef<HTMLButtonElement>(null);
      return (
        <>
          <button type="button" ref={target}>
            Target
          </button>
          <Harness returnFocus={target} />
        </>
      );
    }
    renderInAppRoot(<WithTarget />);
    await user.click(screen.getByRole('button', { name: 'Open' }));
    await user.keyboard('{Escape}');
    expect(screen.getByRole('button', { name: 'Target' })).toHaveFocus();
  });

  it('returnFocus as a function is called once, at close, and its element gets focus', async () => {
    const user = userEvent.setup();
    const pick = jest.fn(() => screen.getByRole('button', { name: 'Background' }));
    renderInAppRoot(<Harness returnFocus={pick} />);
    await user.click(screen.getByRole('button', { name: 'Open' }));
    expect(pick).not.toHaveBeenCalled();
    await user.keyboard('{Escape}');
    expect(pick).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Background' })).toHaveFocus();
  });

  it('when the click did not focus the opener (Safari, Firefox), returnFocus still gets focus back', () => {
    // A click there leaves focus on <body>, so the element captured at open
    // is body: chain step 2 is skipped and only an explicit returnFocus helps.
    function SafariPage() {
      const opener = useRef<HTMLButtonElement>(null);
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" ref={opener} onClick={() => setOpen(true)}>
            Roll back
          </button>
          <Dialog open={open} onClose={() => setOpen(false)} title="Confirm" returnFocus={opener}>
            <button type="button">Cancel</button>
          </Dialog>
        </>
      );
    }
    renderInAppRoot(<SafariPage />);
    const opener = screen.getByRole('button', { name: 'Roll back' });
    expect(document.activeElement).toBe(document.body);
    fireEvent.click(opener); // no focus, as in Safari
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
    escape();
    expect(opener).toHaveFocus();
  });

  it('with no returnFocus and nothing focused at open, focus ends on body', () => {
    renderInAppRoot(<Harness />);
    expect(document.activeElement).toBe(document.body);
    fireEvent.click(screen.getByRole('button', { name: 'Open' }));
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
    escape();
    expect(document.activeElement).toBe(document.body);
  });

  it('an opener that left the page is not focused; focus ends on body, and nothing throws', async () => {
    const user = userEvent.setup();
    function VanishingOpener() {
      const [open, setOpen] = useState(false);
      return (
        <>
          {!open && (
            <button type="button" onClick={() => setOpen(true)}>
              Open
            </button>
          )}
          <Dialog open={open} onClose={() => setOpen(false)} title="Rename flag">
            <button type="button">Cancel</button>
          </Dialog>
        </>
      );
    }
    renderInAppRoot(<VanishingOpener />);
    const first = screen.getByRole('button', { name: 'Open' });
    const focusSpy = jest.spyOn(first, 'focus');
    await user.click(first);
    expect(first.isConnected).toBe(false);
    focusSpy.mockClear();
    await user.keyboard('{Escape}');
    expect(focusSpy).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(document.body);
  });

  it('a returnFocus that throws does not stop the close: the opener gets focus', async () => {
    const user = userEvent.setup();
    const broken = () => {
      throw new Error('gone');
    };
    renderInAppRoot(<Harness returnFocus={broken} />);
    const opener = screen.getByRole('button', { name: 'Open' });
    await user.click(opener);
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('when the parent unmounts the open Dialog, focus, inert and scrolling are all given back', async () => {
    const user = userEvent.setup();
    function Parent({ show }: { show: boolean }) {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Open
          </button>
          {show && (
            <Dialog open={open} onClose={() => setOpen(false)} title="Rename flag">
              <button type="button">Cancel</button>
            </Dialog>
          )}
        </>
      );
    }
    const { rerender } = renderInAppRoot(<Parent show />);
    const opener = screen.getByRole('button', { name: 'Open' });
    await user.click(opener);
    expect(appRoot()).toHaveAttribute('inert');
    expect(document.body.style.overflow).toBe('hidden');

    rerender(<Parent show={false} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(appRoot()).not.toHaveAttribute('inert');
    expect(document.body.style.overflow).toBe('');
  });
});

describe('Dialog: Escape', () => {
  it('calls onClose exactly once, from document and from inside the dialog', () => {
    const onClose = jest.fn();
    renderInAppRoot(<Harness startOpen onClose={onClose} />);
    escape(screen.getByRole('textbox', { name: 'Name' }));
    expect(onClose).toHaveBeenCalledTimes(1);

    onClose.mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Open' }));
    escape(document);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('is ignored while busy and works again once busy clears', () => {
    const onClose = jest.fn();
    const { rerender } = renderInAppRoot(
      <Dialog open onClose={onClose} title="Saving" busy>
        <button type="button">Cancel</button>
      </Dialog>,
    );
    escape();
    expect(onClose).toHaveBeenCalledTimes(0);
    rerender(
      <Dialog open onClose={onClose} title="Saving" busy={false}>
        <button type="button">Cancel</button>
      </Dialog>,
    );
    escape();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('is ignored when dismissible={false}', () => {
    const onClose = jest.fn();
    renderInAppRoot(
      <Dialog open onClose={onClose} title="Copy your password" dismissible={false}>
        <button type="button">Done</button>
      </Dialog>,
    );
    escape();
    escape(screen.getByRole('button', { name: 'Done' }));
    expect(onClose).toHaveBeenCalledTimes(0);
  });

  it('is ignored while an input method is composing', () => {
    const onClose = jest.fn();
    renderInAppRoot(<Harness startOpen onClose={onClose} />);
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Name' }), { key: 'Escape', isComposing: true });
    expect(onClose).toHaveBeenCalledTimes(0);
  });
});

describe('Dialog: the page behind is inert', () => {
  it('#__next carries the inert attribute while open and none after close', () => {
    renderInAppRoot(<Harness />);
    expect(appRoot()).not.toHaveAttribute('inert');
    fireEvent.click(screen.getByRole('button', { name: 'Open' }));
    expect(appRoot()).toHaveAttribute('inert');
    escape();
    expect(appRoot()).not.toHaveAttribute('inert');
  });

  it('the dialog sits beside #__next, in body, and a control inside it works', () => {
    const onSave = jest.fn();
    renderInAppRoot(
      <Harness startOpen>
        <button type="button" onClick={onSave}>
          Save
        </button>
      </Harness>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Rename flag' });
    expect(appRoot().contains(dialog)).toBe(false);
    expect(dialog.closest('[inert]')).toBeNull();
    expect(document.body.contains(dialog)).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it('leaves alone an inert it did not set', () => {
    renderInAppRoot(<Harness />);
    appRoot().setAttribute('inert', '');
    fireEvent.click(screen.getByRole('button', { name: 'Open' }));
    escape();
    expect(appRoot()).toHaveAttribute('inert');
    appRoot().removeAttribute('inert');
  });
});

describe('Dialog: axe (scanned at the dialog node, which a container scan no longer reaches)', () => {
  const axeOptions: axe.RunOptions = { rules: { 'color-contrast': { enabled: false } } };

  it('the open dialog, with a description, has no violations', async () => {
    renderInAppRoot(<Harness startOpen description="The key used by the SDKs does not change." />);
    // By role alone: a lost name must be axe's finding, not a failed query.
    const dialog = screen.getByRole('dialog');
    expect(document.body).toContainElement(dialog);
    const result = await axe.run(dialog, axeOptions);
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(', ')}`)).toStrictEqual([]);
    // The scan reached the dialog: the rule that names it ran on it and passed.
    expect(result.passes.map((p) => p.id)).toContain('aria-dialog-name');
  });
});

describe('Dialog: labelling', () => {
  it('is a modal dialog named by its title, and described by its description', () => {
    renderInAppRoot(<Harness startOpen description="The key used by the SDKs does not change." />);
    const dialog = screen.getByRole('dialog', { name: 'Rename flag' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleDescription('The key used by the SDKs does not change.');
    expect(screen.getByRole('heading', { level: 2, name: 'Rename flag' })).toBeInTheDocument();
  });

  it('has no aria-describedby without a description', () => {
    renderInAppRoot(<Harness startOpen />);
    expect(screen.getByRole('dialog', { name: 'Rename flag' })).not.toHaveAttribute('aria-describedby');
  });

  it('two mounted at once have distinct ids, each naming its own title', () => {
    renderInAppRoot(
      <>
        <Dialog open onClose={() => undefined} title="First" description="One">
          <button type="button">A</button>
        </Dialog>
        <Dialog open onClose={() => undefined} title="Second" description="Two">
          <button type="button">B</button>
        </Dialog>
      </>,
    );
    const first = screen.getByRole('dialog', { name: 'First' });
    const second = screen.getByRole('dialog', { name: 'Second' });
    expect(first.getAttribute('aria-labelledby')).not.toBe(second.getAttribute('aria-labelledby'));
    expect(first.getAttribute('aria-describedby')).not.toBe(second.getAttribute('aria-describedby'));
    expect(first).toHaveAccessibleDescription('One');
    expect(second).toHaveAccessibleDescription('Two');
  });

  it('puts testId on the dialog element', () => {
    renderInAppRoot(<Harness startOpen testId="rename-dialog" />);
    expect(screen.getByTestId('rename-dialog')).toBe(screen.getByRole('dialog', { name: 'Rename flag' }));
  });
});

describe('Dialog: the page does not scroll behind it', () => {
  it('sets overflow hidden on body while open, and restores what was there on close', () => {
    document.body.style.overflow = 'scroll';
    renderInAppRoot(<Harness />);
    fireEvent.click(screen.getByRole('button', { name: 'Open' }));
    expect(document.body.style.overflow).toBe('hidden');
    escape();
    expect(document.body.style.overflow).toBe('scroll');
    document.body.style.overflow = '';
  });

  it('two open dialogs share one lock: closing the top one keeps it, closing both restores it', () => {
    function Two() {
      const [a, setA] = useState(true);
      const [b, setB] = useState(true);
      return (
        <>
          <button type="button" onClick={() => setA(false)}>
            Close A
          </button>
          <button type="button" onClick={() => setB(false)}>
            Close B
          </button>
          <Dialog open={a} onClose={() => setA(false)} title="A">
            <button type="button">a</button>
          </Dialog>
          <Dialog open={b} onClose={() => setB(false)} title="B">
            <button type="button">b</button>
          </Dialog>
        </>
      );
    }
    renderInAppRoot(<Two />);
    expect(document.body.style.overflow).toBe('hidden');
    escape(); // closes B, the topmost
    expect(screen.queryByRole('dialog', { name: 'B' })).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe('hidden');
    escape();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe('');
  });
});

/** A dialog that opens a second one from inside itself. */
function Stacked() {
  const [a, setA] = useState(false);
  const [b, setB] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setA(true)}>
        Open A
      </button>
      <Dialog open={a} onClose={() => setA(false)} title="A">
        <button type="button" onClick={() => setB(true)}>
          Open B
        </button>
        <button type="button">Other in A</button>
      </Dialog>
      <Dialog open={b} onClose={() => setB(false)} title="B">
        <input aria-label="In B" />
        <button type="button">Last in B</button>
      </Dialog>
    </>
  );
}

const dialogRoot = (name: string) =>
  screen.getByRole('dialog', { name }).closest('[data-dialog-root]') as HTMLElement;

describe('Dialog: a dialog over a dialog', () => {
  it('Tab stays in the top one; Escape closes only it; focus goes back to the control in A', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Stacked />);
    await user.click(screen.getByRole('button', { name: 'Open A' }));
    const openB = screen.getByRole('button', { name: 'Open B' });
    await user.click(openB);

    const inB = screen.getByRole('textbox', { name: 'In B' });
    expect(inB).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Last in B' })).toHaveFocus();
    await user.tab();
    expect(inB).toHaveFocus();

    // The lower dialog is inert too, not only the page.
    expect(dialogRoot('A')).toHaveAttribute('inert');
    expect(dialogRoot('B')).not.toHaveAttribute('inert');
    expect(appRoot()).toHaveAttribute('inert');

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'B' })).not.toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'A' })).toBeInTheDocument();
    expect(openB).toHaveFocus();
    expect(dialogRoot('A')).not.toHaveAttribute('inert');
    expect(appRoot()).toHaveAttribute('inert');

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open A' })).toHaveFocus();
    expect(appRoot()).not.toHaveAttribute('inert');
  });

  it('two opened in the same tick, closed bottom first, leave the right elements inert', () => {
    function SameTick() {
      const [a, setA] = useState(false);
      const [b, setB] = useState(false);
      return (
        <>
          <button
            type="button"
            onClick={() => {
              setA(true);
              setB(true);
            }}
          >
            Open both
          </button>
          <button type="button" onClick={() => setA(false)}>
            Close A
          </button>
          <Dialog open={a} onClose={() => setA(false)} title="A">
            <button type="button">a</button>
          </Dialog>
          <Dialog open={b} onClose={() => setB(false)} title="B">
            <button type="button">b</button>
          </Dialog>
        </>
      );
    }
    renderInAppRoot(<SameTick />);
    fireEvent.click(screen.getByRole('button', { name: 'Open both' }));
    expect(appRoot()).toHaveAttribute('inert');
    expect(dialogRoot('A')).toHaveAttribute('inert');
    expect(dialogRoot('B')).not.toHaveAttribute('inert');

    // A, the lower one, closes first (its parent decided, not Escape). Focus
    // stays in B: a dialog that is not on top gives no focus back.
    fireEvent.click(screen.getByRole('button', { name: 'Close A' }));
    expect(screen.queryByRole('dialog', { name: 'A' })).not.toBeInTheDocument();
    expect(appRoot()).toHaveAttribute('inert');
    expect(dialogRoot('B')).not.toHaveAttribute('inert');
    expect(screen.getByRole('button', { name: 'b' })).toHaveFocus();

    escape();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(appRoot()).not.toHaveAttribute('inert');
  });
});

describe('Dialog: first focus', () => {
  it('goes to the first control by default', () => {
    renderInAppRoot(<Harness startOpen />);
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
  });

  it('initialFocusRef wins over the first control: a destructive confirm opens on Cancel', () => {
    function Confirm() {
      const cancel = useRef<HTMLButtonElement>(null);
      return (
        <Dialog open onClose={() => undefined} title="Delete flag?" initialFocusRef={cancel}>
          <button type="button">Delete</button>
          <button type="button" ref={cancel}>
            Cancel
          </button>
        </Dialog>
      );
    }
    renderInAppRoot(<Confirm />);
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
  });

  it('then the first [data-autofocus] element', () => {
    renderInAppRoot(
      <Harness startOpen>
        <input aria-label="First" />
        <input aria-label="Second" data-autofocus />
      </Harness>,
    );
    expect(screen.getByRole('textbox', { name: 'Second' })).toHaveFocus();
  });

  it('skips a disabled initialFocusRef, and a disabled first control', () => {
    function Own() {
      const role = useRef<HTMLSelectElement>(null);
      return (
        <Dialog open onClose={() => undefined} title="Edit user" initialFocusRef={role}>
          <select aria-label="Role" ref={role} disabled>
            <option>Admin</option>
          </select>
          <button type="button">Cancel</button>
        </Dialog>
      );
    }
    renderInAppRoot(<Own />);
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
  });

  it('a consumer’s own content exists at first focus (a ref into it is already set)', () => {
    // The portal's children are rendered with the Dialog, not one effect
    // later, so the consumer's refs are attached before focus moves.
    function Consumer() {
      const field = useRef<HTMLInputElement>(null);
      const [seen, setSeen] = useState<string>('unset');
      return (
        <>
          <Dialog open onClose={() => undefined} title="Rename flag" initialFocusRef={field}>
            <input aria-label="Field" ref={field} onFocus={() => setSeen(field.current ? 'attached' : 'missing')} />
          </Dialog>
          <output>{seen}</output>
        </>
      );
    }
    renderInAppRoot(<Consumer />);
    expect(screen.getByRole('textbox', { name: 'Field' })).toHaveFocus();
    expect(screen.getByText('attached')).toBeInTheDocument();
  });

  it('a consumer that focuses its own field in an effect finds it rendered (the portal is not one effect late)', () => {
    function OwnEffect() {
      const field = useRef<HTMLInputElement>(null);
      useEffect(() => {
        field.current?.focus();
      }, []);
      return (
        <Dialog open onClose={() => undefined} title="Rename flag">
          <input aria-label="First" />
          <input aria-label="Second" ref={field} />
        </Dialog>
      );
    }
    renderInAppRoot(<OwnEffect />);
    expect(screen.getByRole('textbox', { name: 'Second' })).toHaveFocus();
  });
});

describe('Dialog: rendered without #__next', () => {
  it('throws under Jest, naming the helper, and leaves nothing behind', () => {
    const quiet = jest.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      expect(() =>
        render(
          <Dialog open onClose={() => undefined} title="Orphan">
            <button type="button">x</button>
          </Dialog>,
        ),
      ).toThrow(/renderInAppRoot/);
    } finally {
      quiet.mockRestore();
    }
    expect(document.querySelectorAll('[inert]')).toHaveLength(0);
    expect(document.body.style.overflow).toBe('');
  });
});

function SavePage({ sameBatch = false }: { sameBatch?: boolean }) {
  const [open, setOpen] = useState(false);
  const [status, setStatus, announce] = useDeferredAnnouncement();
  const save = () => {
    setOpen(false);
    (sameBatch ? setStatus : announce)('Saved.');
  };
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Edit
      </button>
      <div role="status" aria-live="polite">
        {status}
      </div>
      <Dialog open={open} onClose={() => setOpen(false)} title="Edit">
        <button type="button" onClick={save}>
          Save
        </button>
      </Dialog>
    </>
  );
}

// The example in docs/development/guidelines.md ("Dialogs"), as it is written
// there.
function RenameFlag({ rename }: { rename: (name: string) => Promise<void> }) {
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

describe('Dialog: the example in the development guidelines', () => {
  it('opens on the name field, saves, gives focus back and announces after the close', async () => {
    const user = userEvent.setup();
    const rename = jest.fn(() => Promise.resolve());
    renderInAppRoot(<RenameFlag rename={rename} />);
    const opener = screen.getByRole('button', { name: 'Rename' });
    await user.click(opener);
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveFocus();
    await user.keyboard('checkout-v2');
    const watch = watchLiveRegions();
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Renamed to checkout-v2.'));
    watch.stop();
    expect(rename).toHaveBeenCalledWith('checkout-v2');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(watch.changes).toStrictEqual([{ text: 'Renamed to checkout-v2.', underInert: false }]);
  });
});

describe('Dialog: an announcement made as it closes', () => {
  it('useDeferredAnnouncement writes the live region after #__next is no longer inert', () => {
    renderInAppRoot(<SavePage />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    const watch = watchLiveRegions();
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });
    watch.stop();
    expect(screen.getByRole('status')).toHaveTextContent('Saved.');
    expect(watch.changes).toStrictEqual([{ text: 'Saved.', underInert: false }]);
  });

  it('the watcher sees the defect it guards: the same text set in the closing batch lands under inert', () => {
    renderInAppRoot(<SavePage sameBatch />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    const watch = watchLiveRegions();
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });
    watch.stop();
    expect(watch.changes).toStrictEqual([{ text: 'Saved.', underInert: true }]);
  });
});
