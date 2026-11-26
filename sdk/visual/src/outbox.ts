/**
 * The goal events not yet delivered, kept in localStorage `controlvar_outbox`
 * so that a page that goes away does not lose them: at most 50 (the oldest
 * goes first), each dropped 24 hours after it was made.
 *
 * Each event is `{event_type, user_id, experiment_key, event_id, metadata?}`:
 * the goal's event name, the visitor, the experiment, `<page-view id>:<event
 * name>` and the page view's website details. Nothing else is ever sent, and
 * every event read back is checked against that form first (a page can write
 * the key); one that fails is dropped.
 *
 * Events go through the one send function (with the API key, keepalive, no
 * cookies and no Referer, and its consent and backoff gates) to POST
 * /api/v1/tracking/batch, in calls of at most 100 events and 60 KiB of JSON;
 * a flush sends its calls one after the other, and never an event already on
 * its way. A 2xx answer removes the call's events (also those it
 * counts as failed or as duplicates); a 4xx other than 429 drops them; a 429,
 * a 5xx, a network failure or a call not made keeps them for the next flush.
 * The outbox is flushed when the script starts and after each new event. Two
 * tabs may send the same event; the API stores an event once per event_id.
 */
import { utf8Bytes } from "./config";
import { DETAIL_KEYS, detailKept } from "./details";
import { EVENT_NAME } from "./goals";
import { KEYS } from "./keys";
import type { Send } from "./send";

export const MAX_OUTBOX = 50;
export const OUTBOX_MS = 24 * 60 * 60 * 1000;
export const MAX_CHUNK_EVENTS = 100;
export const MAX_CHUNK_BYTES = 60 * 1024;
/** No event the script makes comes near this; a kept one over it is not the script's. */
const MAX_EVENT_BYTES = 4096;
const MAX_USER_ID_LENGTH = 255;
const MAX_KEY_LENGTH = 100;
/** A page-view id (a version 4 UUID), then `:` and the event name. */
const EVENT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}:([a-z0-9_]{1,100})$/;
const EVENT_FIELDS = ["event_type", "user_id", "experiment_key", "event_id", "metadata"];

export interface GoalEvent {
  event_type: string;
  user_id: string;
  experiment_key: string;
  event_id: string;
  metadata?: Record<string, string>;
}

interface Entry {
  /** When the event was made, in ms since the epoch. */
  at: number;
  event: GoalEvent;
}

export interface OutboxEnv {
  local(): Storage | null;
  now(): number;
  send: Send;
  /** The path the events are sent to. */
  path: string;
  /** Whether the script may read, store and send now. */
  allowed(): boolean;
}

export interface Outbox {
  /** Keeps new events (when allowed), then flushes. */
  add(events: GoalEvent[]): void;
  /** Sends the kept events that are not on their way already. */
  flush(): void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function metadataFrom(value: unknown): Record<string, string> | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (keys.length === 0) return null;
  const metadata: Record<string, string> = {};
  for (const key of keys) {
    if (DETAIL_KEYS.indexOf(key) < 0 || !detailKept(key, value[key])) return null;
    metadata[key] = value[key] as string;
  }
  return metadata;
}

/** A goal event in its one form, or null. */
export function eventFrom(value: unknown): GoalEvent | null {
  if (!isRecord(value)) return null;
  if (Object.keys(value).some((key) => EVENT_FIELDS.indexOf(key) < 0)) return null;
  const { event_type: type, user_id: userId, experiment_key: key, event_id: id } = value;
  if (typeof type !== "string" || !EVENT_NAME.test(type)) return null;
  if (typeof userId !== "string" || userId.length < 1 || userId.length > MAX_USER_ID_LENGTH) return null;
  if (typeof key !== "string" || key.length < 1 || key.length > MAX_KEY_LENGTH) return null;
  const named = typeof id === "string" ? EVENT_ID.exec(id) : null;
  if (named === null || named[1] !== type) return null;
  const event: GoalEvent = { event_type: type, user_id: userId, experiment_key: key, event_id: id as string };
  if (value.metadata !== undefined) {
    const metadata = metadataFrom(value.metadata);
    if (metadata === null) return null;
    event.metadata = metadata;
  }
  return utf8Bytes(JSON.stringify(event)) <= MAX_EVENT_BYTES ? event : null;
}

/** The kept events that are still in their form and less than 24 hours old, the newest 50. */
export function readOutbox(storage: Storage | null, now: number): Entry[] {
  let parsed: unknown;
  try {
    const text = storage ? storage.getItem(KEYS.outbox) : null;
    if (text === null) return [];
    parsed = JSON.parse(text);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const entries: Entry[] = [];
  for (const item of parsed) {
    if (!isRecord(item)) continue;
    const at = item.at;
    if (typeof at !== "number" || !Number.isInteger(at) || at <= 0 || at > now || now - at >= OUTBOX_MS) continue;
    const event = eventFrom(item.event);
    if (event !== null) entries.push({ at, event });
  }
  return entries.slice(-MAX_OUTBOX);
}

/**
 * The events split into calls: each at most *maxEvents* events and at most
 * *maxBytes* of JSON (`{"events":[…]}`, in UTF-8), in order.
 */
export function chunks(events: GoalEvent[], maxEvents = MAX_CHUNK_EVENTS, maxBytes = MAX_CHUNK_BYTES): GoalEvent[][] {
  const calls: GoalEvent[][] = [];
  const empty = utf8Bytes('{"events":[]}');
  let call: GoalEvent[] = [];
  let bytes = empty;
  for (const event of events) {
    const size = utf8Bytes(JSON.stringify(event));
    const more = call.length === 0 ? size : size + 1;
    if (call.length > 0 && (call.length >= maxEvents || bytes + more > maxBytes)) {
      calls.push(call);
      call = [];
      bytes = empty;
    }
    if (empty + size > maxBytes) continue;
    bytes += call.length === 0 ? size : size + 1;
    call.push(event);
  }
  if (call.length > 0) calls.push(call);
  return calls;
}

/** What an answer means for its call's events: removed (2xx), dropped (a refusal) or kept. */
function settled(response: Response): "removed" | "dropped" | "kept" {
  if (response.ok) return "removed";
  const status = response.status;
  return status >= 400 && status < 500 && status !== 429 ? "dropped" : "kept";
}

const identity = (event: GoalEvent): string => `${event.experiment_key}\n${event.user_id}\n${event.event_id}`;

export function createOutbox(env: OutboxEnv): Outbox {
  /** The events of the calls on their way. */
  const sending = new Set<string>();
  /** The events this page removed or dropped, in case the browser refused the write. */
  const finished = new Set<string>();

  function write(storage: Storage, entries: Entry[]): void {
    if (!env.allowed()) return;
    try {
      if (entries.length > 0) storage.setItem(KEYS.outbox, JSON.stringify(entries));
      else if (storage.getItem(KEYS.outbox) !== null) storage.removeItem(KEYS.outbox);
    } catch {
      // Storage full or refused: the events are sent from this page only.
    }
  }

  /** The kept events, less those this page finished. */
  function kept(storage: Storage): GoalEvent[] {
    return readOutbox(storage, env.now())
      .map((entry) => entry.event)
      .filter((event) => !finished.has(identity(event)));
  }

  function remove(ids: string[]): void {
    for (const id of ids) finished.add(id);
    const storage = env.local();
    if (!storage || !env.allowed()) return;
    const gone = new Set(ids);
    write(storage, readOutbox(storage, env.now()).filter((entry) => !gone.has(identity(entry.event))));
  }

  /**
   * Sends *calls* one after the other: the next once the one before is
   * answered with a 2xx or a refusal, each with only the events still kept.
   * A call kept for later stops the rest until the next flush.
   */
  function sendCalls(calls: GoalEvent[][]): void {
    const storage = env.local();
    if (calls.length === 0 || !storage || !env.allowed()) return;
    const still = new Set(kept(storage).map(identity));
    const first = calls[0].filter((event) => still.has(identity(event)) && !sending.has(identity(event)));
    if (first.length === 0) return sendCalls(calls.slice(1));
    const request = env.send(env.path, { events: first }, { apiKey: true, keepalive: true });
    if (request === null) return;
    const ids = first.map(identity);
    for (const id of ids) sending.add(id);
    const settle = (outcome: "removed" | "dropped" | "kept"): void => {
      for (const id of ids) sending.delete(id);
      if (outcome === "kept") return;
      remove(ids);
      sendCalls(calls.slice(1));
    };
    request
      .then(
        (response) => settle(settled(response)),
        () => settle("kept"),
      )
      .then(undefined, () => undefined);
  }

  function flush(): void {
    if (!env.allowed()) return;
    const storage = env.local();
    if (!storage) return;
    sendCalls(chunks(kept(storage).filter((event) => !sending.has(identity(event)))));
  }

  return {
    add(events) {
      if (events.length === 0 || !env.allowed()) return;
      const storage = env.local();
      if (!storage) return;
      const now = env.now();
      const entries = readOutbox(storage, now).filter((entry) => !finished.has(identity(entry.event)));
      for (const event of events) entries.push({ at: now, event });
      write(storage, entries.slice(-MAX_OUTBOX));
      flush();
    },
    flush,
  };
}
