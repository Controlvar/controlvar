import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * The server's flag bucketing, md5-mod100-v1 (docs/sdk/local-evaluation.md):
 * the whole MD5 digest of "{userId}:{flagKey}" read as a big-endian integer,
 * mod 100. A user is in a rollout of p percent when the bucket is below p.
 *
 * Written here from the documented rule, not imported from the server or an
 * SDK, and proven against the golden vectors before a day uses it.
 */
export function bucket(userId: string, flagKey: string): number {
  const hex = createHash("md5").update(`${userId}:${flagKey}`, "utf8").digest("hex");
  let rest = 0;
  for (const digit of hex) rest = (rest * 16 + parseInt(digit, 16)) % 100;
  return rest;
}

interface RulesetVectors {
  bucketing: string;
  ruleset: { flags: Array<{ key: string; active: boolean; rollout_percentage: number; rules: unknown[] }> };
  cases: Array<{ flag: string; user_id: string; expected: { enabled: boolean; reason: string } }>;
}

const VECTORS = join(__dirname, "..", "..", "..", "..", "tests", "sdk-contract", "ruleset-vectors.json");

/**
 * Every golden case the rollout decides (reason `rollout`, a flag between 0%
 * and 100%) answered by `bucket`: how many there are, and how many disagree.
 * The vectors are generated from the server (tests/sdk-contract/README.md).
 */
export function goldenRolloutCases(): { cases: number; disagree: number } {
  const vectors = JSON.parse(readFileSync(VECTORS, "utf8")) as RulesetVectors;
  if (vectors.bucketing !== "md5-mod100-v1") throw new Error(`bucketing is ${vectors.bucketing}`);
  const percent = new Map(vectors.ruleset.flags.map((f) => [f.key, f.rollout_percentage]));
  let cases = 0;
  let disagree = 0;
  for (const c of vectors.cases) {
    const p = percent.get(c.flag);
    if (c.expected.reason !== "rollout" || p === undefined || p <= 0 || p >= 100) continue;
    cases += 1;
    if (bucket(c.user_id, c.flag) < p !== c.expected.enabled) disagree += 1;
  }
  return { cases, disagree };
}
