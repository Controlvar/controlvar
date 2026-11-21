/**
 * The page-change format the script applies (format 1), as the server
 * defines it. Every list and limit here is pinned equal to
 * tests/sdk-contract/visual-changes-vectors.json, which is generated from the
 * server's validator, by __tests__/validate.test.ts: a server change that is
 * not made here fails that test.
 */
export const FORMAT_VERSION = 1;

export const OPS = ["set_text", "set_attribute", "set_image", "hide", "set_style"] as const;

export const ALLOWED_ATTRIBUTES = ["alt", "title", "aria-label", "placeholder", "href"] as const;

export const MARGIN_PROPERTIES = [
  "margin",
  "margin-block",
  "margin-block-end",
  "margin-block-start",
  "margin-bottom",
  "margin-inline",
  "margin-inline-end",
  "margin-inline-start",
  "margin-left",
  "margin-right",
  "margin-top",
] as const;

export const ALLOWED_STYLE_PROPERTIES = [
  "align-items",
  "background-color",
  "border",
  "border-color",
  "border-radius",
  "border-style",
  "border-width",
  "color",
  "display",
  "flex-direction",
  "font-family",
  "font-size",
  "font-style",
  "font-weight",
  "gap",
  "height",
  "justify-content",
  "letter-spacing",
  "line-height",
  "margin",
  "margin-block",
  "margin-block-end",
  "margin-block-start",
  "margin-bottom",
  "margin-inline",
  "margin-inline-end",
  "margin-inline-start",
  "margin-left",
  "margin-right",
  "margin-top",
  "max-height",
  "max-width",
  "min-height",
  "min-width",
  "opacity",
  "padding",
  "padding-block",
  "padding-block-end",
  "padding-block-start",
  "padding-bottom",
  "padding-inline",
  "padding-inline-end",
  "padding-inline-start",
  "padding-left",
  "padding-right",
  "padding-top",
  "text-align",
  "text-decoration",
  "text-transform",
  "visibility",
  "width",
] as const;

export const ALLOWED_STYLE_FUNCTIONS = ["calc", "clamp", "hsl", "hsla", "max", "min", "rgb", "rgba", "var"] as const;

export const LIMITS = {
  alt: 500,
  attribute_value: 2048,
  bytes: 65536,
  changes: 100,
  selector: 512,
  style_value: 200,
  styles: 20,
  text: 2000,
} as const;

/** The flex directions a change may set (none reverses the visual order). */
export const FLEX_DIRECTIONS = ["row", "column"] as const;
