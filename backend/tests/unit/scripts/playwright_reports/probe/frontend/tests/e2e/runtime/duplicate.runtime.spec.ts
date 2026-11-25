import { test } from "@playwright/test";

/** Two tests with one title in one file: Playwright refuses the file. */
test("one title twice", () => {});

test("one title twice", () => {});
