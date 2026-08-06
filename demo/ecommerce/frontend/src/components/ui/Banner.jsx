import { AlertTriangle, CheckCircle2, Info } from "lucide-react";
import { provenance } from "../../lib/errors.js";

const TONES = {
  ok: { wrap: "border-pine/30 bg-pine/5 text-pine", Icon: CheckCircle2 },
  err: { wrap: "border-coral/30 bg-coral/5 text-coral-dark", Icon: AlertTriangle },
  warn: { wrap: "border-gold/50 bg-gold/10 text-ink", Icon: Info },
};

/**
 * Message panel.
 *
 * When given an `error` from describeApiError() it also renders the
 * provenance line (HTTP status + latency). That line is the whole reason
 * this app is worth looking at during a failure — "504 from order-service
 * after 5,012 ms" tells you which hop broke; "Something went wrong" does not.
 */
export default function Banner({ tone = "err", title, children, error, actions }) {
  const { wrap, Icon } = TONES[tone] ?? TONES.err;
  const heading = title ?? error?.title;
  const body = children ?? error?.detail;

  return (
    <div className={`flex gap-3 rounded-xl border p-4 text-sm ${wrap}`} role="status">
      <Icon size={18} className="mt-0.5 shrink-0" />
      <div className="min-w-0 flex-1">
        {heading && <p className="font-semibold">{heading}</p>}
        {body && <p className="mt-0.5 text-ink/70">{body}</p>}
        {error && (
          <p className="mono mt-1.5 text-[11px] uppercase tracking-wider text-muted">
            {provenance(error)}
          </p>
        )}
        {actions && <div className="mt-3 flex flex-wrap gap-2">{actions}</div>}
      </div>
    </div>
  );
}
