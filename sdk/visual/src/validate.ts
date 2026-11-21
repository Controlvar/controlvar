/**
 * Checks a variant's page changes exactly as the server's validator does
 * (backend/app/schemas/visual.py), before any of them is applied. The server
 * has already checked them; this is the second check, so a change the
 * server would refuse is never applied, whatever the answer holds. An
 * experiment with one change that fails is skipped whole.
 */
import {
  ALLOWED_ATTRIBUTES,
  ALLOWED_STYLE_FUNCTIONS,
  ALLOWED_STYLE_PROPERTIES,
  FLEX_DIRECTIONS,
  FORMAT_VERSION,
  LIMITS,
  MARGIN_PROPERTIES,
  OPS,
} from "./rules";
import { codePoints, linkProblem, SPACE } from "./url";

export type Op = (typeof OPS)[number];

export interface SetText {
  op: "set_text";
  selector: string;
  text: string;
}
export interface SetAttribute {
  op: "set_attribute";
  selector: string;
  name: (typeof ALLOWED_ATTRIBUTES)[number];
  value: string;
}
export interface SetImage {
  op: "set_image";
  selector: string;
  src: string;
  alt: string | null;
}
export interface Hide {
  op: "hide";
  selector: string;
}
export interface SetStyle {
  op: "set_style";
  selector: string;
  styles: Record<string, string>;
}
export type Change = SetText | SetAttribute | SetImage | Hide | SetStyle;

/** Why an experiment's changes were refused: an operation this script does not know, or anything else. */
export type Refusal = "unsupported_operation" | "invalid";

export type Verdict = { ok: true; changes: Change[] } | { ok: false; reason: Refusal };

const FIELDS: Record<Op, { required: string[]; optional: string[] }> = {
  set_text: { required: ["text"], optional: [] },
  set_attribute: { required: ["name", "value"], optional: [] },
  set_image: { required: ["src"], optional: ["alt"] },
  hide: { required: [], optional: [] },
  set_style: { required: ["styles"], optional: [] },
};

const CONTROL = /[\x00-\x1f\x7f-\x9f]/;
const SELECTOR_FORBIDDEN = /[{};\\@<]/;
const STYLE_FORBIDDEN = /[;{}<>\\@!]/;
const STYLE_FORBIDDEN_WORDS = ["expression", "-moz-binding", "behavior", "javascript"];
const FUNCTION = new RegExp(`([A-Za-z_-][A-Za-z0-9_-]{0,63})[${SPACE}]{0,8}\\(`, "g");
const MARGIN_TOKEN = /^(?:0|auto|(?:[0-9]{1,6}(?:\.[0-9]{1,6})?|\.[0-9]{1,6})(?:px|em|rem|%))$/i;
const SPACES = new RegExp(`[${SPACE}]+`);
const TRIM = new RegExp(`^[${SPACE}]+|[${SPACE}]+$`, "g");

const hasOwn = (target: object, key: string): boolean => Object.prototype.hasOwnProperty.call(target, key);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function includes<T extends string>(list: readonly T[], value: unknown): value is T {
  return typeof value === "string" && (list as readonly string[]).indexOf(value) >= 0;
}

function hasLoneSurrogate(value: string): boolean {
  for (const character of value) {
    const code = character.charCodeAt(0);
    if (character.length === 1 && code >= 0xd800 && code <= 0xdfff) return true;
  }
  return false;
}

/** Whether the server's selector_problem refuses *selector*. */
export function selectorProblem(selector: unknown): boolean {
  if (typeof selector !== "string") return true;
  const length = codePoints(selector);
  if (length < 1 || length > LIMITS.selector) return true;
  if (SELECTOR_FORBIDDEN.test(selector)) return true;
  if (selector.indexOf("/*") >= 0 || selector.indexOf("*/") >= 0) return true;
  return CONTROL.test(selector) || hasLoneSurrogate(selector);
}

/** Whether the server's style_problem refuses one set_style entry. */
export function styleProblem(property: string, value: unknown): boolean {
  if (!includes(ALLOWED_STYLE_PROPERTIES, property)) return true;
  if (typeof value !== "string") return true;
  const length = codePoints(value);
  if (length < 1 || length > LIMITS.style_value) return true;
  if (CONTROL.test(value) || value.indexOf("/*") >= 0 || value.indexOf("*/") >= 0) return true;
  if (STYLE_FORBIDDEN.test(value)) return true;
  const lowered = value.toLowerCase();
  if (STYLE_FORBIDDEN_WORDS.some((word) => lowered.indexOf(word) >= 0)) return true;
  FUNCTION.lastIndex = 0;
  for (let match = FUNCTION.exec(value); match !== null; match = FUNCTION.exec(value)) {
    if (!includes(ALLOWED_STYLE_FUNCTIONS, match[1].toLowerCase())) return true;
  }
  if (includes(MARGIN_PROPERTIES, property)) {
    const tokens = value.split(SPACES).filter((token) => token !== "");
    if (tokens.length < 1 || tokens.length > 4 || value.indexOf("(") >= 0) return true;
    if (!tokens.every((token) => MARGIN_TOKEN.test(token))) return true;
  }
  if (property === "flex-direction" && !includes(FLEX_DIRECTIONS, value.replace(TRIM, "").toLowerCase())) {
    return true;
  }
  return false;
}

function stringWithin(value: unknown, limit: number): value is string {
  return typeof value === "string" && codePoints(value) <= limit;
}

/** One change, or the reason it is refused. */
function checkChange(raw: unknown): Change | Refusal {
  if (!isRecord(raw)) return "invalid";
  const op = raw.op;
  if (typeof op !== "string") return "invalid";
  if (!includes(OPS, op)) return "unsupported_operation";
  const fields = FIELDS[op];
  const known = ["op", "selector", ...fields.required, ...fields.optional];
  for (const key of Object.keys(raw)) if (known.indexOf(key) < 0) return "invalid";
  for (const key of ["selector", ...fields.required]) if (!hasOwn(raw, key)) return "invalid";
  if (selectorProblem(raw.selector)) return "invalid";
  const selector = raw.selector as string;
  switch (op) {
    case "set_text": {
      const text = raw.text;
      if (!stringWithin(text, LIMITS.text) || text.indexOf("\x00") >= 0) return "invalid";
      return { op, selector, text };
    }
    case "set_attribute": {
      const { name, value } = raw;
      if (!includes(ALLOWED_ATTRIBUTES, name)) return "invalid";
      if (!stringWithin(value, LIMITS.attribute_value) || CONTROL.test(value)) return "invalid";
      if (name === "href" && linkProblem(value)) return "invalid";
      return { op, selector, name, value };
    }
    case "set_image": {
      const src = raw.src;
      const alt = hasOwn(raw, "alt") ? raw.alt : null;
      if (!stringWithin(src, LIMITS.attribute_value) || linkProblem(src)) return "invalid";
      if (alt !== null && (!stringWithin(alt, LIMITS.alt) || CONTROL.test(alt))) return "invalid";
      return { op, selector, src, alt };
    }
    case "hide":
      return { op, selector };
    case "set_style": {
      const styles = raw.styles;
      if (!isRecord(styles)) return "invalid";
      const properties = Object.keys(styles);
      if (properties.length < 1 || properties.length > LIMITS.styles) return "invalid";
      const copy: Record<string, string> = {};
      for (const property of properties) {
        if (styleProblem(property, styles[property])) return "invalid";
        copy[property] = styles[property] as string;
      }
      return { op, selector, styles: copy };
    }
    default:
      return "invalid";
  }
}

/** How many bytes *value* takes as compact UTF-8 JSON, as the server counts it. */
export function storedSize(value: unknown): number {
  let bytes = 0;
  for (const character of JSON.stringify(value)) {
    const code = character.codePointAt(0) as number;
    bytes += code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
  }
  return bytes;
}

/** The server's model_dump of one change: set_image always carries `alt`. */
function dumped(change: Change): Change {
  return change.op === "set_image" ? { op: change.op, selector: change.selector, src: change.src, alt: change.alt } : change;
}

/**
 * Validates `{"version": 1, "changes": [...]}`. The answer holds copies made
 * here, so nothing the page or the answer changes later reaches the runtime.
 */
export function validateChanges(document: unknown): Verdict {
  if (!isRecord(document)) return { ok: false, reason: "invalid" };
  for (const key of Object.keys(document)) {
    if (key !== "version" && key !== "changes") return { ok: false, reason: "invalid" };
  }
  const version = document.version;
  if (typeof version !== "number" || version !== FORMAT_VERSION) return { ok: false, reason: "invalid" };
  const list = document.changes;
  if (!Array.isArray(list) || list.length > LIMITS.changes) return { ok: false, reason: "invalid" };
  const changes: Change[] = [];
  for (const raw of list) {
    const checked = checkChange(raw);
    if (typeof checked === "string") return { ok: false, reason: checked };
    changes.push(checked);
  }
  if (storedSize({ version, changes: changes.map(dumped) }) > LIMITS.bytes) return { ok: false, reason: "invalid" };
  return { ok: true, changes };
}
