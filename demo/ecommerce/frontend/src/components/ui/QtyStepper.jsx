import { Minus, Plus } from "lucide-react";

/**
 * Quantity control.
 *
 * Buttons only, clamped 1..10 — no free-text input. This matches the design
 * and structurally prevents a NaN qty reaching pydantic's `qty: int` and
 * coming back as a 422.
 */
export default function QtyStepper({ qty, onChange, min = 1, max = 10 }) {
  return (
    <div className="flex items-center overflow-hidden rounded-full border border-line bg-white">
      <button
        type="button"
        onClick={() => onChange(Math.max(min, qty - 1))}
        disabled={qty <= min}
        aria-label="Decrease quantity"
        className="flex h-8 w-8 items-center justify-center transition hover:bg-ink/5 disabled:opacity-30"
      >
        <Minus size={14} />
      </button>
      <span className="mono w-8 text-center text-sm">{qty}</span>
      <button
        type="button"
        onClick={() => onChange(Math.min(max, qty + 1))}
        disabled={qty >= max}
        aria-label="Increase quantity"
        className="flex h-8 w-8 items-center justify-center transition hover:bg-ink/5 disabled:opacity-30"
      >
        <Plus size={14} />
      </button>
    </div>
  );
}
