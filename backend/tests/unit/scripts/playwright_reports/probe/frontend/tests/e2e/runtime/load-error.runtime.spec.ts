import { test } from "@playwright/test";

/** A spec that throws while it loads: no test of its project runs. */
throw new Error("this spec does not load");

test("never declared", () => {});
