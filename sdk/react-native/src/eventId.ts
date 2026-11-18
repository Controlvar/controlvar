/**
 * The id an event is sent with (`event_id`), so the server stores an event sent again once.
 *
 * A lowercase UUID v4: `crypto.randomUUID()` where the runtime has it, else
 * `crypto.getRandomValues()`, else `Math.random()` (Hermes has no `crypto` unless the app
 * installs a polyfill; the id only has to differ between events, it is not a secret).
 */
type CryptoLike = {
  randomUUID?: () => string;
  getRandomValues?: (bytes: Uint8Array) => Uint8Array;
};

export function newEventId(): string {
  const webCrypto = (globalThis as { crypto?: CryptoLike }).crypto;
  if (webCrypto && typeof webCrypto.randomUUID === 'function') {
    return webCrypto.randomUUID().toLowerCase();
  }
  const bytes = new Uint8Array(16);
  if (webCrypto && typeof webCrypto.getRandomValues === 'function') {
    webCrypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** The caller's own id when it gave one (`undefined`, `null` and `''` mean none), else a new one. */
export function eventIdFor(callerId: string | null | undefined): string {
  return callerId !== undefined && callerId !== null && callerId !== '' ? callerId : newEventId();
}
