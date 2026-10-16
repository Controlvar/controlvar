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
  record: TrackFn;
  clear: () => void;
}

export const MAX_LOGGED_EVENTS = 50;

const EventLogContext = createContext<EventLogContextValue | null>(null);

/**
 * In-memory log of every event the app sends, for the Controlvar panel.
 *
 * It deliberately lives *outside* the SDK provider (which is re-mounted on every
 * device change and on "Refresh now"), so the log survives a refresh; the page
 * keys this provider on the device id so switching devices starts a fresh log.
 */
export function EventLogProvider({ children }: { children: React.ReactNode }) {
  const [events, setEvents] = useState<LoggedEvent[]>([]);
  const nextId = useRef(1);

  const record = useCallback<TrackFn>((eventName, properties, options) => {
    const entry: LoggedEvent = { id: nextId.current, name: eventName, properties, options, at: Date.now() };
    nextId.current += 1;
    setEvents((prev) => [...prev, entry].slice(-MAX_LOGGED_EVENTS));
  }, []);

  const clear = useCallback(() => setEvents([]), []);
  const value = useMemo(() => ({ events, record, clear }), [events, record, clear]);

  return <EventLogContext.Provider value={value}>{children}</EventLogContext.Provider>;
}

/**
 * Track an event through the SDK *and* record it in the panel log. Must be used
 * inside the SDK provider. The returned function is stable while the SDK user is.
 */
export function useTrack(): TrackFn {
  const sdkTrack = useTrackEvent();
  const record = useContext(EventLogContext)?.record;
  return useCallback<TrackFn>(
    (eventName, properties, options) => {
      record?.(eventName, properties, options);
      sdkTrack(eventName, properties, options);
    },
    [sdkTrack, record],
  );
}

export function useEventLog(): LoggedEvent[] {
  return useContext(EventLogContext)?.events ?? [];
}

/** Fire `screen_view {screen}` once per mount (guarded against StrictMode double-invocation). */
export function useScreenView(screen: string): void {
  const track = useTrack();
  const fired = useRef<string | null>(null);
  useEffect(() => {
    if (fired.current === screen) return;
    fired.current = screen;
    track('screen_view', { screen });
  }, [screen, track]);
}
