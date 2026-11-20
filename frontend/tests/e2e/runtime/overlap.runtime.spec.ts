import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "./runtime.fixture";
import { routeSite, SITE } from "./editor";
import { answering, calls, experiment, shown, sitePage, VISUAL_PATH } from "./visual";

/**
 * No accepted change lets another element take a button's clicks (#1545):
 * a button, then a link below it; each change the server accepts (every
 * accepted vector in tests/sdk-contract/visual-changes-vectors.json, and a
 * few more styles) is applied to the button or to the link, and the point at
 * the button's centre still belongs to the button. Changes the server
 * refuses, negative margins among them, never apply at all.
 *
 * Left out, as a known class: an element can still be overlapped by its own
 * container's content when a style makes the container smaller than what it
 * holds (`height: 0` and the like) or makes text invisible but clickable
 * (transparent colours). EXCLUDED lists them; the corpus holds none.
 */

const VECTORS = JSON.parse(
  readFileSync(join(__dirname, "..", "..", "..", "..", "tests", "sdk-contract", "visual-changes-vectors.json"), "utf8"),
);

const EXCLUDED = new Set([
  "height:0",
  "max-height:0",
  "line-height:0",
  "font-size:0",
  "color:transparent",
  "background-color:transparent",
]);

/** Styles the server accepts, beyond the vectors. */
const EXTRA_ACCEPTED: Record<string, string>[] = [
  { "letter-spacing": "-50px" },
  { width: "2000px", height: "300px" },
  { padding: "60px" },
  { "font-size": "80px" },
  { display: "inline" },
  { "margin-top": "0", "margin-bottom": "auto" },
];

/** Changes the server refuses: the script must not apply them either. */
const REFUSED_MARGINS: Record<string, string>[] = [{ "margin-top": "-40px" }, { margin: "-40px 0 0 0" }];

interface Change {
  op: string;
  selector: string;
  [key: string]: unknown;
}

function corpus(): { id: string; changes: Change[] }[] {
  const accepted = (VECTORS.changes as { id: string; accepted: boolean; document: { changes: Change[] } }[])
    .filter((vector) => vector.accepted && vector.document.changes.length > 0)
    .map((vector) => ({ id: vector.id, changes: vector.document.changes }));
  const extra = [...EXTRA_ACCEPTED, ...REFUSED_MARGINS].map((styles) => ({
    id: Object.entries(styles).map(([name, value]) => `${name}:${value}`).join(";"),
    changes: [{ op: "set_style", selector: "x", styles }],
  }));
  return [...accepted, ...extra];
}

// Both are inline blocks, painted in document order, so a link moved over the
// button is painted above it.
const BODY = `<div style="padding-top:100px"><button id="btn" type="button" style="display:inline-block;vertical-align:top;width:200px;height:40px">Buy now</button><br>
<a id="lnk" href="/x" style="display:inline-block;vertical-align:top;width:200px;height:40px">Another link</a></div>`;

test("the corpus leaves out the known overlap class", () => {
  const styles = corpus().flatMap((entry) =>
    entry.changes.flatMap((change) =>
      Object.entries((change.styles as Record<string, string>) ?? {}).map(([name, value]) => `${name}:${value}`.replace(/\s+/g, "")),
    ),
  );
  expect(styles.filter((style) => EXCLUDED.has(style))).toEqual([]);
  expect(corpus().length).toBeGreaterThan(30);
});

for (const target of ["#btn", "#lnk"]) {
  test(`no change applied to ${target} lets the link take the button's centre`, async ({ page, visualPackage }) => {
    const tookThePoint: string[] = [];
    for (const entry of corpus()) {
      const changes = entry.changes.map((change) => ({ ...change, selector: target }));
      const site = await routeSite(page, visualPackage, {
        pages: { "/": sitePage(BODY) },
        answer: answering({ experiments: [experiment("overlap", changes)] }),
      });
      await page.goto(`${SITE}/`);
      await expect.poll(() => calls(site, VISUAL_PATH).length).toBe(1);
      await expect.poll(() => shown(page)).toBe(true);
      const owner = await page.evaluate(() => {
        const button = document.getElementById("btn") as HTMLElement;
        if (button.getClientRects().length === 0) return "button not shown";
        const box = button.getBoundingClientRect();
        const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
        return hit && (hit === button || button.contains(hit)) ? "button" : hit ? hit.id || hit.tagName : "nothing";
      });
      if (owner !== "button" && owner !== "button not shown") tookThePoint.push(`${entry.id}: ${owner} took the point`);
      await page.unrouteAll({ behavior: "ignoreErrors" });
    }
    expect(tookThePoint).toEqual([]);
  });
}
