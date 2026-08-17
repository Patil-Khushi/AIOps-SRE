import { useState } from 'react';
import { Copy, Check, MessageSquareShare, FileText } from 'lucide-react';
import { api } from '@/lib/api';
import { useToast } from '@/hooks/useToast';

// Copy / Teams / Postmortem — the action row under an agent answer card.
// "Postmortem" formats the same shareText as a short draft rather than
// opening a separate flow: Knowledge Synthesizer (PRS-007) owns the real,
// HITL-gated KB publish; this is just a fast, no-commitment starting point
// a human can paste into that flow or anywhere else.
export function MessageActions({
  shareText,
  title,
  incidentId,
  service,
}: {
  shareText: string;
  title: string;
  incidentId?: string | null;
  service?: string | null;
}) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  const [sending, setSending] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(shareText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.push('Could not copy — clipboard access was denied', 'bad');
    }
  };

  const handleTeams = async () => {
    if (sending) return;
    setSending(true);
    try {
      const res = await api.rcaChatShareTeams({ title, body: shareText, incidentId, service });
      if (res.sent) {
        toast.push('Sent to Teams', 'good');
      } else {
        toast.push(res.reason ?? 'Teams is not configured for this deployment', 'bad');
      }
    } catch (e) {
      toast.push(e instanceof Error ? e.message : 'Failed to send to Teams', 'bad');
    } finally {
      setSending(false);
    }
  };

  const handlePostmortem = async () => {
    const draft = `## ${title}\n\n${shareText}\n\n_Draft only — publish via Knowledge Synthesizer for the reviewed, HITL-gated version._`;
    try {
      await navigator.clipboard.writeText(draft);
      toast.push('Postmortem draft copied', 'good');
    } catch {
      toast.push('Could not copy — clipboard access was denied', 'bad');
    }
  };

  return (
    <div className="mt-2 flex items-center gap-1 border-t border-[var(--icc-border)] pt-2">
      <button
        type="button"
        onClick={handleCopy}
        className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-[var(--icc-fg-muted)] transition hover:bg-[var(--icc-surface-3)] hover:text-[var(--icc-fg)]"
      >
        {copied ? <Check className="h-3 w-3 text-[var(--icc-ok)]" /> : <Copy className="h-3 w-3" />}
        Copy
      </button>
      <button
        type="button"
        onClick={handleTeams}
        disabled={sending}
        className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-[var(--icc-fg-muted)] transition hover:bg-[var(--icc-surface-3)] hover:text-[var(--icc-fg)] disabled:opacity-50"
      >
        <MessageSquareShare className="h-3 w-3" /> {sending ? 'Sending…' : 'Teams'}
      </button>
      <button
        type="button"
        onClick={handlePostmortem}
        className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-[var(--icc-fg-muted)] transition hover:bg-[var(--icc-surface-3)] hover:text-[var(--icc-fg)]"
      >
        <FileText className="h-3 w-3" /> Postmortem
      </button>
    </div>
  );
}
