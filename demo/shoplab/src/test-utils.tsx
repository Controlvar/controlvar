import React from 'react';
import { render, type RenderOptions } from '@testing-library/react';
import { EventLogProvider } from '@/lib/eventLog';
import { trackEventMock, __reset as resetSdk } from '@/__mocks__/controlvar-sdk';
import { __resetRouter } from '@/__mocks__/next-router';

/** Render inside the event-log provider, exactly as `_app.tsx` does. */
export function renderPage(ui: React.ReactElement, options?: RenderOptions) {
  return render(<EventLogProvider>{ui}</EventLogProvider>, options);
}

/** All `trackEvent` calls for a given event name: `[name, properties, options]` tuples. */
export function callsFor(eventName: string) {
  return trackEventMock.mock.calls.filter((c) => c[0] === eventName);
}

/** Reset SDK mock state, router mock and localStorage between tests. */
export function resetTestState(): void {
  resetSdk();
  __resetRouter();
  window.localStorage.clear();
}
