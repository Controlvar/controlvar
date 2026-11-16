/**
 * InlineConfirm (#1797): focus moves in, Escape cancels, focus comes back,
 * and nothing around the panel changes.
 *
 * Every case renders with renderInAppRoot, so `#__next` exists and the "page
 * not inert" assertions read a real node, and drives the keyboard with
 * user-event: `fireEvent.keyDown` Tab moves no focus, so a trap would pass
 * unseen. jsdom ignores `inert` for focus and queries, so the attribute is
 * what is asserted.
 */
import React, { StrictMode, useEffect, useRef, useState } from 'react';
import { fireEvent, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { InlineConfirm, type InlineConfirmProps } from '@/components/ui/InlineConfirm';
import { renderInAppRoot } from '@/tests/helpers/renderInAppRoot';

type PanelOptions = Partial<Omit<InlineConfirmProps, 'label' | 'labelledBy' | 'onCancel' | 'children'>>;

interface HarnessProps {
  onCancel?: () => void;
  onConfirm?: () => void;
  panel?: PanelOptions;
  /** Mark the dismiss button `data-autofocus`, as every caller does. */
  autofocus?: boolean;
  /** A control inside the panel that handles the key first. */
  child?: React.ReactNode;
}

/** A caller: an opener, the panel while open, and a control after it. */
function Harness({ onCancel = () => undefined, onConfirm = () => undefined, panel = {}, autofocus = true, child }: HarnessProps) {
  const [open, setOpen] = useState(false);
  const cancel = () => {
    onCancel();
    setOpen(false);
  };
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Opener
      </button>
      {open && (
        <InlineConfirm label="Revoke key Alpha" describedBy="question" onCancel={cancel} testId="panel" {...panel}>
          <p id="question">Revoke Alpha? Anything that uses it stops working.</p>
          {child}
          <button type="button" data-autofocus={autofocus ? '' : undefined} onClick={cancel}>
            Cancel
          </button>
          <button
            type="button"
            onClick={() => {
              onConfirm();
              setOpen(false);
            }}
          >
            Revoke key
          </button>
        </InlineConfirm>
      )}
      <button type="button">After</button>
    </>
  );
}

/** Opens the panel the way a keyboard user does: focus the opener, press Enter. */
async function openFromKeyboard(user: ReturnType<typeof userEvent.setup>) {
  screen.getByRole('button', { name: 'Opener' }).focus();
  await user.keyboard('{Enter}');
  return screen.getByTestId('panel');
}

const cancelButton = () => screen.getByRole('button', { name: 'Cancel' });
const confirmButton = () => screen.getByRole('button', { name: 'Revoke key' });
const opener = () => screen.getByRole('button', { name: 'Opener' });

describe('InlineConfirm: opening', () => {
  it('opens on the [data-autofocus] child: the Cancel a caller marks', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Harness />);
    await openFromKeyboard(user);
    expect(cancelButton()).toHaveFocus();
  });

  it('opens on initialFocusRef before any [data-autofocus] child', async () => {
    function WithInitial() {
      const confirmRef = useRef<HTMLButtonElement>(null);
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Opener
          </button>
          {open && (
            <InlineConfirm label="Question" onCancel={() => setOpen(false)} initialFocusRef={confirmRef} testId="panel">
              <button type="button" data-autofocus>
                Cancel
              </button>
              <button type="button" ref={confirmRef}>
                Revoke key
              </button>
            </InlineConfirm>
          )}
        </>
      );
    }
    const user = userEvent.setup();
    renderInAppRoot(<WithInitial />);
    await openFromKeyboard(user);
    expect(confirmButton()).toHaveFocus();
  });

  it('opens on the panel itself when no child is marked', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Harness autofocus={false} />);
    const panel = await openFromKeyboard(user);
    expect(panel).toHaveFocus();
    expect(panel).toHaveAttribute('tabindex', '-1');
  });

  it('is a dialog named by its label and described by its sentence, never aria-modal', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Harness />);
    await openFromKeyboard(user);
    const panel = screen.getByRole('dialog', { name: 'Revoke key Alpha' });
    expect(panel).toHaveAccessibleDescription('Revoke Alpha? Anything that uses it stops working.');
    expect(panel).not.toHaveAttribute('aria-modal');
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('with alert, is an alertdialog, still never aria-modal', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Harness panel={{ alert: true }} />);
    await openFromKeyboard(user);
    const panel = screen.getByRole('alertdialog', { name: 'Revoke key Alpha' });
    expect(panel).not.toHaveAttribute('aria-modal');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('takes its name from labelledBy when given that instead', () => {
    renderInAppRoot(
      <InlineConfirm labelledBy="heading" onCancel={() => undefined}>
        <h3 id="heading">Remove Copy?</h3>
      </InlineConfirm>,
    );
    expect(screen.getByRole('dialog', { name: 'Remove Copy?' })).toBeInTheDocument();
  });
});

describe('InlineConfirm: Escape', () => {
  it('Escape from the focused Cancel calls onCancel once and sends nothing', async () => {
    const onCancel = jest.fn();
    const onConfirm = jest.fn();
    const user = userEvent.setup();
    renderInAppRoot(<Harness onCancel={onCancel} onConfirm={onConfirm} />);
    await openFromKeyboard(user);
    expect(cancelButton()).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledTimes(0);
    expect(screen.queryByTestId('panel')).toBeNull();
  });

  it('Escape from any control inside, not only the panel element, cancels', async () => {
    const onCancel = jest.fn();
    const user = userEvent.setup();
    renderInAppRoot(<Harness onCancel={onCancel} />);
    await openFromKeyboard(user);
    fireEvent.keyDown(confirmButton(), { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('Escape with focus outside the panel does nothing to it', async () => {
    const onCancel = jest.fn();
    const user = userEvent.setup();
    renderInAppRoot(<Harness onCancel={onCancel} />);
    await openFromKeyboard(user);
    opener().focus();
    await user.keyboard('{Escape}');
    expect(onCancel).toHaveBeenCalledTimes(0);
    expect(screen.getByTestId('panel')).toBeInTheDocument();
  });

  it('Escape stops at the panel: a document key handler never sees it', async () => {
    const seen = jest.fn();
    document.addEventListener('keydown', seen);
    try {
      const user = userEvent.setup();
      renderInAppRoot(<Harness />);
      await openFromKeyboard(user);
      seen.mockClear();
      // The control: an Escape outside the panel does reach document.
      fireEvent.keyDown(opener(), { key: 'Escape' });
      expect(seen).toHaveBeenCalledTimes(1);
      seen.mockClear();
      await user.keyboard('{Escape}');
      expect(screen.queryByTestId('panel')).toBeNull();
      expect(seen).toHaveBeenCalledTimes(0);
    } finally {
      document.removeEventListener('keydown', seen);
    }
  });

  it('Escape while busy still stops at the panel: a document key handler never sees it', async () => {
    const seen = jest.fn();
    document.addEventListener('keydown', seen);
    try {
      const user = userEvent.setup();
      renderInAppRoot(<Harness panel={{ busy: true }} />);
      await openFromKeyboard(user);
      seen.mockClear();
      await user.keyboard('{Escape}');
      expect(screen.getByTestId('panel')).toBeInTheDocument();
      expect(seen).toHaveBeenCalledTimes(0);
    } finally {
      document.removeEventListener('keydown', seen);
    }
  });

  it('an Escape a child already prevented does nothing', async () => {
    const onCancel = jest.fn();
    const user = userEvent.setup();
    const child = (
      <button type="button" onKeyDown={(event) => event.preventDefault()}>
        Owns Escape
      </button>
    );
    renderInAppRoot(<Harness onCancel={onCancel} child={child} />);
    await openFromKeyboard(user);
    fireEvent.keyDown(screen.getByRole('button', { name: 'Owns Escape' }), { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(0);
    expect(screen.getByTestId('panel')).toBeInTheDocument();
  });

  it('an Escape a document capture listener already prevented does nothing', async () => {
    const onCancel = jest.fn();
    const prevent = (event: KeyboardEvent) => {
      if (event.key === 'Escape') event.preventDefault();
    };
    const user = userEvent.setup();
    renderInAppRoot(<Harness onCancel={onCancel} />);
    await openFromKeyboard(user);
    document.addEventListener('keydown', prevent, true);
    try {
      fireEvent.keyDown(cancelButton(), { key: 'Escape' });
      expect(onCancel).toHaveBeenCalledTimes(0);
      expect(screen.getByTestId('panel')).toBeInTheDocument();
    } finally {
      document.removeEventListener('keydown', prevent, true);
    }
  });

  it('an Escape while an input method is composing does nothing', async () => {
    const onCancel = jest.fn();
    const user = userEvent.setup();
    renderInAppRoot(<Harness onCancel={onCancel} />);
    await openFromKeyboard(user);
    fireEvent.keyDown(cancelButton(), { key: 'Escape', isComposing: true });
    expect(onCancel).toHaveBeenCalledTimes(0);
    fireEvent.keyDown(cancelButton(), { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('Escape does nothing while busy, and cancels again once busy clears', async () => {
    const onCancel = jest.fn();
    const user = userEvent.setup();
    const { rerender } = renderInAppRoot(<Harness onCancel={onCancel} panel={{ busy: true }} />);
    await openFromKeyboard(user);
    await user.keyboard('{Escape}');
    expect(onCancel).toHaveBeenCalledTimes(0);
    expect(cancelButton()).toHaveFocus();
    rerender(<Harness onCancel={onCancel} panel={{ busy: false }} />);
    await user.keyboard('{Escape}');
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

describe('InlineConfirm: the page around it', () => {
  it('traps no focus: Tab from its last button reaches the next control after it', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Harness />);
    await openFromKeyboard(user);
    await user.tab();
    expect(confirmButton()).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'After' })).toHaveFocus();
    await user.tab({ shift: true });
    await user.tab({ shift: true });
    await user.tab({ shift: true });
    expect(opener()).toHaveFocus();
  });

  it('leaves the page usable: #__next is neither inert nor aria-hidden while it is open', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Harness />);
    await openFromKeyboard(user);
    const app = document.getElementById('__next');
    expect(app).not.toBeNull();
    expect(app).toContainElement(screen.getByTestId('panel'));
    expect(app).not.toHaveAttribute('inert');
    expect(app).not.toHaveAttribute('aria-hidden');
    expect(document.body.querySelector('[inert], [aria-hidden="true"]')).toBeNull();
    // The page behind is still found by role, so it is still read.
    expect(screen.getByRole('button', { name: 'Opener' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'After' })).toBeInTheDocument();
  });
});

describe('InlineConfirm: focus back on close', () => {
  it('gives focus back to the opener after Cancel and after Escape', async () => {
    const user = userEvent.setup();
    renderInAppRoot(<Harness />);
    await openFromKeyboard(user);
    await user.click(cancelButton());
    expect(screen.queryByTestId('panel')).toBeNull();
    expect(opener()).toHaveFocus();

    await user.keyboard('{Enter}');
    expect(cancelButton()).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(opener()).toHaveFocus();
  });

  it('gives focus back to an opener that remounts on close, through its ref', async () => {
    // Panels 5, 6 and 10: the question replaces the button that asked it, so
    // the ref is null while the panel is open and points at a new button after.
    function Remounting() {
      const [open, setOpen] = useState(false);
      const openerRef = useRef<HTMLButtonElement>(null);
      return (
        <>
          {!open && (
            <button type="button" ref={openerRef} onClick={() => setOpen(true)}>
              Opener
            </button>
          )}
          {open && (
            <InlineConfirm label="Replace rules" onCancel={() => setOpen(false)} returnFocus={openerRef} testId="panel">
              <button type="button" data-autofocus onClick={() => setOpen(false)}>
                Cancel
              </button>
            </InlineConfirm>
          )}
        </>
      );
    }
    const user = userEvent.setup();
    renderInAppRoot(<Remounting />);
    await openFromKeyboard(user);
    expect(screen.queryByRole('button', { name: 'Opener' })).toBeNull();
    await user.keyboard('{Escape}');
    expect(opener()).toHaveFocus();
  });

  it('falls back to fallbackFocus when the opener has left the page', async () => {
    function OpenerGoes({ showOpener }: { showOpener: boolean }) {
      const [open, setOpen] = useState(false);
      const heading = useRef<HTMLHeadingElement>(null);
      return (
        <>
          <h2 ref={heading} tabIndex={-1}>
            Keys
          </h2>
          {showOpener && (
            <button type="button" onClick={() => setOpen(true)}>
              Opener
            </button>
          )}
          {open && (
            <InlineConfirm label="Question" onCancel={() => setOpen(false)} fallbackFocus={heading} testId="panel">
              <button type="button" data-autofocus onClick={() => setOpen(false)}>
                Cancel
              </button>
            </InlineConfirm>
          )}
        </>
      );
    }
    const user = userEvent.setup();
    const { rerender } = renderInAppRoot(<OpenerGoes showOpener />);
    await openFromKeyboard(user);
    rerender(<OpenerGoes showOpener={false} />);
    expect(cancelButton()).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.getByRole('heading', { name: 'Keys' })).toHaveFocus();
  });

  it('passes over a returnFocus that is disabled, to fallbackFocus', async () => {
    function DisabledOpener() {
      const [open, setOpen] = useState(false);
      const [busy, setBusy] = useState(false);
      const openerRef = useRef<HTMLButtonElement>(null);
      const after = useRef<HTMLButtonElement>(null);
      return (
        <>
          <button type="button" ref={openerRef} disabled={busy} onClick={() => setOpen(true)}>
            Opener
          </button>
          {open && (
            <InlineConfirm label="Question" onCancel={() => setOpen(false)} returnFocus={openerRef} fallbackFocus={after} testId="panel">
              <button
                type="button"
                data-autofocus
                onClick={() => {
                  setBusy(true);
                  setOpen(false);
                }}
              >
                Confirm
              </button>
            </InlineConfirm>
          )}
          <button type="button" ref={after}>
            After
          </button>
        </>
      );
    }
    const user = userEvent.setup();
    renderInAppRoot(<DisabledOpener />);
    await openFromKeyboard(user);
    await user.keyboard('{Enter}');
    expect(opener()).toBeDisabled();
    expect(screen.getByRole('button', { name: 'After' })).toHaveFocus();
  });

  it('gives nothing back over focus the caller moved before the panel unmounted', async () => {
    function MovesFirst() {
      const [open, setOpen] = useState(false);
      const status = useRef<HTMLParagraphElement>(null);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Opener
          </button>
          {open && (
            <InlineConfirm label="Question" onCancel={() => setOpen(false)} testId="panel">
              <button type="button" data-autofocus onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button
                type="button"
                onClick={() => {
                  // In the click handler, before the commit that unmounts the panel.
                  status.current?.focus();
                  setOpen(false);
                }}
              >
                Revoke key
              </button>
            </InlineConfirm>
          )}
          <p ref={status} tabIndex={-1}>
            Status
          </p>
        </>
      );
    }
    const user = userEvent.setup();
    renderInAppRoot(<MovesFirst />);
    await openFromKeyboard(user);
    await user.click(confirmButton());
    expect(screen.queryByTestId('panel')).toBeNull();
    expect(screen.getByText('Status')).toHaveFocus();
  });

  it("a caller's useEffect after a confirm keeps the focus it sets", async () => {
    function EffectAfter() {
      const [open, setOpen] = useState(false);
      const [done, setDone] = useState<string | null>(null);
      const status = useRef<HTMLParagraphElement>(null);
      useEffect(() => {
        if (done) status.current?.focus();
      }, [done]);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Opener
          </button>
          {open && (
            <InlineConfirm label="Question" onCancel={() => setOpen(false)} testId="panel">
              <button type="button" data-autofocus onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  setDone('Revoked Alpha.');
                }}
              >
                Revoke key
              </button>
            </InlineConfirm>
          )}
          <p ref={status} tabIndex={-1} role="status">
            {done}
          </p>
        </>
      );
    }
    const user = userEvent.setup();
    renderInAppRoot(<EffectAfter />);
    await openFromKeyboard(user);
    await user.click(confirmButton());
    expect(screen.getByRole('status')).toHaveTextContent('Revoked Alpha.');
    expect(screen.getByRole('status')).toHaveFocus();
  });

  it('under React StrictMode, opens on Cancel and gives focus back to the opener', async () => {
    const user = userEvent.setup();
    renderInAppRoot(
      <StrictMode>
        <Harness />
      </StrictMode>,
    );
    await openFromKeyboard(user);
    expect(cancelButton()).toHaveFocus();
    await user.click(cancelButton());
    expect(screen.queryByTestId('panel')).toBeNull();
    expect(opener()).toHaveFocus();
  });
});
