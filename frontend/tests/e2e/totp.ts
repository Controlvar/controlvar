import { createHmac } from "node:crypto";
import { type Page } from "@playwright/test";

/**
 * The authenticator app, for the two-step sign-in journeys (#1812): RFC 6238
 * over the key the page shows as text, and what the API accepts.
 *
 * Codes: the API accepts a code once, and a code of a step no later than the
 * last one it accepted never (the replay guard), so each code is computed for
 * the step after the last one used, which the one-step window accepts.
 */

function base32Decode(text: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const char of text.replace(/=+$/, "").toUpperCase()) {
    const index = alphabet.indexOf(char);
    if (index < 0) throw new Error(`not base32: ${char}`);
    value = (value << 5) | index;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

/** RFC 6238, HMAC-SHA1, 6 digits, the code of 30-second step `step`. */
export function totp(secret: string, step: number): string {
  const counter = Buffer.alloc(8);
  counter.writeUInt32BE(Math.floor(step / 2 ** 32), 0);
  counter.writeUInt32BE(step >>> 0, 4);
  const digest = createHmac("sha1", base32Decode(secret)).update(counter).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const number = digest.readUInt32BE(offset) & 0x7fffffff;
  return String(number % 1_000_000).padStart(6, "0");
}

export function currentStep(): number {
  return Math.floor(Date.now() / 30_000);
}

/** The code for the first step after `lastStep` that the API still accepts. */
export async function nextCode(
  page: Page,
  secret: string,
  lastStep: number,
): Promise<{ code: string; step: number }> {
  for (;;) {
    const now = currentStep();
    const step = Math.max(now, lastStep + 1);
    if (step <= now + 1) return { code: totp(secret, step), step };
    await page.waitForTimeout(1_000);
  }
}

/** Everything in both storages and the URL, for "the mfa_token is nowhere". */
export async function storedAndUrl(page: Page): Promise<string> {
  const stored = await page.evaluate(() => {
    const values: string[] = [];
    for (const storage of [window.localStorage, window.sessionStorage]) {
      for (let i = 0; i < storage.length; i += 1) {
        const key = storage.key(i) as string;
        values.push(key, storage.getItem(key) ?? "");
      }
    }
    return values.join("\n");
  });
  return `${stored}\n${page.url()}`;
}
