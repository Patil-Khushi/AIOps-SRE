import { useEffect, useRef } from 'react';
import { Loader2 } from 'lucide-react';
import type { ChatMessageVM } from '@/hooks/useRcaChat';
import { EmptyState } from '@/components/states';
import { ChatMessageBubble } from './ChatMessageBubble';

export function ChatMessageList({
  messages,
  streaming,
  onRetry,
  incidentPath,
  incidentId,
  service,
}: {
  messages: ChatMessageVM[];
  streaming: boolean;
  onRetry: () => void;
  incidentPath?: string;
  incidentId?: string | null;
  service?: string | null;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedToBottom = useRef(true);

  useEffect(() => {
    if (pinnedToBottom.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streaming]);

  return (
    <div
      ref={scrollRef}
      onScroll={(e) => {
        const el = e.currentTarget;
        pinnedToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
      }}
      className="h-full min-h-0 space-y-3 overflow-y-auto p-3"
    >
      {messages.length === 0 && !streaming && (
        <EmptyState label="Ask the RCA Agent" hint="Type a question, or press Enter to send the auto-typed investigation prompt." />
      )}
      {messages.map((m) => (
        <ChatMessageBubble
          key={m.id}
          message={m}
          onRetry={onRetry}
          incidentPath={incidentPath}
          incidentId={incidentId}
          service={service}
        />
      ))}
      {streaming && (
        <div className="flex items-center gap-2 text-xs text-[var(--icc-fg-muted)]">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Thinking…
        </div>
      )}
    </div>
  );
}
