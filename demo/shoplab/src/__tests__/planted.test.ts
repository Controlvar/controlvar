// Planted failure for a tamper run (do not merge): the demo jest job must go red.
test('a planted failure', () => {
  expect(1 + 1).toBe(3);
});
