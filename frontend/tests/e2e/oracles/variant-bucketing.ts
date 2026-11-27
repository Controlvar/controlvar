import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * The server's experiment bucketing (docs/architecture/technical-guide.md,
 * "Assignment Algorithm"): the first four bytes of MD5("{userId}:{key}"),
 * read as a little-endian unsigned integer and divided by 2^32, give a
 * position in [0, 1); times 100, rounded down, a bucket 0-99. The bucket
 * picks the variant whose share of the cumulative traffic allocation holds
 * it, in the experiment's variant order. It is not the flag rollout's
 * md5-mod100-v1 (bucketing.ts).
 *
 * Written here from the documented rule, not imported from the server or an
 * SDK, and proven against the golden hash vectors before a day uses it.
 */
export function assignmentHash(userId: string, key: string): number {
  return md5(userId, key).readUInt32LE(0) / 4294967296;
}

function md5(userId: string, key: string): Buffer {
  return createHash("md5").update(`${userId}:${key}`, "utf8").digest();
}

/** The variant a new user gets: `variants` in the order the API lists them. */
export function variantFor(
  userId: string,
  experimentKey: string,
  variants: ReadonlyArray<{ name: string; traffic_allocation: number }>,
): string {
  const bucket = Math.floor(assignmentHash(userId, experimentKey) * 100);
  let cumulative = 0;
  for (const variant of variants) {
    cumulative += variant.traffic_allocation;
    if (bucket < cumulative) return variant.name;
  }
  return variants[variants.length - 1].name;
}

interface GoldenVectors {
  algorithm: { divisor: number };
  hash_vectors: Array<{ user_id: string; flag_key: string; md5_hex: string; expected_hash: number }>;
}

const VECTORS = join(__dirname, "..", "..", "..", "..", "tests", "sdk-contract", "golden-vectors.json");

/** tests/sdk-contract/hash_contract.py's: less than one step of 2^-32 (about 2.3e-10). */
const TOLERANCE = 1e-10;

/**
 * Every golden hash vector (tests/sdk-contract/golden-vectors.json, the one
 * every SDK is held to) answered by `assignmentHash`, judged as
 * hash_contract.py judges an SDK: the MD5 digest exactly, the hash within
 * TOLERANCE (the file writes it to 16 digits). How many there are, and how
 * many disagree.
 */
export function goldenHashCases(): { cases: number; disagree: number } {
  const vectors = JSON.parse(readFileSync(VECTORS, "utf8")) as GoldenVectors;
  if (vectors.algorithm.divisor !== 4294967296) throw new Error(`divisor is ${vectors.algorithm.divisor}`);
  const disagree = vectors.hash_vectors.filter(
    (v) =>
      md5(v.user_id, v.flag_key).toString("hex") !== v.md5_hex ||
      !(Math.abs(assignmentHash(v.user_id, v.flag_key) - v.expected_hash) < TOLERANCE),
  );
  return { cases: vectors.hash_vectors.length, disagree: disagree.length };
}
