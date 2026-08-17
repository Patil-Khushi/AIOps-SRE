import { RefreshCw, ExternalLink } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { ChatMessageVM } from '@/hooks/useRcaChat';
import { toAnswerCard, toVerdictCard } from '@/lib/chatAnswerCard';
import { AnswerCard } from './AnswerCard';

export function ChatMessageBubble({
  message,
  onRetry,
  incidentPath,
  incidentId,
  service,
}: {
  message: ChatMessageVM;
  onRetry?: () => void;
  incidentPath?: string;
  incidentId?: string | null;
  service?: string | null;
}) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div
          className={
            message.failed
              ? 'max-w-[85%] rounded-lg border border-[var(--icc-bad)]/50 bg-[var(--icc-bad)]/10 px-3 py-2 text-sm text-[var(--icc-bad)]'
              : 'max-w-[85%] rounded-lg bg-[var(--icc-accent)] px-3 py-2 text-sm text-white'
          }
        >
          <p>{message.text}</p>
          {message.failed && (
            <button
              type="button"
              onClick={onRetry}
              className="mt-1 inline-flex items-center gap-1 text-[11px] font-medium underline"
            >
              <RefreshCw className="h-3 w-3" /> Retry
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start">
      <div className="max-w-[92%]">
        {message.verdict ? (
          <>
            <AnswerCard card={toVerdictCard(message.verdict)} incidentId={incidentId} service={service} />
            {incidentPath && (
              <Link
                to={incidentPath}
                className="mt-1.5 inline-flex items-center gap-1 text-[11px] font-medium text-[var(--icc-accent)] hover:underline"
              >
                <ExternalLink className="h-3 w-3" /> Open incident view
              </Link>
            )}
          </>
        ) : message.answer ? (
          <AnswerCard card={toAnswerCard(message.answer)} incidentId={incidentId} service={service} />
        ) : (
          <p className="text-sm text-[var(--icc-fg)]">{message.text}</p>
        )}
      </div>
    </div>
  );
}
