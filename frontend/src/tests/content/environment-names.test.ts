/**
 * The dashboard never calls an environment "stage".
 *
 * The flag environments are Production, Staging and Development; "stage" is
 * a rollout schedule's step ("Start stage", "stage 2", "Minimum stage
 * duration"). A string that uses "stage" for the environment ("Promote to
 * stage", `env=stage`, `'stage'` as a value, "the stage environment") reads
 * as one when it is the other, so this scans every dashboard source file
 * (`frontend/src`, and `modules/frontend/src` in a full run; tests and mocks
 * excluded) for those shapes. Rollout stages do not match: they are a stage
 * with a number, a name or a property after it, never one of the shapes
 * below.
 *
 * The patterns are checked against planted offenders and real rollout-stage
 * strings first, so a pattern that stops matching fails here rather than
 * passing the scan for ever.
 */
import fs from 'fs';
import { PROFILE, moduleSourcePrefixes, profileSourceFiles, sourceLabel } from '../helpers/api-literals';

/** Each shape of "stage" used as an environment name. Case-insensitive. */
export const STAGE_AS_ENVIRONMENT: RegExp[] = [
  // "Promote to stage", "in stage", "from stage" -- not "to stage 3", "in stage ${n}",
  // "stage's", or a rollout's "the stage" ("advance only on the stage the schedule is at").
  /\b(?:to|in|into|from)\s+stage\b(?!\s*(?:\d|\$\{|\{|#|["“'’(])|['’]s\b|_|-)/i,
  // 'stage' or "stage" or `stage` as a whole value.
  /(['"`])stage\1/i,
  // env=stage, environment: stage, env stage.
  /\benv(?:ironment)?s?\b\s*[:=]?\s*['"`]?stage\b/i,
  // A route's environment segment: /environments/stage.
  /\/environments?\/stage\b/i,
  // "the stage environment", "a stage key", "stage install".
  /\bstage\s+(?:environment|env|key|keys|install|instance|deployment|server)\b/i,
  // "dev, stage and prod", "prod/stage".
  /\b(?:prod(?:uction)?|dev(?:elopment)?)\s*(?:,|\/|\band\b|\bor\b)\s*stage\b/i,
  /\bstage\s*(?:,|\/|\band\b|\bor\b)\s*(?:prod(?:uction)?|dev(?:elopment)?)\b/i,
];

export function stageAsEnvironment(line: string): boolean {
  return STAGE_AS_ENVIRONMENT.some((re) => re.test(line));
}

describe(`no dashboard string calls an environment "stage" (${PROFILE})`, () => {
  describe('the patterns', () => {
    it.each([
      'Promote to stage',
      'promote this flag into stage',
      'Configured in stage',
      "const environment = 'stage';",
      'apiFetch(`/api/v1/feature-flags/${id}/environments/stage`)',
      '?env=stage',
      'environment: "stage"',
      'Create a stage key',
      'Turn it off in the stage environment',
      'dev, stage and prod',
      'prod/stage',
      'Stage or production',
    ])('fire on the planted offender %p', (line) => {
      expect(stageAsEnvironment(line)).toBe(true);
    });

    it.each([
      'Start stage',
      'Complete stage',
      '+ Add stage',
      'Minimum stage duration (hours)',
      'Stage ${n} is time-based but follows a manual stage',
      'Give stage ${n} a name.',
      'sets the flag to 50% in stage 2',
      'Complete “Half” now? This completes the stage',
      'the next stage that starts sets this percentage again',
      'No stage starts or completes until you resume',
      'Rollout Stage Advanced',
      'data-testid="rollout-stage-remove"',
      'Staging',
      'Promote to Staging',
      'stage_order',
      'a stage\'s target',
      'current stage in progress',
      'advance only on the stage the schedule is at',
      'Advance to the stage after it',
    ])('stay quiet on the rollout stage or environment string %p', (line) => {
      expect(stageAsEnvironment(line)).toBe(false);
    });
  });

  it('finds no such string in the dashboard sources', () => {
    const files = profileSourceFiles(PROFILE, moduleSourcePrefixes());
    expect(files.length).toBeGreaterThan(20);
    const hits: string[] = [];
    for (const file of files) {
      fs.readFileSync(file, 'utf8')
        .split('\n')
        .forEach((line, i) => {
          if (stageAsEnvironment(line)) hits.push(`${sourceLabel(file)}:${i + 1}  ${line.trim()}`);
        });
    }
    expect(hits).toEqual([]);
  });
});
