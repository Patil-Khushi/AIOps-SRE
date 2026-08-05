import { ArrowRight } from "lucide-react";
import { money } from "../../lib/format.js";
import { useCart } from "../../state/CartContext.jsx";

/** Sticky right rail, reused verbatim across Cart / Address / Payment. */
export default function OrderSummary({ ctaLabel, onCta, ctaDisabled, busy, children }) {
  const { count, subtotal, savings, shipping, total } = useCart();

  return (
    <div className="surface sticky top-24 h-fit p-5">
      <h3 className="mb-4 font-display text-sm uppercase tracking-wide text-muted">Order summary</h3>

      <div className="mono flex flex-col gap-2 text-sm">
        <div className="flex justify-between">
          <span className="text-ink/70">Items ({count})</span>
          <span>{money(subtotal)}</span>
        </div>
        {savings > 0 && (
          <div className="flex justify-between text-pine">
            <span>You save</span>
            <span>−{money(savings)}</span>
          </div>
        )}
        <div className="flex justify-between">
          <span className="text-ink/70">Delivery</span>
          <span>{shipping === 0 ? "FREE" : money(shipping)}</span>
        </div>
        <div className="my-2 border-t border-line" />
        <div className="flex justify-between text-base font-semibold">
          <span>Total</span>
          <span>{money(total)}</span>
        </div>
      </div>

      {onCta && (
        <button onClick={onCta} disabled={ctaDisabled} className="btn-primary mt-5 w-full">
          {busy ? "Working…" : ctaLabel}
          {!busy && <ArrowRight size={15} />}
        </button>
      )}

      {children}
    </div>
  );
}
