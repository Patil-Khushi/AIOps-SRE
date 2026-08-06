import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, CreditCard, Info, Smartphone, Wallet } from "lucide-react";
import { api } from "../../api/client.js";
import { describeApiError } from "../../lib/errors.js";
import { money } from "../../lib/format.js";
import { CHECKOUT_LABELS, STEP } from "../../data/checkoutSteps.js";
import { toOrderPayload, useCart } from "../../state/CartContext.jsx";
import { useCheckout } from "../../state/CheckoutContext.jsx";
import { useAuth } from "../../state/AuthContext.jsx";
import OrbitTracker from "../../components/orbit/OrbitTracker.jsx";
import OrderSummary from "../../components/checkout/OrderSummary.jsx";
import Banner from "../../components/ui/Banner.jsx";

const METHODS = [
  { id: "card", label: "Card", icon: CreditCard, hint: "Credit or debit card" },
  { id: "upi", label: "UPI", icon: Smartphone, hint: "Pay via any UPI app" },
  { id: "wallet", label: "Wallet", icon: Wallet, hint: "Orbit Pay balance" },
];

export default function Payment() {
  const { lines, total, clear } = useCart();
  const { method, setMethod, address, setLastOrder } = useCheckout();
  const { invalidate } = useAuth();
  const navigate = useNavigate();

  const [phase, setPhase] = useState("idle"); // idle | placing | failed
  const [error, setError] = useState(null);
  const [recovered, setRecovered] = useState(null);

  async function placeOrder() {
    // The guard is load-bearing, not cosmetic: POST /orders has no
    // idempotency key and there is no client-side timeout, so under
    // payment_timeout the request hangs for ~5s. A second click would create
    // a second real Postgres row.
    if (phase === "placing" || lines.length === 0) return;

    setPhase("placing");
    setError(null);
    setRecovered(null);

    const { items, amount } = toOrderPayload(lines, total);
    const r = await api.createOrder(items, amount);

    if (r.ok) {
      setLastOrder(r.data);
      clear(); // only ever on success — retry depends on the cart surviving
      navigate(`/checkout/confirmation/${r.data.id}`, {
        replace: true, // kills back-button re-submit
        state: { order: r.data, shipTo: address, method },
      });
      return;
    }

    const err = describeApiError(r, { service: "Order Service" });
    // A genuinely dead session (not a user-service outage — describeApiError
    // separates those) must bounce to login, preserving the destination.
    if (err.kind === "auth") {
      invalidate();
      navigate("/login", { replace: true, state: { from: { pathname: "/checkout/payment" } } });
      return;
    }

    setError({ ...err, http: r.status, recordedFailed: r.status === 502 || r.status === 504 });
    setPhase("failed");
  }

  // 502/504 mean the order row EXISTS as FAILED but the error body carries no
  // id (create_order.py:57,62). Rather than guessing with getOrders()[0],
  // this is an explicit, on-demand lookup — one request, inspectable.
  async function findFailedOrder() {
    const r = await api.getOrders();
    if (!r.ok) return;
    const list = Array.isArray(r.data) ? r.data : r.data?.orders || [];
    const failed = list.find((o) => String(o.status).toUpperCase() === "FAILED");
    setRecovered(failed ?? null);
  }

  const busy = phase === "placing";

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <div className="mx-auto mb-8 max-w-3xl">
        <OrbitTracker steps={CHECKOUT_LABELS} current={STEP.PAYMENT} />
      </div>

      <div className="grid gap-8 md:grid-cols-[1fr_320px]">
        <div className="surface p-6">
          <h2 className="mb-1 font-display text-xl font-semibold">Payment method</h2>
          <p className="mb-5 text-sm text-muted">
            This is a mock gateway — no real payment is processed.
          </p>

          <div className="mb-6 grid grid-cols-3 gap-3">
            {METHODS.map((opt) => {
              const Icon = opt.icon;
              const active = method === opt.id;
              return (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => setMethod(opt.id)}
                  className={`flex flex-col items-center gap-2 rounded-xl border p-4 text-center transition
                    ${active ? "border-coral bg-coral/5" : "border-line bg-white hover:border-ink/30"}`}
                >
                  <Icon size={22} className={active ? "text-coral" : "text-ink/70"} />
                  <span className="text-sm font-medium">{opt.label}</span>
                  <span className="text-[11px] text-muted">{opt.hint}</span>
                </button>
              );
            })}
          </div>

          {method === "card" && (
            <div className="grid grid-cols-2 gap-4">
              <div className="col-span-2">
                <label className="mb-1 block text-xs font-medium text-muted">Card number</label>
                <input placeholder="4242 4242 4242 4242" className="field mono" />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-muted">Expiry</label>
                <input placeholder="MM/YY" className="field mono" />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-muted">CVV</label>
                <input placeholder="•••" className="field mono" />
              </div>
            </div>
          )}
          {method === "upi" && (
            <div>
              <label className="mb-1 block text-xs font-medium text-muted">UPI ID</label>
              <input placeholder="yourname@upi" className="field mono" />
            </div>
          )}
          {method === "wallet" && (
            <div className="rounded-lg bg-ink/5 p-3 text-sm text-ink/70">
              Orbit Pay balance: <span className="mono">{money(5000)}</span>
            </div>
          )}

          <p className="mt-5 flex items-start gap-2 rounded-lg bg-ink/5 p-3 text-xs text-muted">
            <Info size={14} className="mt-px shrink-0" />
            <span>
              Payment details are display-only and never transmitted. The order request carries
              items and total; the Payment Service always approves.
            </span>
          </p>

          <button onClick={placeOrder} disabled={busy} className="btn-primary mt-6">
            {busy ? "Placing order…" : `Place order · ${money(total)}`}
            {!busy && <ArrowRight size={15} />}
          </button>

          {error && (
            <div className="mt-5">
              <Banner
                tone="err"
                error={error}
                actions={
                  error.recordedFailed && (
                    <>
                      {!recovered && (
                        <button type="button" onClick={findFailedOrder} className="btn-outline px-4 py-1.5 text-xs">
                          Find this order
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => navigate("/orders")}
                        className="btn-outline px-4 py-1.5 text-xs"
                      >
                        View orders
                      </button>
                    </>
                  )
                }
              >
                {error.recordedFailed
                  ? "Your order was recorded as FAILED and payment was not taken. Your cart has been kept so you can retry."
                  : error.detail}
              </Banner>

              {recovered && (
                <p className="mono mt-2 text-xs text-muted">
                  Recorded as order #{recovered.id} — {money(recovered.amount)} — {recovered.status}
                </p>
              )}
            </div>
          )}
        </div>

        <OrderSummary ctaLabel="Place order" onCta={placeOrder} ctaDisabled={busy} busy={busy} />
      </div>
    </div>
  );
}
