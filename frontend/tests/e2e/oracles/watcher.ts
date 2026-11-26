import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Page } from "@playwright/test";
import { BASE_URL } from "../env";
import { writeLogLine } from "./oracle";

/**
 * The passive watcher of a persona day: on its page it notes every uncaught
 * page error, every console error and every same-origin answer of 500 or
 * more, and `step()` (the last line of each day step) scans the visible text
 * for the strings a user must never be shown. Anything noted fails that
 * step, unless it is listed in `known-noise.json` with the issue that tracks
 * it.
 *
 * A listed entry that matches writes a `noise` line to the oracle log, and
 * tests/personas/check_run.py fails a run in which a listed entry matched
 * nothing: an entry outlives its issue only until the next full run.
 */

/** Never in visible text. Word-bounded where the string is a word. */
export const NEVER_SHOWN: ReadonlyArray<[string, RegExp]> = [
  ["undefined", /\bundefined\b/],
  ["null", /\bnull\b/],
  ["NaN", /\bNaN\b/],
  ["[object Object]", /\[object Object\]/],
  ["Traceback", /\bTraceback\b/],
  ['{"detail"', /\{"detail"/],
  ["Internal Server Error", /Internal Server Error/],
  ["an HTML error page", /<!DOCTYPE html|<html[\s>]|\b50[234] Bad Gateway\b|\bBad Gateway\b/i],
];

export type NoiseKind = "pageerror" | "console" | "response" | "text";

export interface NoiseEntry {
  issue: number;
  kind: NoiseKind;
  match: string;
}

const NOISE_FILE = join(__dirname, "known-noise.json");

export function knownNoise(): NoiseEntry[] {
  const data = JSON.parse(readFileSync(NOISE_FILE, "utf8")) as { entries: NoiseEntry[] };
  return data.entries;
}

interface Noted {
  kind: NoiseKind;
  text: string;
}

export class Watcher {
  private readonly noted: Noted[] = [];
  private readonly noise = knownNoise();
  private readonly origin = new URL(BASE_URL).origin;

  constructor(private readonly page: Page) {
    page.on("pageerror", (error) => this.note("pageerror", `page error: ${error.message}`));
    page.on("console", (message) => {
      if (message.type() === "error") this.note("console", `console error: ${message.text()}`);
    });
    page.on("response", (response) => {
      const url = new URL(response.url());
      if (url.origin === this.origin && response.status() >= 500) {
        this.note("response", `${response.status()} from ${response.request().method()} ${url.pathname}`);
      }
    });
  }

  private note(kind: NoiseKind, text: string): void {
    const entry = this.noise.find((e) => e.kind === kind && text.includes(e.match));
    if (entry) {
      writeLogLine({ noise: entry.issue, kind });
      return;
    }
    this.noted.push({ kind, text });
  }

  /** Scan the visible text, then fail on anything noted since the last step. */
  async step(): Promise<void> {
    const where = new URL(this.page.url()).pathname;
    const text = await this.page.locator("body").innerText();
    for (const [name, pattern] of NEVER_SHOWN) {
      if (pattern.test(text)) this.note("text", `visible text contains ${name} on ${where}`);
    }
    const found = this.noted.splice(0);
    if (found.length > 0) {
      throw new Error(`watcher: ${found.map((n) => n.text).join("; ")}`);
    }
  }
}
