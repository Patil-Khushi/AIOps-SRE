import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import type { IncidentRowVM } from '@/lib/incidentVm';
import { runIdFor } from '@/lib/runId';

interface ChatDockState {
  open: boolean;
  row: IncidentRowVM | null;
  runId: string | null;
  seedPrompt: string | null;
}

interface ChatDockContextValue extends ChatDockState {
  openFor(row: IncidentRowVM, prompt: string): void;
  focus(row: IncidentRowVM): void;
  close(): void;
  toggle(): void;
}

const ChatDockContext = createContext<ChatDockContextValue | null>(null);

const INITIAL: ChatDockState = { open: false, row: null, runId: null, seedPrompt: null };

// Deliberately NOT route-aware: the dock renders nothing until openFor() is
// called (RcaChatDock returns null while closed), so mounting this provider
// around the whole console (Layout.tsx) has zero visual/behavioral impact on
// the other console pages.
export function ChatDockProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<ChatDockState>(INITIAL);

  const openFor = useCallback((row: IncidentRowVM, prompt: string) => {
    setState({ open: true, row, runId: runIdFor(row.id), seedPrompt: prompt });
  }, []);
  // Scopes the dock to `row` (so the launcher bubble appears) without opening
  // the panel — for pages like the incident workspace that should offer the
  // RCA agent entry point on load, but not force the panel open unasked.
  const focus = useCallback((row: IncidentRowVM) => {
    setState((s) => (s.row?.id === row.id ? s : { ...s, row, runId: runIdFor(row.id) }));
  }, []);
  const close = useCallback(() => setState((s) => ({ ...s, open: false })), []);
  const toggle = useCallback(() => setState((s) => ({ ...s, open: !s.open })), []);

  const value = useMemo(() => ({ ...state, openFor, focus, close, toggle }), [state, openFor, focus, close, toggle]);

  return <ChatDockContext.Provider value={value}>{children}</ChatDockContext.Provider>;
}

export function useChatDock(): ChatDockContextValue {
  const ctx = useContext(ChatDockContext);
  if (!ctx) throw new Error('useChatDock must be used inside a ChatDockProvider');
  return ctx;
}
