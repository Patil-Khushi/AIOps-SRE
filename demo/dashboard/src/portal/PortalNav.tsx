import { Link } from 'react-router-dom';

// Top-bar — always rendered, but the letters materialise left-to-right once
// the boot is ~complete. Each character animates with an 80ms stagger so
// the audience reads it as the system "writing itself into existence".

interface PortalNavProps {
  progress: number;
}

const NAV_ITEMS: { label: string; to: string }[] = [
  { label: 'Agents',       to: '/agents' },
  { label: 'Architecture', to: '/console/topology' },
  { label: 'Integrations', to: '/console/health' },
  { label: 'SRE Ops',      to: '/console/alerts' },
  { label: 'RCA Agent',    to: '/console/reasoning' },
];

const REVEAL_AT = 0.95;
const LETTER_STAGGER_MS = 80;
const ITEM_STAGGER_MS   = 200; // gap between items so it reads left-to-right

interface StaggeredTextProps {
  text: string;
  reveal: boolean;
  baseDelayMs: number;
}

function StaggeredText({ text, reveal, baseDelayMs }: StaggeredTextProps) {
  return (
    <span aria-label={text}>
      {[...text].map((ch, i) => (
        <span
          key={i}
          aria-hidden="true"
          className={reveal ? 'nav-letter-reveal' : 'nav-letter-pending'}
          style={{
            display: 'inline-block',
            animationDelay: reveal ? `${baseDelayMs + i * LETTER_STAGGER_MS}ms` : undefined,
            whiteSpace: 'pre',
          }}
        >
          {ch === ' ' ? ' ' : ch}
        </span>
      ))}
    </span>
  );
}

export default function PortalNav({ progress }: PortalNavProps) {
  const reveal = progress >= REVEAL_AT;

  return (
    <header className="pointer-events-none fixed inset-x-0 top-0 z-[50] px-8 py-6">
      <div className="mx-auto flex max-w-7xl items-center justify-between">
        <Link
          to="/"
          className="pointer-events-auto flex items-center gap-3"
          aria-label="Adaptive AIOps home"
        >
          <div
            className="flex h-9 w-9 items-center justify-center rounded-full bg-white"
            style={{ boxShadow: reveal ? '0 0 16px rgba(99, 102, 241, 0.45)' : undefined }}
          >
            <svg width="16" height="16" viewBox="0 0 32 32" fill="none">
              <path d="M16 2 L4 8 V20 L16 30 L28 20 V8 Z" fill="#4f46e5" />
              <path d="M16 10 L10 14 V20 L16 24 L22 20 V14 Z" fill="white" />
            </svg>
          </div>
          <span
            className="font-display text-[13px] font-extrabold uppercase text-white"
            style={{ letterSpacing: '-0.02em' }}
          >
            <StaggeredText text="Adaptive AIOps" reveal={reveal} baseDelayMs={0} />
          </span>
        </Link>

        <nav className="pointer-events-auto hidden items-center gap-8 md:flex">
          {NAV_ITEMS.map(({ label, to }, idx) => (
            <Link
              key={label}
              to={to}
              className="font-body text-[11px] font-medium uppercase text-white/80 transition-colors hover:text-white"
              style={{ letterSpacing: '0.12em' }}
            >
              <StaggeredText
                text={label}
                reveal={reveal}
                baseDelayMs={400 + idx * ITEM_STAGGER_MS}
              />
            </Link>
          ))}
        </nav>
      </div>
    </header>
  );
}
