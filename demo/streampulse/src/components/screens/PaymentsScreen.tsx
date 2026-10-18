import React, { useState } from 'react';
import { useExperiment } from '@controlvar/react-sdk';
import { EXPERIMENT_KEYS } from '@/lib/env';
import { useScreenView, useTrack } from '@/lib/eventLog';
import { Binding, ScreenHeader } from '@/components/ui';

export type Plan = 'monthly' | 'annual';
export type UpsellModal = 'classic' | 'value';

export const PLANS: Record<Plan, { label: string; price: string; note: string }> = {
  monthly: { label: 'Monthly', price: '$9.99 / month', note: 'Cancel any time' },
  annual: { label: 'Annual', price: '$79.99 / year', note: 'Two months free' },
};

interface Props {
  tier: string;
}

/**
 * Payments. `streampulse_upsell_modal` chooses the classic upsell (a price list)
 * or the value modal (benefits first, savings called out). Subscribing sends
 * `subscribe {plan, modal}`. Every change to this experiment is in the audit
 * trail (who created / started it, when) — the compliance story.
 */
export default function PaymentsScreen({ tier }: Props) {
  useScreenView('payments');
  const track = useTrack();
  const assignment = useExperiment(EXPERIMENT_KEYS.upsellModal);
  const modal: UpsellModal = assignment.configuration?.modal === 'value' ? 'value' : 'classic';
  const [open, setOpen] = useState(false);
  const [plan, setPlan] = useState<Plan>('annual');
  const [subscribed, setSubscribed] = useState(false);

  const subscribe = () => {
    setSubscribed(true);
    setOpen(false);
    track('subscribe', { plan, modal }, { experimentKey: EXPERIMENT_KEYS.upsellModal });
  };

  return (
    <section aria-labelledby="payments-title" data-testid="payments" data-modal={modal}>
      <ScreenHeader id="payments-title" title="Premium" subtitle="Plans and billing" badge={<Binding>{EXPERIMENT_KEYS.upsellModal}</Binding>} />

      <div className="rounded-xl bg-white/5 p-3">
        <div className="text-xs uppercase tracking-wide text-neutral-400">Current plan</div>
        <div className="mt-0.5 text-lg font-semibold" data-testid="current-plan">
          {subscribed ? `Premium · ${PLANS[plan].label}` : tier === 'premium' ? 'Premium' : 'Free with ads'}
        </div>
      </div>

      {subscribed ? (
        <div className="mt-4 rounded-xl border border-emerald-400/40 bg-emerald-500/10 p-3 text-sm" role="status">
          Welcome to Premium. Ad-free, offline, lossless.
          <button type="button" className="btn-phone mt-2 w-full" onClick={() => setSubscribed(false)}>
            Cancel subscription
          </button>
        </div>
      ) : (
        <button type="button" className="btn-phone-primary mt-4 w-full" onClick={() => setOpen(true)}>
          Go Premium
        </button>
      )}

      <ul className="mt-4 space-y-1 text-xs text-neutral-400">
        <li>Payment method: •••• 4242</li>
        <li>Billing region: derived from device</li>
      </ul>

      {open && (
        <div className="absolute inset-0 z-20 flex items-end bg-black/60 p-3" data-testid="upsell-modal" data-modal={modal}>
          <div role="dialog" aria-modal="true" aria-labelledby="upsell-title" className="w-full rounded-2xl bg-ink-800 p-4 shadow-2xl">
            {modal === 'value' ? (
              <>
                <h3 id="upsell-title" className="text-lg font-extrabold">
                  Everything you love, without the ads
                </h3>
                <ul className="mt-2 space-y-1 text-sm text-neutral-200">
                  <li>✓ Ad-free listening</li>
                  <li>✓ Offline downloads</li>
                  <li>✓ Lossless audio</li>
                  <li>✓ Unlimited skips</li>
                </ul>
                <p className="mt-2 rounded-lg bg-pulse-500/20 px-2 py-1 text-xs text-pulse-300">Annual saves 33% — that is less than a coffee a month.</p>
              </>
            ) : (
              <h3 id="upsell-title" className="text-lg font-extrabold">
                Choose a plan
              </h3>
            )}

            <fieldset className="mt-3">
              <legend className="sr-only">Plan</legend>
              <div className="grid grid-cols-2 gap-2">
                {(Object.keys(PLANS) as Plan[]).map((p) => (
                  <label
                    key={p}
                    className={`cursor-pointer rounded-xl border p-2 text-sm ${plan === p ? 'border-pulse-400 bg-pulse-500/15' : 'border-white/15'}`}
                  >
                    <input type="radio" name="plan" value={p} checked={plan === p} onChange={() => setPlan(p)} className="sr-only" />
                    <div className="font-semibold">{PLANS[p].label}</div>
                    <div className="text-xs text-neutral-300">{PLANS[p].price}</div>
                    <div className="text-[10px] text-neutral-400">{PLANS[p].note}</div>
                  </label>
                ))}
              </div>
            </fieldset>

            <button type="button" className="btn-phone-primary mt-3 w-full" onClick={subscribe}>
              Subscribe
            </button>
            <button type="button" className="btn-phone mt-2 w-full" onClick={() => setOpen(false)}>
              Not now
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
