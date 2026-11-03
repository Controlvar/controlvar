/**
 * The consumer check's child runner: a child that exits before it reads its
 * input must give a clean result (its exit code and stderr), never an
 * unhandled `write EPIPE` that crashes the whole check.
 */

import { join } from "node:path";

// eslint-disable-next-line @typescript-eslint/no-require-imports
const { run } = require(join(__dirname, "..", "scripts", "registry-consumer-check.js")) as {
  run: (
    command: string,
    args: string[],
    options: { input?: string },
  ) => Promise<{ code: number | string | null; stdout: string; stderr: string }>;
};

describe("registry-consumer-check run()", () => {
  test("a child that exits at once without reading stdin reports its exit code and stderr", async () => {
    // Larger than any pipe buffer, so the write cannot complete before the child is gone.
    const input = "x".repeat(8 * 1024 * 1024);
    const result = await run(process.execPath, ["-e", "process.stderr.write('early-exit-canary'); process.exit(3)"], {
      input,
    });
    expect(result.code).toBe(3);
    expect(result.stderr).toBe("early-exit-canary");
  });

  test("a child that reads its input still gets it", async () => {
    const result = await run(
      process.execPath,
      ["-e", "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>process.stdout.write(s.toUpperCase()))"],
      { input: "abc\n" },
    );
    expect(result.code).toBe(0);
    expect(result.stdout).toBe("ABC\n");
  });
});
