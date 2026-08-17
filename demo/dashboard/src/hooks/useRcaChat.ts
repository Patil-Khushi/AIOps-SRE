import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import { makeCache } from '@/lib/persistentCache';
import type { ChatAnswer } from '@/types/rca';
import type { RCAVerdict, TriageVerdict } from '@/types/api';

export interface ChatMessageVM {
  id: string;
  role: 'user' | 'agent';
  text: string;
  answer?: ChatAnswer;
  verdict?: RCAVerdict;
  failed?: boolean;
}

// Persisted per run_id so reopening the dock for the same incident restores
// the thread instead of showing empty until the history fetch resolves.
const chatCache = makeCache<ChatMessageVM[]>('icc-chat');

let seq = 0;
function nextId(prefix: string): string {
  seq += 1;
  return `${prefix}-${seq}`;
}

type PendingRetry =
  | { kind: 'analysis'; text: string; triageVerdict: TriageVerdict; incidentId: string | null }
  | { kind: 'question'; text: string; opts?: { triageVerdict?: TriageVerdict | null; incidentId?: string | null } };

export function useRcaChat(runId: string | null) {
  const [messages, setMessages] = useState<ChatMessageVM[]>([]);
  const [status, setStatus] = useState<'idle' | 'sending' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  // Whether a real RCA run has happened for this run_id — before that, the
  // FIRST message (the auto-typed investigation prompt) must trigger the
  // actual analysis, not a read-only chat question over a verdict that
  // doesn't exist yet. After it, every turn is a normal chat question.
  const [analyzed, setAnalyzed] = useState(false);
  const lastFailedRef = useRef<PendingRetry | null>(null);
  // Tracks the currently-active runId so an in-flight request's result can
  // check, when it resolves, whether the dock has since moved on to a
  // different incident — without this, switching incidents mid-request lets
  // the old request's answer (or failure) land on the new incident's thread.
  const activeRunIdRef = useRef(runId);
  useEffect(() => {
    activeRunIdRef.current = runId;
  }, [runId]);

  const appendMessage = useCallback(
    (msg: ChatMessageVM) => {
      setMessages((prev) => {
        const next = [...prev, msg];
        if (runId) chatCache.set(runId, next);
        return next;
      });
    },
    [runId],
  );

  // Load whatever's cached immediately, then reconcile with the server's
  // transcript (covers a session that already exists from a prior tab/reload).
  useEffect(() => {
    setAnalyzed(false);
    if (!runId) {
      setMessages([]);
      return;
    }
    const cached = chatCache.get(runId) ?? [];
    setMessages(cached);
    if (cached.length > 0) setAnalyzed(true);
    // Guards against a stale response landing after the user has already
    // switched to a different incident's runId — without this, incident A's
    // slow history fetch can resolve after the dock has moved on to incident
    // B and overwrite B's messages with A's history.
    let cancelled = false;
    api
      .rcaChatHistory(runId)
      .then((history) => {
        if (cancelled) return;
        const restored: ChatMessageVM[] = history.messages.map((m, i) => ({
          id: nextId(`h${i}`),
          role: m.role === 'assistant' ? 'agent' : 'user',
          text: m.text,
        }));
        setMessages(restored);
        chatCache.set(runId, restored);
        if (restored.length > 0) setAnalyzed(true);
      })
      .catch(() => {
        // No session yet (404) is the common, expected case — runAnalysis
        // below is what creates one.
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // First turn: run the REAL RCA analysis (POST /api/rca with this run_id),
  // which both answers the auto-typed investigation prompt and seeds the
  // chat session server-side — every later question re-uses that session.
  const runAnalysis = useCallback(
    async (text: string, triageVerdict: TriageVerdict, incidentId: string | null) => {
      const trimmed = text.trim();
      if (!trimmed || !runId) return;

      appendMessage({ id: nextId('u'), role: 'user', text: trimmed });
      setStatus('sending');
      setError(null);

      try {
        const verdict = await api.rca(triageVerdict, undefined, { runId, incidentId });
        if (activeRunIdRef.current !== runId) return verdict; // dock moved on; don't apply to the new thread
        appendMessage({ id: nextId('a'), role: 'agent', text: verdict.root_cause, verdict });
        setStatus('idle');
        setAnalyzed(true);
        lastFailedRef.current = null;
        return verdict;
      } catch (e) {
        if (activeRunIdRef.current !== runId) return undefined;
        const message = e instanceof Error ? e.message : String(e);
        setStatus('error');
        setError(message);
        lastFailedRef.current = { kind: 'analysis', text: trimmed, triageVerdict, incidentId };
        setMessages((prev) => {
          const next = prev.map((m) => (m.text === trimmed && m.role === 'user' ? { ...m, failed: true } : m));
          if (runId) chatCache.set(runId, next);
          return next;
        });
        return undefined;
      }
    },
    [runId, appendMessage],
  );

  const send = useCallback(
    async (
      text: string,
      opts?: { triageVerdict?: TriageVerdict | null; incidentId?: string | null },
    ) => {
      const trimmed = text.trim();
      if (!trimmed || !runId) return;

      appendMessage({ id: nextId('u'), role: 'user', text: trimmed });
      setStatus('sending');
      setError(null);

      try {
        const res = await api.rcaChatSend(runId, trimmed, opts);
        if (activeRunIdRef.current !== runId) return res; // dock moved on; don't apply to the new thread
        appendMessage({ id: nextId('a'), role: 'agent', text: res.message.text, answer: res.message.answer });
        setStatus('idle');
        lastFailedRef.current = null;
        return res;
      } catch (e) {
        if (activeRunIdRef.current !== runId) return undefined;
        const message = e instanceof Error ? e.message : String(e);
        setStatus('error');
        setError(message);
        lastFailedRef.current = { kind: 'question', text: trimmed, opts };
        setMessages((prev) => {
          const next = prev.map((m) => (m.text === trimmed && m.role === 'user' ? { ...m, failed: true } : m));
          if (runId) chatCache.set(runId, next);
          return next;
        });
        return undefined;
      }
    },
    [runId, appendMessage],
  );

  const retry = useCallback(() => {
    const last = lastFailedRef.current;
    if (!last) return;
    setMessages((prev) => prev.filter((m) => !m.failed));
    if (last.kind === 'analysis') void runAnalysis(last.text, last.triageVerdict, last.incidentId);
    else void send(last.text, last.opts);
  }, [runAnalysis, send]);

  return { messages, draft, setDraft, send, runAnalysis, analyzed, retry, status, error };
}
