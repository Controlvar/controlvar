// Jest's globalSetup: builds dist/ once, from package.json's own build
// script, before any test file runs, so that test files running in parallel
// never read dist/ while another rebuilds it.
const { execFileSync } = require("child_process");
const { join } = require("path");

module.exports = () => {
  execFileSync("npm", ["run", "--silent", "build"], { cwd: join(__dirname, ".."), stdio: "pipe" });
};
