import { expect, test } from "@playwright/test";

/**
 * Every outcome the release evidence tells apart, without a browser: run with
 * retries 1, each test below ends as its title says.
 */

test("a top-level test passes", () => {
  expect(1).toBe(1);
});

test.describe("outer", () => {
  test("a nested test passes", () => {
    expect(1).toBe(1);
  });

  test.describe("mid", () => {
    test("b", () => {
      expect(1).toBe(1);
    });
  });
});

test("a test fails", () => {
  expect(1, "VISUAL_PACKAGE_DIR is not set").toBe(2);
});

test("a test fails on its first attempt and passes on its retry", () => {
  if (test.info().retry === 0) throw new Error("the first attempt fails");
});

test("a test marked test.fail() fails as expected", () => {
  test.fail();
  expect(1).toBe(2);
});

test("a test marked test.fail() passes", () => {
  test.fail();
  expect(1).toBe(1);
});

test.skip("a test skipped with test.skip", () => {});

test.fixme("a test marked fixme", () => {});

test("a test that skips itself on a condition", () => {
  test.skip(true, "the condition holds");
});

for (const kind of ["count", "revenue"]) {
  test(`every change applies under ${kind}, with no violation`, () => {
    expect(kind.length).toBeGreaterThan(0);
  });
}
