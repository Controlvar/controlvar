import type { Page } from "@playwright/test";
import {
  claimSettled,
  claimsIn,
  EDITOR,
  flushToFrame,
  FRAME_SELECTOR,
  frameAt,
  html,
  newTicket,
  routeSite,
  sayHello,
  SECOND,
  SITE,
  snippet,
} from "./editor";
import { expect, test } from "./runtime.fixture";

/**
 * A page whose own script, right after the snippet, wraps browser functions
 * and records whatever passes through them. The editor link's values must
 * reach only the panel's window, once.
 */

const WRAP = `(function () {
  var seen = (window.__seen = []);
  var flags = (window.__flags = []);
  var RA = Reflect.apply;
  var slice = Array.prototype.slice;
  var stringify = JSON.stringify;
  var protoOf = Object.getPrototypeOf;
  var plain = Object.prototype;
  var getDesc = Object.getOwnPropertyDescriptor;
  var define = Object.defineProperty;
  var CLAIM = "/visual-editor/tickets/claim";
  function describe(value) {
    try {
      if (typeof value === "string") return value;
      return stringify(value, function (key, v) {
        if (typeof v === "function") return "[function]";
        if (v && typeof v === "object" && !Array.isArray(v) && protoOf(v) !== plain && protoOf(v) !== null) return "[object]";
        return v;
      });
    } catch (e) {
      return "[unserialisable]";
    }
  }
  function record(where, values) {
    for (var i = 0; i < values.length; i++) seen[seen.length] = where + " " + describe(values[i]);
  }
  function isClaimResponse(value) {
    try {
      return value instanceof Response && String(value.url).indexOf(CLAIM) >= 0;
    } catch (e) {
      return false;
    }
  }

  var frameWindow = getDesc(HTMLIFrameElement.prototype, "contentWindow");
  define(HTMLIFrameElement.prototype, "contentWindow", {
    configurable: true,
    get: function () {
      var real = RA(frameWindow.get, this, []);
      if (real === null) return null;
      return {
        postMessage: function (message, target) {
          record("contentWindow.postMessage", [message, target]);
          return real.postMessage(message, target);
        },
      };
    },
  });

  var listen = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, listener, options) {
    record("addEventListener", [type]);
    var wrapped = listener;
    if (typeof listener === "function") {
      wrapped = function (event) {
        if (event && event.type === "message") record("listener", [event.data]);
        return RA(listener, this, [event]);
      };
    }
    return RA(listen, this, [type, wrapped, options]);
  };

  var call = Function.prototype.call;
  var apply = Function.prototype.apply;
  var bind = Function.prototype.bind;
  Function.prototype.call = function () {
    record("Function.prototype.call", RA(slice, arguments, []));
    return RA(this, arguments[0], RA(slice, arguments, [1]));
  };
  Function.prototype.apply = function (thisArg, args) {
    record("Function.prototype.apply", [thisArg, args]);
    return RA(this, thisArg, args || []);
  };
  Function.prototype.bind = function () {
    record("Function.prototype.bind", RA(slice, arguments, []));
    return RA(bind, this, RA(slice, arguments, []));
  };
  Reflect.apply = function (target, thisArg, args) {
    record("Reflect.apply", [thisArg, args]);
    return RA(target, thisArg, args);
  };

  ["data", "origin", "source"].forEach(function (name) {
    var descriptor = getDesc(MessageEvent.prototype, name);
    define(MessageEvent.prototype, name, {
      configurable: true,
      get: function () {
        var value = RA(descriptor.get, this, []);
        record("MessageEvent." + name, [value]);
        return value;
      },
    });
  });

  var fetchBefore = window.fetch;
  window.fetch = function (input, init) {
    record("fetch", [String(input), init]);
    return RA(fetchBefore, window, [input, init]);
  };
  ["json", "text"].forEach(function (name) {
    var method = Response.prototype[name];
    Response.prototype[name] = function () {
      record("Response." + name, [this.url]);
      if (isClaimResponse(this)) flags[flags.length] = "Response.prototype." + name + " called on the claim's response";
      return RA(method, this, []);
    };
  });
  var parse = JSON.parse;
  JSON.parse = function (text, reviver) {
    record("JSON.parse", [text]);
    return RA(parse, JSON, [text, reviver]);
  };
  var then = Promise.prototype.then;
  Promise.prototype.then = function (onFulfilled, onRejected) {
    var wrapped = onFulfilled;
    if (typeof onFulfilled === "function") {
      wrapped = function (value) {
        record("Promise.prototype.then", [value]);
        if (isClaimResponse(value)) flags[flags.length] = "Promise.prototype.then saw the claim's response";
        return RA(onFulfilled, undefined, [value]);
      };
    }
    return RA(then, this, [wrapped, onRejected]);
  };

  window.addEventListener("message", function (event) {
    record("page message", [event.data, event.origin]);
  });
})();`;

const PAGE = html(`${snippet()}<script>${WRAP}</script>`, "<p>page</p>");

interface Records {
  seen: string[];
  flags: string[];
}

async function records(page: Page): Promise<Records> {
  return page.evaluate(() => {
    const w = window as unknown as Records & { __seen: string[]; __flags: string[] };
    return { seen: w.__seen.slice(), flags: w.__flags.slice() };
  });
}

/** Every place on the page a value could be left: attributes and markup, frame src, both storages, cookies. */
async function leftOnPage(page: Page): Promise<string> {
  const inPage = await page.evaluate((selector) => {
    const parts = [document.documentElement.outerHTML, document.querySelector(selector)?.getAttribute("src") ?? ""];
    for (const storage of [localStorage, sessionStorage]) {
      for (let i = 0; i < storage.length; i += 1) {
        const key = storage.key(i) ?? "";
        parts.push(key, storage.getItem(key) ?? "");
      }
    }
    parts.push(document.cookie);
    return parts.join("\n");
  }, FRAME_SELECTOR);
  return inPage;
}

function expectNothingHolds(found: Records, left: string, ticket: string, claim: string): void {
  expect(found.flags, "calls on the claim's response").toEqual([]);
  for (const [name, value] of [
    ["ticket", ticket],
    ["claim", claim],
  ] as const) {
    const hits = found.seen.filter((entry) => entry.includes(value));
    expect(hits, `wrapped functions that saw the ${name}`).toEqual([]);
    expect(left.includes(value), `the page holds the ${name}`).toBe(false);
  }
  expect(found.seen.length, "the wrappers recorded something").toBeGreaterThan(0);
}

test("a page that wraps browser functions after the snippet sees nothing of the editor link", async ({
  page,
  visualPackage,
}) => {
  const ticket = newTicket();
  const site = await routeSite(page, visualPackage, { pages: { "/a/b": PAGE } });
  await page.goto(`${SITE}/a/b#controlvar-editor=${ticket}`, { waitUntil: "load" });
  const panel = await frameAt(page, EDITOR);
  await sayHello(panel);
  await expect.poll(async () => (await claimsIn(panel)).length, { message: "claims in the panel" }).toBe(1);
  const claim = (site.api[0].body as { claim: string }).claim;
  expect((await claimsIn(panel))[0].data?.claim).toBe(claim);

  expectNothingHolds(await records(page), await leftOnPage(page), ticket, claim);
});

test("when the panel frame moves to another origin after hello, that origin gets no claim", async ({
  page,
  visualPackage,
}) => {
  const ticket = newTicket();
  const site = await routeSite(page, visualPackage, { pages: { "/a/b": PAGE }, holdClaim: true });
  await page.goto(`${SITE}/a/b#controlvar-editor=${ticket}`, { waitUntil: "load" });
  const panel = await frameAt(page, EDITOR);
  await sayHello(panel);
  await panel.evaluate((url) => {
    location.href = url;
  }, `${SECOND}/second`);
  const second = await frameAt(page, SECOND);
  await sayHello(second);

  site.releaseClaim();
  await claimSettled(page);
  await flushToFrame(page, second);
  expect(
    (await claimsIn(second)).map((message) => message.origin),
    `${new URL(SECOND).host} got the claim`,
  ).toEqual([]);
  const claim = (site.api[0].body as { claim: string }).claim;
  expectNothingHolds(await records(page), await leftOnPage(page), ticket, claim);
});
