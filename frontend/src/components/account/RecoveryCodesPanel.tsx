import React, { useId, useState } from 'react';
import { DOWNLOAD_UNAVAILABLE, useCopyStatus } from '@/components/account/useCopyStatus';
import { groupRecoveryCode, recoveryCodesFileText } from '@/services/twoFactor';

export const CODES_SHOWN_ONCE =
  'These codes are shown once. Each signs in one time when your authenticator app is not at hand. ' +
  'Keep them somewhere safe, such as a password manager.';

const buttonPrimary =
  'px-4 py-2 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed';
const buttonSecondary =
  'px-4 py-2 rounded-md text-sm font-medium text-slate-700 bg-white border border-slate-300 hover:bg-slate-50 disabled:opacity-60';

export interface RecoveryCodesPanelProps {
  codes: string[];
  /** Called once "I have saved these codes" is ticked and Done is chosen. */
  onDone: () => void;
  /** The button's words: "Done" on the account page. */
  doneLabel?: string;
  /** While the caller is busy after Done. */
  busy?: boolean;
}

/**
 * A new set of recovery codes, shown this once (#1812): the warning, the
 * codes as an ordered list, download and copy, and Done, which waits for the
 * labelled "I have saved these codes" checkbox. Used by the account page and
 * by the set-up a sign-in asks for when a role requires two-step sign-in. The
 * caller renders the heading above it.
 */
export function RecoveryCodesPanel({ codes, onDone, doneLabel = 'Done', busy = false }: RecoveryCodesPanelProps) {
  const savedId = useId();
  const [saved, setSaved] = useState(false);
  const { status, setStatus, copy } = useCopyStatus();

  const download = () => {
    if (typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') {
      setStatus(DOWNLOAD_UNAVAILABLE);
      return;
    }
    const url = URL.createObjectURL(new Blob([recoveryCodesFileText(codes)], { type: 'text/plain' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'controlvar-recovery-codes.txt';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <p
        className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
        data-testid="recovery-codes-warning"
      >
        <strong>Important: </strong>
        {CODES_SHOWN_ONCE}
      </p>
      <ol data-testid="recovery-codes" className="grid grid-cols-1 gap-x-8 gap-y-1 sm:grid-cols-2 list-decimal pl-6">
        {codes.map((item) => (
          <li key={item} className="font-mono text-sm tracking-wider text-slate-900">
            <code>{groupRecoveryCode(item)}</code>
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-3">
        <button type="button" data-testid="recovery-codes-download" onClick={download} className={buttonSecondary}>
          Download as a text file
        </button>
        <button
          type="button"
          data-testid="recovery-codes-copy"
          onClick={() => copy(codes.map(groupRecoveryCode).join('\n'))}
          className={buttonSecondary}
        >
          Copy codes
        </button>
      </div>
      <p role="status" data-testid="two-factor-copy-status" className="text-sm text-slate-600 min-h-[1.25rem]">
        {status}
      </p>
      <div className="flex items-center gap-2">
        <input
          id={savedId}
          type="checkbox"
          data-testid="recovery-codes-saved"
          checked={saved}
          onChange={(event) => setSaved(event.target.checked)}
          className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
        />
        <label htmlFor={savedId} className="text-sm font-medium text-slate-700">
          I have saved these codes
        </label>
      </div>
      <div>
        <button
          type="button"
          data-testid="recovery-codes-done"
          onClick={() => {
            if (saved) onDone();
          }}
          disabled={!saved || busy}
          className={buttonPrimary}
        >
          {doneLabel}
        </button>
      </div>
    </>
  );
}

export default RecoveryCodesPanel;
