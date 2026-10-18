import React, { useState } from 'react';
import { useExperiment } from '@controlvar/react-sdk';
import { EXPERIMENT_KEYS } from '@/lib/env';
import { useScreenView, useTrack } from '@/lib/eventLog';
import { Binding, ScreenHeader } from '@/components/ui';

const FIVE_STEPS = [
  { title: 'Welcome to StreamPulse', body: 'Music and podcasts, tuned to you.' },
  { title: 'Pick three genres', body: 'Synthwave, ambient, rock, pop, world…' },
  { title: 'Follow some artists', body: 'We will tell you when they release.' },
  { title: 'Connect your speakers', body: 'Cast to any room.' },
  { title: 'Turn on notifications', body: 'Weekly mixes and new releases.' },
];

const THREE_STEPS = [
  { title: 'Welcome to StreamPulse', body: 'Music and podcasts, tuned to you.' },
  { title: 'Pick three genres', body: 'That is all we need to start.' },
  { title: 'Turn on notifications', body: 'Weekly mixes and new releases.' },
];

/**
 * Onboarding. `streampulse_onboarding_steps` chooses the 5-step (control) or
 * 3-step flow. Finishing sends `onboarding_complete {steps}`; the first play
 * afterwards sends `first_play`. The experiment is analysed with the Bayesian
 * engine (probability-to-be-best, expected loss) so it can stop early.
 */
export default function OnboardingScreen() {
  useScreenView('onboarding');
  const track = useTrack();
  const assignment = useExperiment(EXPERIMENT_KEYS.onboardingSteps);
  const steps = assignment.configuration?.steps === 3 ? 3 : 5;
  const flow = steps === 3 ? THREE_STEPS : FIVE_STEPS;
  const [index, setIndex] = useState(0);
  const [done, setDone] = useState(false);
  const [played, setPlayed] = useState(false);

  const next = () => {
    if (index < flow.length - 1) {
      setIndex(index + 1);
      return;
    }
    setDone(true);
    track('onboarding_complete', { steps }, { experimentKey: EXPERIMENT_KEYS.onboardingSteps });
  };
  const firstPlay = () => {
    setPlayed(true);
    track('first_play', { steps }, { experimentKey: EXPERIMENT_KEYS.onboardingSteps });
  };
  const restart = () => {
    setIndex(0);
    setDone(false);
    setPlayed(false);
  };

  return (
    <section aria-labelledby="onboarding-title" data-testid="onboarding" data-steps={steps}>
      <ScreenHeader id="onboarding-title" title="Get started" subtitle={`${steps}-step setup`} badge={<Binding>{EXPERIMENT_KEYS.onboardingSteps}</Binding>} />

      <ol className="flex items-center gap-1" aria-label="Progress">
        {flow.map((s, i) => (
          <li
            key={s.title}
            className={`h-1.5 flex-1 rounded-full ${i < index || done ? 'bg-pulse-500' : i === index ? 'bg-pulse-300' : 'bg-white/15'}`}
            aria-current={!done && i === index ? 'step' : undefined}
          >
            <span className="sr-only">
              Step {i + 1} of {flow.length}
            </span>
          </li>
        ))}
      </ol>

      {done ? (
        <div className="mt-6 rounded-2xl bg-white/5 p-4 text-center" role="status">
          <div className="text-2xl">🎉</div>
          <h3 className="mt-1 text-lg font-bold">You&apos;re all set</h3>
          <p className="text-xs text-neutral-400">Onboarding complete in {steps} steps.</p>
          <button type="button" className="btn-phone-primary mt-4 w-full" onClick={firstPlay} disabled={played}>
            {played ? 'Playing ▶' : 'Play your first track'}
          </button>
          <button type="button" className="btn-phone mt-2 w-full" onClick={restart}>
            Restart onboarding
          </button>
        </div>
      ) : (
        <div className="mt-6 rounded-2xl bg-white/5 p-4">
          <p className="text-xs uppercase tracking-wide text-neutral-400" data-testid="onboarding-progress">
            Step {index + 1} of {steps}
          </p>
          <h3 className="mt-1 text-lg font-bold">{flow[index].title}</h3>
          <p className="mt-1 text-sm text-neutral-300">{flow[index].body}</p>
          <button type="button" className="btn-phone-primary mt-4 w-full" onClick={next}>
            {index === flow.length - 1 ? 'Finish' : 'Next'}
          </button>
          {index > 0 && (
            <button type="button" className="btn-phone mt-2 w-full" onClick={() => setIndex(index - 1)}>
              Back
            </button>
          )}
        </div>
      )}
    </section>
  );
}
