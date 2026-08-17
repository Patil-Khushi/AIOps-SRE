import type { ReactNode } from 'react';
import { LoadingState, ErrorState, EmptyState, UnavailableState, PartialDataNotice } from '@/components/states';

export type SectionState = 'ok' | 'loading' | 'empty' | 'unavailable' | 'error' | 'partial';

// The one place that decides among the five states so no tab hand-rolls its
// own — the product rule "unavailable must never read as healthy" is
// enforced by which sub-component gets rendered here, not by convention.
export function SectionShell({
  state,
  message,
  reason,
  sources,
  present,
  missing,
  emptyIcon,
  children,
}: {
  state: SectionState;
  message?: string;
  reason?: string;
  sources?: string[];
  present?: string[];
  missing?: string[];
  emptyIcon?: ReactNode;
  children: ReactNode;
}) {
  switch (state) {
    case 'loading':
      return <LoadingState label={message ?? 'Loading…'} />;
    case 'error':
      return <ErrorState error={message ?? 'Something went wrong'} />;
    case 'empty':
      return <EmptyState label={message ?? 'Nothing here'} hint={reason} icon={emptyIcon} />;
    case 'unavailable':
      return <UnavailableState label={message ?? 'Not examined'} reason={reason} sources={sources} />;
    case 'partial':
      return (
        <>
          <PartialDataNotice present={present ?? []} missing={missing ?? []} note={reason} />
          {children}
        </>
      );
    case 'ok':
    default:
      return <>{children}</>;
  }
}
