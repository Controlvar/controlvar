import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useTrackEvent, type TrackEventOptions } from '@getexperimently/react-sdk';

export type TrackFn = (eventName: string, properties?: Record<string, unknown>, options?: TrackEventOptions) => void;

export interface LoggedEvent {
  id: number;
  name: string;
  properties?: Record<string, unknown>;
  options?: TrackEventOptions;
  at: number;
}

interface EventLogContextValue {
  events: LoggedEvent[];
  track: TrackFn;
  clear: () => void;
}

export const MAX_LOGGED_EVENTS = 50;

const EventLogContext = createContext<EventLogContextValue | null>(null);

/**
 * Wraps the SDK's `useTrackEvent()` so every event the storefront sends is also kept in
 * an in-memory log for the "Powered by Controlvar" panel. The returned `track` has a
 * stable identity, so it is safe to list in effect dependencies.
 */
export function EventLogProvider({ children }: { children: React.ReactNode }) {
  const sdkTrack = useTrackEvent();
  const sdkTrackRef = useRef(sdkTrack);
  sdkTrackRef.current = sdkTrack;

  const [events, setEvents] = useState<LoggedEvent[]>([]);
  const nextId = useRef(1);

  const track = useCallback<TrackFn>((eventName, properties, options) => {
    const entry: LoggedEvent = { id: nextId.current, name: eventName, properties, options, at: Date.now() };
    nextId.current += 1;
    setEvents((prev) => [...prev, entry].slice(-MAX_LOGGED_EVENTS));
    sdkTrackRef.current(eventName, properties, options);
  }, []);

  const clear = useCallback(() => setEvents([]), []);
  const value = useMemo(() => ({ events, track, clear }), [events, track, clear]);

  return <EventLogContext.Provider value={value}>{children}</EventLogContext.Provider>;
}

/** Track an event (and record it in the panel log). Falls back to the raw SDK hook outside the provider. */
export function useTrack(): TrackFn {
  const ctx = useContext(EventLogContext);
  const sdkTrack = useTrackEvent();
  return ctx ? ctx.track : sdkTrack;
}

export function useEventLog(): LoggedEvent[] {
  return useContext(EventLogContext)?.events ?? [];
}

/** Fire `page_view` once per mount (guarded against StrictMode double-invocation). */
export function usePageView(page: string): void {
  const track = useTrack();
  const fired = useRef<string | null>(null);
  useEffect(() => {
    if (fired.current === page) return;
    fired.current = page;
    track('page_view', { page });
  }, [page, track]);
}
