// The whole suite runs in America/Los_Angeles, never in UTC (#1134).
//
// CI runners are in UTC, where a time read as local and the same time read as
// UTC are the same number, so a test of "this time is shown in the viewer's
// zone" passes whether or not the code converts. A zone with a non-zero offset
// (and daylight saving, which Los Angeles has: -07:00 in summer, -08:00 in
// winter) is what makes such a test able to fail.
//
// It has to be set HERE. `process.env.TZ = ...` inside a test file changes a
// copy of the environment that jest gives each test, not the process's zone,
// so it does nothing (measured: the offset stayed 0 under `TZ=UTC`). This file
// runs in the parent process before any worker starts, and the workers inherit
// it. Set unconditionally: a `TZ=UTC npm test` must not switch it back.
module.exports = async () => {
  process.env.TZ = 'America/Los_Angeles';
};
