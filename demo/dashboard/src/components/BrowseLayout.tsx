import { Outlet, Link } from 'react-router-dom';
import { useTheme } from '@/hooks/useTheme';

// Sidebar-less shell for the agent browser (phase → agent drill-down).
// Styled to match the landing portal: deep-space backdrop, ambient
// phase-coloured halos, and a slim top bar with the platform mark.
export default function BrowseLayout() {
  useTheme();
  return (
    <div className="portal-deepspace relative min-h-screen overflow-x-hidden font-body text-white">
      {/* Ambient phase-coloured halos — depth behind the glass panels. */}
      <div
        className="pointer-events-none absolute -left-32 -top-32 h-96 w-96 rounded-full opacity-40 blur-3xl"
        style={{ background: 'rgba(79, 70, 229, 0.25)' }}
      />
      <div
        className="pointer-events-none absolute -right-32 top-1/3 h-96 w-96 rounded-full opacity-30 blur-3xl"
        style={{ background: 'rgba(219, 39, 119, 0.20)' }}
      />
      <div
        className="pointer-events-none absolute -bottom-24 left-1/3 h-96 w-96 rounded-full opacity-25 blur-3xl"
        style={{ background: 'rgba(245, 158, 11, 0.18)' }}
      />

      {/* Slim top bar — platform mark + return to landing. */}
      <header className="relative z-10 px-6 py-6 md:px-10">
        <div className="mx-auto flex max-w-7xl items-center justify-between">
          <Link to="/" className="flex items-center gap-3" aria-label="Adaptive AIOps home">
            <div
              className="flex h-9 w-9 items-center justify-center rounded-full bg-white"
              style={{ boxShadow: '0 0 16px rgba(99, 102, 241, 0.45)' }}
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
              Adaptive AIOps
            </span>
          </Link>
          <Link
            to="/"
            className="font-body text-[11px] font-medium uppercase text-white/70 transition-colors hover:text-white"
            style={{ letterSpacing: '0.12em' }}
          >
            ← Home
          </Link>
        </div>
      </header>

      <main className="relative z-10 px-6 pb-16 md:px-10">
        <div className="mx-auto max-w-7xl animate-fade-in">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
