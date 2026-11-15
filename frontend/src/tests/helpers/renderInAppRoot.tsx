/**
 * Render the way the dashboard is mounted: inside `<div id="__next">` (#369).
 *
 * Next's pages router renders every page into `#__next`, and a Dialog makes
 * that element `inert` while it is open, portalling itself to `document.body`
 * beside it. Testing Library's own `render` puts the tree in a bare `<div>`, so
 * a Dialog rendered with it has nothing to make inert: under Jest the Dialog
 * throws, rather than letting a test pass with the background still live.
 *
 * `baseElement` stays `document.body`, so `screen` and the result's queries
 * find the dialog in its portal; `appRoot` is the `#__next` element.
 */
import type { ReactElement } from 'react';
import { render, type RenderOptions, type RenderResult } from '@testing-library/react';

export const APP_ROOT_ID = '__next';

export type AppRootRenderResult = RenderResult & { appRoot: HTMLElement };

export function renderInAppRoot(
  ui: ReactElement,
  options: Omit<RenderOptions, 'container' | 'baseElement'> = {},
): AppRootRenderResult {
  // A root left by an earlier render in the same test (after `unmount()`, it
  // is empty) would be the one `getElementById` finds.
  document.querySelectorAll(`body > #${APP_ROOT_ID}`).forEach((stale) => {
    if (stale.childNodes.length === 0) stale.remove();
  });
  const appRoot = document.createElement('div');
  appRoot.id = APP_ROOT_ID;
  document.body.appendChild(appRoot);
  const result = render(ui, { ...options, container: appRoot, baseElement: document.body });
  return { ...result, appRoot };
}
