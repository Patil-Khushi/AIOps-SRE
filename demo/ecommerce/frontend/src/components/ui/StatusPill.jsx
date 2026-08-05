// Order status. The backend only ever produces these three —
// see order-service/src/routes/create_order.py and models/order.py.
const TONES = {
  PAID: "bg-pine/15 text-pine",
  PENDING: "bg-gold/25 text-ink",
  FAILED: "bg-coral/15 text-coral-dark",
};

export default function StatusPill({ status }) {
  const key = String(status || "PENDING").toUpperCase();
  return (
    <span
      className={`mono inline-block rounded-full px-2 py-0.5 text-[11px] uppercase tracking-wider
        ${TONES[key] ?? "bg-muted/15 text-muted"}`}
    >
      {key}
    </span>
  );
}
