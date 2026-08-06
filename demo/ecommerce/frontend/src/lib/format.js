// Formatting helpers. This is the ONLY place a currency symbol appears —
// the backend stores a bare float with no currency field anywhere in the
// stack, so the symbol is purely a display decision made here.

const inr = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

/** Whole-rupee currency, e.g. ₹2,499. */
export function money(n) {
  const value = Number(n);
  return Number.isFinite(value) ? inr.format(value) : "—";
}

/** Two-decimal rounding for the amount we POST. */
export function round2(n) {
  return Math.round(Number(n) * 100) / 100;
}

/** Percentage off, for the MRP strike-through. Display only — the server
 *  never sees `mrp`, only the final per-line `price`. */
export function discountPct(price, mrp) {
  if (!mrp || mrp <= price) return 0;
  return Math.round((1 - price / mrp) * 100);
}

/** Compact review counts, e.g. 1,284. */
export function count(n) {
  return Number(n).toLocaleString("en-IN");
}

/** ISO timestamp → readable local time. Keep the raw ISO in a title attribute
 *  at the call site so it stays greppable against Loki. */
export function dateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
