import { useEffect, useRef } from 'react';

// Types `text` into the composer via `setDraft`, character by character, and
// stops — it never calls submit. The Debug button's auto-type-then-wait-for-
// Enter behavior depends on this NOT sending anything itself.
//
// Cancels cleanly on: the caller invoking `cancel()` (wired to the
// composer's real onChange — see RcaChatDock, which calls cancel() before
// forwarding a genuine keystroke), `text` changing (a new Debug click while
// one is still typing), or unmount.
export function useAutoType(text: string | null, setDraft: (s: string) => void, opts?: { cps?: number }) {
  const cps = opts?.cps ?? 45;
  const cancelledRef = useRef(false);

  useEffect(() => {
    if (!text) return;
    cancelledRef.current = false;
    setDraft('');
    let i = 0;
    const interval = setInterval(() => {
      if (cancelledRef.current) {
        clearInterval(interval);
        return;
      }
      i += 1;
      setDraft(text.slice(0, i));
      if (i >= text.length) clearInterval(interval);
    }, 1000 / cps);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  const cancel = () => {
    cancelledRef.current = true;
  };

  return { cancel };
}
