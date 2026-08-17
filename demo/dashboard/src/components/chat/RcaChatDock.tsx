import { useRcaChat } from '@/hooks/useRcaChat';
import { useRcaProgress } from '@/lib/rcaStream';
import { IccRoot } from '@/components/icc/IccRoot';
import { useChatDock } from './ChatDockProvider';
import { useAutoType } from './useAutoType';
import { ChatHeader } from './ChatHeader';
import { ChatMessageList } from './ChatMessageList';
import { ChatComposer } from './ChatComposer';
import { ProgressStageList } from './ProgressStageList';
import { SlashCommandChips } from './SlashCommandChips';

// A fixed overlay panel (not a flex sibling — Layout.tsx's page content never
// reflows when this opens/closes) that slides in from the right, the way an
// ordinary website chat widget does. Stays mounted even while closed — a
// closed panel is translated fully off-screen (`translate-x-full`) rather
// than unmounted, which is what makes the open/close transition possible at
// all; a component that returns null has nothing to animate from.
export function RcaChatDock() {
  const dock = useChatDock();
  const chat = useRcaChat(dock.runId);
  const autoType = useAutoType(dock.open ? dock.seedPrompt : null, chat.setDraft);
  // Live stage labels only during the real analysis run (POST /api/rca) —
  // that's the ~5-15s call worth showing progress for; a read-only chat
  // question answers in ~2-5s with just the existing "Thinking…" indicator.
  const analyzing = chat.status === 'sending' && !chat.analyzed;
  const progress = useRcaProgress(analyzing ? dock.runId : null);

  const handleChange = (v: string) => {
    autoType.cancel();
    chat.setDraft(v);
  };

  const sendText = (text: string) => {
    autoType.cancel();
    if (!text.trim() || !dock.row) return;
    chat.setDraft('');
    if (!chat.analyzed) {
      // No RCA verdict exists for this run yet — the first turn (normally
      // the auto-typed investigation prompt) triggers the REAL analysis
      // (POST /api/rca), which also seeds the chat session server-side.
      // Every turn after this is a normal read-only question.
      void chat.runAnalysis(text, dock.row.triageVerdict, dock.row.incidentId);
    } else {
      void chat.send(text, {
        triageVerdict: dock.row.triageVerdict,
        incidentId: dock.row.incidentId,
      });
    }
  };

  const handleSubmit = () => sendText(chat.draft);

  return (
    <>
      {/* Backdrop: dims + blurs the page behind the dock and swallows clicks/
          hover so the dashboard underneath is inert while the chat is open.
          Kept mounted (like the panel) and cross-faded via opacity/pointer-events
          rather than unmounted, so it can transition in step with the panel. */}
      <div
        className={
          'fixed inset-0 z-30 bg-black/45 backdrop-blur-sm transition-opacity duration-300 ease-out ' +
          (dock.open ? 'opacity-100' : 'pointer-events-none opacity-0')
        }
        onClick={dock.close}
        aria-hidden="true"
      />
      <IccRoot
        className={
          'fixed bottom-24 right-6 top-6 z-40 flex w-[380px] flex-col ' +
          'overflow-hidden rounded-2xl border border-[var(--icc-border)] shadow-2xl ' +
          'origin-bottom-right transition-all duration-300 ease-out ' +
          (dock.open ? 'scale-100 opacity-100' : 'pointer-events-none scale-0 opacity-0')
        }
        aria-hidden={!dock.open}
      >
        {/* Fixed header row — flex-none so it never shrinks or scrolls */}
        <div className="flex-none">
          <ChatHeader row={dock.row} onClose={dock.close} />
        </div>
        {/* Message thread — the only scrollable region. min-h-0 is required
            here: without it a flex child defaults to min-height:auto and
            refuses to shrink below its content size, which is what let the
            thread grow underneath the composer instead of scrolling. */}
        <div className="flex min-h-0 flex-1 flex-col">
          <ChatMessageList
            messages={chat.messages}
            streaming={chat.status === 'sending' && !analyzing}
            onRetry={chat.retry}
            incidentPath={dock.row ? `/console/incidents/${dock.row.id}` : undefined}
            incidentId={dock.row?.incidentId}
            service={dock.row?.service}
          />
        </div>
        {/* Fixed footer stack — progress / slash-chips / composer, flex-none
            so it stays pinned below the thread and never overlaps it */}
        <div className="flex-none">
          {analyzing && (
            <div className="border-t border-[var(--icc-border)] px-3 py-2">
              <ProgressStageList stages={progress.stages} />
            </div>
          )}
          {chat.analyzed && chat.status !== 'sending' && <SlashCommandChips onPick={sendText} />}
          <ChatComposer
            value={chat.draft}
            onChange={handleChange}
            onSubmit={handleSubmit}
            disabled={chat.status === 'sending'}
            placeholder="Ask, or type / for commands…"
          />
        </div>
      </IccRoot>
    </>
  );
}
