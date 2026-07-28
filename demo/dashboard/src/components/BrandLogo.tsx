import type { ReactElement } from 'react';
import { BRAND_ICONS } from '../data/brandIcons';

// Brand logos for the Integrations directory.
//
// Three tiers, in priority order:
//   1. BRAND_ICONS  — official marks vendored from simple-icons (CC0-1.0). Exact.
//   2. CUSTOM_MARKS — hand-authored glyphs for brands simple-icons does not carry
//      (the Slack, Microsoft and ServiceNow marks have all been removed from that
//      set on trademark grounds). Slack and Teams are drawn in their real brand
//      colours; ServiceNow / Loki / flagd are deliberately *stylised* marks rather
//      than facsimiles — see the note on each below.
//   3. Monogram   — defensive fallback so a newly-added tool renders something
//      sensible before anyone draws it a mark. Every tool currently on the
//      Integrations page resolves to tier 1 or 2, so nothing hits this today.
//
// Everything is inline SVG: no network request, so the page renders identically
// on a disconnected demo laptop. Brand names and marks remain the property of
// their owners and appear here only to identify supported integrations.

interface CustomMark {
  /** Rendered as-is; already carries its own fills. */
  node: ReactElement;
  /** Multi-colour marks must not be tinted by the tile's currentColor. */
  multicolor?: boolean;
  /** Tile accent when the mark defines its own colours. */
  hex: string;
}

const CUSTOM_MARKS: Record<string, CustomMark> = {
  // Slack — the four-bar pinwheel in Slack's four brand colours.
  Slack: {
    multicolor: true,
    hex: '4A154B',
    node: (
      <>
        <rect x="9.6" y="1.6" width="3.1" height="8.2" rx="1.55" fill="#36C5F0" />
        <rect x="14.2" y="9.6" width="8.2" height="3.1" rx="1.55" fill="#2EB67D" />
        <rect x="11.3" y="14.2" width="3.1" height="8.2" rx="1.55" fill="#ECB22E" />
        <rect x="1.6" y="11.3" width="8.2" height="3.1" rx="1.55" fill="#E01E5A" />
      </>
    ),
  },

  // Microsoft Teams — the purple rounded tile with its white "T".
  'Microsoft Teams': {
    multicolor: true,
    hex: '6264A7',
    node: (
      <>
        <rect x="2.5" y="3.5" width="19" height="17" rx="4.2" fill="#6264A7" />
        <path d="M7.4 8.1h9.2v2.35h-3.4v6.6h-2.4v-6.6H7.4z" fill="#fff" />
      </>
    ),
  },

  // ServiceNow — stylised, not a facsimile. The wordmark does not reduce to a
  // 24px glyph, so this is a brand-green ring-and-core mark standing in for it.
  ServiceNow: {
    hex: '62D84E',
    node: (
      <>
        <path
          d="M12 1.6a10.4 10.4 0 1 0 0 20.8 10.4 10.4 0 0 0 0-20.8Zm0 2.6a7.8 7.8 0 1 1 0 15.6 7.8 7.8 0 0 1 0-15.6Z"
          fill="currentColor"
        />
        <circle cx="12" cy="12" r="3.9" fill="currentColor" />
      </>
    ),
  },

  // Grafana Loki — stylised: a log-stream glyph. Loki has no standalone mark in
  // simple-icons, and the Grafana logo would misrepresent which product this is.
  Loki: {
    hex: 'F9A825',
    node: (
      <>
        <rect
          x="2.6"
          y="3.4"
          width="18.8"
          height="17.2"
          rx="3.4"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.1"
        />
        <path
          d="M6.6 8.6h10.8M6.6 12h7.6M6.6 15.4h9.2"
          stroke="currentColor"
          strokeWidth="2.1"
          strokeLinecap="round"
        />
      </>
    ),
  },

  // flagd — stylised: the feature-flag glyph the project itself uses to mean
  // "flag-gated scenario".
  flagd: {
    hex: 'FFC008',
    node: (
      <path
        d="M5.2 2.2a1.3 1.3 0 0 1 1.3 1.3v.9l11.6-.9c1 0 1.6 1.1 1.1 1.9l-2.3 3.7 2.3 3.7c.5.9-.1 1.9-1.1 1.9l-11.6-.9v7.7a1.3 1.3 0 0 1-2.6 0V3.5a1.3 1.3 0 0 1 1.3-1.3Z"
        fill="currentColor"
      />
    ),
  },
};

/** Official brand hex where we have one, else the caller's fallback. */
export function brandColor(name: string, fallback: string): string {
  const hex = BRAND_ICONS[name]?.hex ?? CUSTOM_MARKS[name]?.hex;
  return hex ? `#${hex}` : fallback;
}

/** True when the mark supplies its own colours and must not be tinted. */
export function isMulticolor(name: string): boolean {
  return CUSTOM_MARKS[name]?.multicolor === true;
}

export function hasLogo(name: string): boolean {
  return name in BRAND_ICONS || name in CUSTOM_MARKS;
}

export function BrandLogo({
  name,
  mono,
  className = 'h-6 w-6',
}: {
  name: string;
  /** Monogram fallback for names with no mark (MCP, A2A). */
  mono: string;
  className?: string;
}) {
  const official = BRAND_ICONS[name];
  const custom = CUSTOM_MARKS[name];

  if (official || custom) {
    return (
      <svg
        viewBox="0 0 24 24"
        className={className}
        // The tile supplies the colour via CSS `color`, so a single-colour mark
        // inherits the brand hex while a multi-colour mark keeps its own fills.
        fill="none"
        role="img"
        aria-label={`${name} logo`}
      >
        {official ? <path d={official.path} fill="currentColor" /> : custom!.node}
      </svg>
    );
  }

  return (
    <span className="font-display text-[11px] font-black leading-none" aria-hidden="true">
      {mono}
    </span>
  );
}
