// Slash-command shortcuts. Each maps to plain language the existing
// deterministic intent matcher already understands (agents/rca_agent/chat.py
// `_INTENTS`) — no new command-parsing surface, just a fast way to ask the
// same questions the chatbot already answers.
const COMMANDS: { command: string; question: string }[] = [
  { command: '/cause', question: 'What is the root cause?' },
  { command: '/verify', question: 'What is the verification plan?' },
  { command: '/similar', question: 'Has this happened before?' },
];

export function SlashCommandChips({ onPick }: { onPick: (question: string) => void }) {
  return (
    <div className="flex flex-wrap gap-1.5 border-t border-[var(--icc-border)] px-3 pt-2">
      {COMMANDS.map((c) => (
        <button
          key={c.command}
          type="button"
          onClick={() => onPick(c.question)}
          className="chip !text-[11px] transition hover:!border-[var(--icc-accent)] hover:!text-[var(--icc-accent)]"
        >
          {c.command}
        </button>
      ))}
    </div>
  );
}
