/**
 * Dialog: the dashboard's one modal overlay (#369).
 *
 * While it is open:
 *   - focus moves into it (`initialFocusRef`, else the first `[data-autofocus]`,
 *     else the first control, else the dialog itself) and Tab and Shift+Tab
 *     wrap inside it;
 *   - the page behind it, `#__next`, carries the `inert` attribute, so it can
 *     be neither clicked, focused nor read. The dialog portals to
 *     `document.body`, beside `#__next`, so it is not inert itself; Next's
 *     route announcer is a child of body too and stays live. A dialog opened
 *     over another makes the lower one inert as well;
 *   - the page does not scroll (`overflow: hidden` on body, counted, so two
 *     open dialogs restore it once);
 *   - Escape calls `onClose`, unless `dismissible={false}` or `busy`. The key
 *     is taken in the capture phase on `document` and stopped there, so the
 *     header's and the shell's own Escape handlers (bubble phase, on
 *     `document`) never also act on it. Only the topmost dialog acts.
 *
 * On close (or unmount) focus goes back: to `returnFocus` if given (a ref, or
 * a function called at close that returns the element), else to the element
 * that had focus when it opened, unless that was body or has left the page,
 * else to body. A dialog that is not the topmost when it closes gives no focus
 * back: the one above it keeps it.
 *
 * Where a browser has no `inert` (Firefox 111, the oldest Firefox Next
 * compiles for, is one), the Tab trap is what keeps focus inside. With no
 * `#__next` there is nothing to make inert: under Jest that throws (render
 * with `renderInAppRoot`), in development it logs.
 *
 * A page that announces something in a live region as it closes a dialog
 * (`Saved changes to bob.`) writes the text with `useDeferredAnnouncement`'s
 * third value, one commit after the dialog has taken `inert` off the page.
 *
 * There is no backdrop click to close and no close button of its own: each
 * dialog keeps the controls its content already has.
 */
import {
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import { createPortal } from 'react-dom';

export type DialogSize = 'sm' | 'md' | 'lg';

export interface DialogProps {
  open: boolean;
  /** Called on Escape (unless refused). The parent sets `open` to false. */
  onClose: () => void;
  /** The dialog's accessible name, shown as its heading. */
  title: string;
  /** Shown under the title and read as the dialog's description. */
  description?: ReactNode;
  children?: ReactNode;
  /** `false`: Escape does nothing; the content's own controls are the only way out. */
  dismissible?: boolean;
  /** While true, Escape does nothing (a request that must finish is in flight). */
  busy?: boolean;
  /** The element that gets focus on open. */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** Where focus goes on close: a ref, or a function called at close. */
  returnFocus?: RefObject<HTMLElement | null> | (() => HTMLElement | null | undefined);
  size?: DialogSize;
  /** `data-testid` of the dialog element (the one with role="dialog"). */
  testId?: string;
}

/** The element Next's pages router renders every page into. */
export const APP_ROOT_ID = '__next';

const SIZES: Record<DialogSize, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-2xl',
};

// ---------------------------------------------------------------------------
// Shared state of every open Dialog
// ---------------------------------------------------------------------------

interface OpenDialog {
  /** The portal's outer element, made inert when another dialog opens above. */
  root: HTMLElement;
  /** The role="dialog" element. */
  panel: HTMLElement;
}

/** Open dialogs, bottom first. */
const stack: OpenDialog[] = [];
/** Elements a Dialog made inert; an `inert` someone else set is never removed. */
const madeInert = new Set<Element>();
let scrollLocks = 0;
let overflowBeforeLock = '';

/** Make `#__next` and every dialog but the topmost inert; undo what no longer applies. */
function syncInert(): void {
  const wanted = new Set<Element>();
  const top = stack[stack.length - 1];
  if (top) {
    const app = document.getElementById(APP_ROOT_ID);
    if (app) wanted.add(app);
    for (const dialog of stack) if (dialog !== top) wanted.add(dialog.root);
  }
  wanted.forEach((el) => {
    if (!el.hasAttribute('inert')) {
      // The attribute, not the `inert` property: jsdom implements no property,
      // and the attribute is what every browser reads.
      el.setAttribute('inert', '');
      madeInert.add(el);
    }
  });
  Array.from(madeInert).forEach((el) => {
    if (!wanted.has(el)) {
      el.removeAttribute('inert');
      madeInert.delete(el);
    }
  });
}

function lockScroll(): void {
  if (scrollLocks === 0) {
    overflowBeforeLock = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
  }
  scrollLocks += 1;
}

function unlockScroll(): void {
  if (scrollLocks === 0) return;
  scrollLocks -= 1;
  if (scrollLocks === 0) document.body.style.overflow = overflowBeforeLock;
}

const TABBABLE = [
  'a[href]',
  'area[href]',
  'button',
  'input',
  'select',
  'textarea',
  'iframe',
  'summary',
  '[tabindex]',
  '[contenteditable]:not([contenteditable="false"])',
].join(',');

/** The controls inside `container` that Tab reaches, in document order. */
function tabbables(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(TABBABLE)).filter(
    (el) =>
      el.tabIndex >= 0 &&
      !el.matches(':disabled') &&
      !(el instanceof HTMLInputElement && el.type === 'hidden') &&
      el.closest('[hidden], [inert]') === null,
  );
}

function focusFirst(panel: HTMLElement, initial: HTMLElement | null | undefined): void {
  const candidates: Array<HTMLElement | null | undefined> = [
    initial,
    panel.querySelector<HTMLElement>('[data-autofocus]'),
    ...tabbables(panel),
    panel,
  ];
  for (const el of candidates) {
    if (!el || !panel.contains(el)) continue;
    el.focus();
    if (document.activeElement === el) return;
  }
}

function focusIfPossible(el: Element | null | undefined): boolean {
  if (!el || el === document.body || !el.isConnected || typeof (el as HTMLElement).focus !== 'function') {
    return false;
  }
  (el as HTMLElement).focus();
  return document.activeElement === el;
}

/** The return-focus chain: explicit target, then the opener, then body. Never throws. */
function giveFocusBack(
  returnFocus: DialogProps['returnFocus'],
  opener: Element | null,
  root: HTMLElement,
): void {
  try {
    const explicit = typeof returnFocus === 'function' ? returnFocus() : returnFocus?.current;
    if (focusIfPossible(explicit)) return;
  } catch {
    // A returnFocus that throws falls through to the opener.
  }
  try {
    if (focusIfPossible(opener)) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement && root.contains(active)) active.blur();
  } catch {
    // Nothing focusable is left; body has focus.
  }
}

function reportMissingAppRoot(): void {
  const message =
    `Dialog: no #${APP_ROOT_ID} element to make inert, so the page behind the dialog stays reachable. ` +
    'In a Jest test, render with renderInAppRoot (src/tests/helpers/renderInAppRoot.tsx).';
  if (process.env.NODE_ENV === 'test') throw new Error(message);
  if (process.env.NODE_ENV !== 'production') console.error(message);
}

const subscribeNothing = () => () => undefined;

// ---------------------------------------------------------------------------
// The component
// ---------------------------------------------------------------------------

export function Dialog(props: DialogProps) {
  const { open, title, description, children, size = 'md', testId } = props;
  const titleId = useId();
  const descriptionId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // Read at event and close time, so a change of `busy` or `onClose` needs no
  // new listener.
  const latest = useRef(props);
  latest.current = props;

  // False while hydrating a server render, true on every client render after:
  // the portal is created in the same render as the Dialog, so its children,
  // and the refs a consumer passes as `initialFocusRef`, exist at first focus.
  const canPortal = useSyncExternalStore(
    subscribeNothing,
    () => true,
    () => false,
  );

  // The element to give focus back to, read during the render that opens the
  // dialog: by its layout effect, an `autoFocus` child already has focus.
  const opener = useRef<Element | null>(null);
  const committedOpen = useRef(false);
  if (open && !committedOpen.current && typeof document !== 'undefined') {
    opener.current = document.activeElement;
  }

  useLayoutEffect(() => {
    if (!open || !canPortal) return undefined;
    const root = rootRef.current;
    const panel = panelRef.current;
    if (!root || !panel) return undefined;
    if (!document.getElementById(APP_ROOT_ID)) reportMissingAppRoot();

    committedOpen.current = true;
    const entry: OpenDialog = { root, panel };
    const openedFrom = opener.current;
    stack.push(entry);
    syncInert();
    lockScroll();

    const onKeyDown = (event: KeyboardEvent) => {
      if (stack[stack.length - 1] !== entry) return; // only the topmost acts
      if (event.key === 'Escape' || event.key === 'Esc') {
        if (event.isComposing) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        const { dismissible = true, busy = false, onClose } = latest.current;
        if (dismissible && !busy) onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const controls = tabbables(panel);
      const active = document.activeElement;
      const inside = active !== null && panel.contains(active);
      let target: HTMLElement | null = null;
      if (controls.length === 0) {
        target = panel;
      } else if (event.shiftKey) {
        if (!inside || active === controls[0] || active === panel) target = controls[controls.length - 1];
      } else if (!inside || active === controls[controls.length - 1]) {
        target = controls[0];
      }
      if (!target) return; // a Tab between two controls inside: the browser's own
      event.preventDefault();
      event.stopImmediatePropagation();
      target.focus();
    };
    document.addEventListener('keydown', onKeyDown, true);

    focusFirst(panel, latest.current.initialFocusRef?.current);

    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      committedOpen.current = false;
      const wasTop = stack[stack.length - 1] === entry;
      const at = stack.indexOf(entry);
      if (at >= 0) stack.splice(at, 1);
      // `inert` comes off before focus moves: an inert element cannot take it.
      syncInert();
      unlockScroll();
      if (wasTop) giveFocusBack(latest.current.returnFocus, openedFrom, root);
    };
  }, [open, canPortal]);

  if (!open || !canPortal) return null;

  return createPortal(
    <div
      ref={rootRef}
      data-dialog-root=""
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description != null ? descriptionId : undefined}
        tabIndex={-1}
        data-testid={testId}
        className={`w-full ${SIZES[size]} max-h-full overflow-y-auto rounded-lg bg-white p-6 shadow-xl focus:outline-none`}
      >
        <h2 id={titleId} className="mb-1 text-lg font-semibold text-slate-900">
          {title}
        </h2>
        {description != null && (
          <div id={descriptionId} className="mb-4 text-sm text-slate-600">
            {description}
          </div>
        )}
        {children}
      </div>
    </div>,
    document.body,
  );
}

/**
 * Live-region text that a page writes as it closes a Dialog.
 *
 * `[text, setText, announceAfterClose]`: the first two are `useState`'s. The
 * third sets the text one commit later, after the commit that closes the
 * dialog, in which the dialog takes `inert` off `#__next`; text written in that
 * same commit would change under an inert ancestor. Call it in the handler that
 * closes the dialog.
 */
export function useDeferredAnnouncement(
  initial = '',
): [string, (text: string) => void, (text: string) => void] {
  const [text, setText] = useState(initial);
  const [pending, setPending] = useState<string | null>(null);
  useEffect(() => {
    if (pending === null) return;
    setText(pending);
    setPending(null);
  }, [pending]);
  const announceAfterClose = useCallback((next: string) => setPending(next), []);
  return [text, setText, announceAfterClose];
}
