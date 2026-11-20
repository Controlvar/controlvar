/**
 * InlineConfirm: the dashboard's one in-page confirmation panel (#1797).
 *
 * A question asked inside the page, beside the button that asked it ("Revoke
 * this key?", "Replace these rules?"), not over it. The modal overlay is
 * `Dialog`; this is its in-page sibling, and the only other file that may
 * write a `dialog` or `alertdialog` role (frontend/src/tests/a11y/dialog-sweep.test.ts).
 *
 * The caller mounts it while the question is shown and unmounts it on close,
 * and keeps its own buttons as children. While it is mounted:
 *
 *   - focus moves into it once: to `initialFocusRef`, else the first
 *     `[data-autofocus]` child, else the panel itself. Every caller marks its
 *     dismiss button (Cancel, Go back, Keep editing) `data-autofocus`, so a
 *     held Enter lands on the safe answer;
 *   - Escape inside it calls `onCancel` and stops there, so one Escape does
 *     one thing (the header's and the shell's menus listen on `document`).
 *     An Escape some handler already `preventDefault`ed, a key pressed while
 *     an input method is composing, and any Escape while `busy` do nothing;
 *   - there is no focus trap, it is not marked modal, and nothing is made
 *     inert: Tab leaves it like any other part of the page.
 *
 * On unmount, and only if focus was lost (the focused control went with the
 * panel, so `document.activeElement` is body), focus goes to `returnFocus`
 * if it is in the page and not disabled, else to `fallbackFocus`, else
 * nowhere. This runs in a passive effect's cleanup, never a layout effect's:
 * by then a ref to an opener that remounts on close (the panels whose opener
 * the question replaces) points at the new element. The default `returnFocus`
 * is the element focused when the panel first rendered, read during that
 * render, not in an effect: React StrictMode runs effects twice, and a second
 * read would find the panel's own Cancel.
 *
 * After a confirm the caller owns focus: it sets it in its own `useEffect`
 * (which runs after this cleanup), or moves it before the panel unmounts, and
 * this gives nothing back over it.
 *
 * Nothing is sent by opening, by focus moving, or by Escape.
 */
import { type KeyboardEvent, type ReactNode, type RefObject, useEffect, useRef, useState } from 'react';

/** Where focus goes: a ref, or a function called when the panel unmounts. */
export type FocusTarget = RefObject<HTMLElement | null> | (() => HTMLElement | null | undefined);

/** Colour classes only (background and border); layout stays the caller's. */
const TONES = {
  warning: 'border-amber-200 bg-amber-50',
  danger: 'border-red-200 bg-red-50',
} as const;

interface InlineConfirmBase {
  /** Id of the sentence the panel asks, read as its description. */
  describedBy?: string;
  /** Called on Escape, never while `busy`. The caller unmounts the panel. */
  onCancel: () => void;
  /**
   * A request is in flight: Escape does nothing. The caller makes its Cancel
   * do nothing, marks its confirm button `aria-disabled` (not `disabled`,
   * which drops focus to body in Chromium), and returns early from its
   * confirm handler, since `aria-disabled` does not stop a second click.
   */
  busy?: boolean;
  /** `role="alertdialog"` instead of `role="dialog"`. */
  alert?: boolean;
  /** The element that gets focus on open, before any `[data-autofocus]` child. */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** Where focus goes on close. Default: the element focused when the panel first rendered. */
  returnFocus?: FocusTarget;
  /** Where focus goes on close when `returnFocus` has left the page or is disabled. */
  fallbackFocus?: FocusTarget;
  tone?: keyof typeof TONES;
  className?: string;
  testId?: string;
  children?: ReactNode;
}

/** The accessible name: exactly one of `label` and `labelledBy`. */
export type InlineConfirmProps = InlineConfirmBase &
  ({ label: string; labelledBy?: never } | { labelledBy: string; label?: never });

function resolve(target: FocusTarget | undefined): HTMLElement | null {
  if (!target) return null;
  try {
    return (typeof target === 'function' ? target() : target.current) ?? null;
  } catch {
    return null;
  }
}

/** Focus `el` if it is in the page and can take focus; true when it did. */
function focusIfPossible(el: Element | null | undefined): boolean {
  if (!el || el === document.body || !el.isConnected || !(el instanceof HTMLElement)) return false;
  if (el.matches(':disabled')) return false;
  el.focus();
  return document.activeElement === el;
}

export function InlineConfirm(props: InlineConfirmProps) {
  const { label, labelledBy, describedBy, alert = false, tone, className = '', testId, children } = props;
  const panelRef = useRef<HTMLDivElement>(null);
  // Read at key and unmount time, so a change of `busy`, `onCancel` or the
  // focus targets needs no new effect.
  const latest = useRef(props);
  latest.current = props;

  // The opener: the element focused during the first render, before the
  // effect below moves focus in. A state initialiser runs on that render only.
  const [focusedAtOpen] = useState<Element | null>(() =>
    typeof document === 'undefined' ? null : document.activeElement,
  );

  useEffect(() => {
    const panel = panelRef.current;
    if (panel) {
      const candidates = [
        latest.current.initialFocusRef?.current,
        panel.querySelector<HTMLElement>('[data-autofocus]'),
      ];
      const first = candidates.find((el) => el && panel.contains(el));
      if (!focusIfPossible(first)) panel.focus();
    }
    return () => {
      const active = document.activeElement;
      // Focus is somewhere the user or the caller put it: leave it there.
      if (active !== null && active !== document.body) return;
      const { returnFocus, fallbackFocus } = latest.current;
      const back = returnFocus === undefined ? focusedAtOpen : resolve(returnFocus);
      if (focusIfPossible(back)) return;
      focusIfPossible(resolve(fallbackFocus));
    };
    // Once per mount: `focusedAtOpen` never changes, and the other targets are
    // read through `latest` when they are used.
  }, [focusedAtOpen]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.defaultPrevented) return;
    if (event.key !== 'Escape' && event.key !== 'Esc') return;
    if (event.nativeEvent.isComposing) return;
    // The panel's key, busy or not: it never also closes a menu behind it.
    event.stopPropagation();
    if (latest.current.busy) return;
    latest.current.onCancel();
  };

  const classes = [tone ? TONES[tone] : '', className, 'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600']
    .filter(Boolean)
    .join(' ');

  return (
    <div
      ref={panelRef}
      role={alert ? 'alertdialog' : 'dialog'}
      aria-label={label}
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      data-testid={testId}
      className={classes}
    >
      {children}
    </div>
  );
}
