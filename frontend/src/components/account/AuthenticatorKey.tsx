import React, { useEffect, useState } from 'react';
import { useCopyStatus } from '@/components/account/useCopyStatus';
import { QR_ALT, TwoFactorEnrolment, groupKey, qrCodeDataUrl } from '@/services/twoFactor';

const buttonSecondary =
  'px-4 py-2 rounded-md text-sm font-medium text-slate-700 bg-white border border-slate-300 hover:bg-slate-50 disabled:opacity-60';

/**
 * The QR image of `uri`, once drawn, or null. The QR library is loaded only
 * here, so only the two-step screens fetch it. An image drawn for a link that
 * has since been replaced is never returned for the new one: a slow first
 * draw for an earlier secret can never stand in for this one.
 */
function useQrCode(uri: string): string | null {
  const [qr, setQr] = useState<{ uri: string; src: string } | null>(null);
  useEffect(() => {
    let current = true;
    qrCodeDataUrl(uri).then(
      (src) => {
        if (current) setQr({ uri, src });
      },
      // Without the QR, the key and the link still add the account.
      () => undefined,
    );
    return () => {
      current = false;
    };
  }, [uri]);
  return qr && qr.uri === uri ? qr.src : null;
}

/**
 * A new secret for an authenticator app, three ways (#1812): a QR code, the
 * key as text in groups of four with Copy, and the `otpauth://` link. The QR
 * is never the only way to add the account. Used by the account page and by
 * the set-up a sign-in asks for when a role requires two-step sign-in.
 */
export function AuthenticatorKey({ enrolment }: { enrolment: TwoFactorEnrolment }) {
  const src = useQrCode(enrolment.otpauth_uri);
  const { status, copy } = useCopyStatus();
  return (
    <>
      <p className="text-sm text-slate-600">Scan this QR code with the app, or type the key below into it.</p>
      {src && (
        <img
          src={src}
          alt={QR_ALT}
          width={192}
          height={192}
          data-testid="two-factor-qr"
          className="h-48 w-48 border border-slate-200 bg-white [image-rendering:pixelated]"
        />
      )}
      <div>
        <p className="text-sm font-medium text-slate-700">Key</p>
        <code
          data-testid="two-factor-key"
          className="mt-1 block break-all rounded-md bg-slate-100 px-3 py-2 font-mono text-sm tracking-wider text-slate-900 select-all"
        >
          {groupKey(enrolment.secret)}
        </code>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <button
            type="button"
            data-testid="two-factor-copy-key"
            onClick={() => copy(enrolment.secret)}
            className={buttonSecondary}
          >
            Copy key
          </button>
          <a
            href={enrolment.otpauth_uri}
            data-testid="two-factor-otpauth-link"
            className="text-sm font-medium text-blue-700 underline hover:text-blue-900"
          >
            Open in an authenticator app on this device
          </a>
        </div>
        <p role="status" data-testid="two-factor-copy-status" className="text-sm text-slate-600 min-h-[1.25rem]">
          {status}
        </p>
      </div>
    </>
  );
}

export default AuthenticatorKey;
