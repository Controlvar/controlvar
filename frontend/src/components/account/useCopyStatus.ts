import { useCallback, useState } from 'react';

export const COPIED = 'Copied.';
export const COPY_UNAVAILABLE = 'Copying is not available here. Select the text and copy it.';
export const DOWNLOAD_UNAVAILABLE = 'Downloading is not available here. Copy the codes instead.';

/**
 * Copying to the clipboard, and the one-line status it leaves ("Copied.", or
 * how to do it by hand), for the two-step sign-in screens: the account page
 * and the set-up a sign-in asks for (#1812).
 */
export function useCopyStatus(): {
  status: string;
  setStatus: (text: string) => void;
  copy: (text: string) => void;
} {
  const [status, setStatus] = useState('');
  const copy = useCallback((text: string) => {
    const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
    if (!clipboard) {
      setStatus(COPY_UNAVAILABLE);
      return;
    }
    clipboard.writeText(text).then(
      () => setStatus(COPIED),
      () => setStatus(COPY_UNAVAILABLE),
    );
  }, []);
  return { status, setStatus, copy };
}
