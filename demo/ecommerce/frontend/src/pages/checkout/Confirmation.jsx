import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { AlertTriangle, PartyPopper, Truck } from "lucide-react";
import { api } from "../../api/client.js";
import { money } from "../../lib/format.js";
import { CHECKOUT_LABELS, STEP } from "../../data/checkoutSteps.js";
import { getProduct } from "../../data/catalog.js";
import OrbitTracker from "../../components/orbit/OrbitTracker.jsx";
import StatusPill from "../../components/ui/StatusPill.jsx";

export default function Confirmation() {
  const { orderId } = useParams();
  const location = useLocation();
  const navigate = useNavigate();

  // Fast path: the order handed over by Payment via router state. On a hard
  // refresh that state is gone, so fall back to reading the real row by the
  // id in the URL — which is the actual Postgres primary key.
  const passed = location.state?.order ?? null;
  const shipTo = location.state?.shipTo ?? null;
  const [order, setOrder] = useState(passed);
  const [loading, setLoading] = useState(!passed);

  useEffect(() => {
    if (passed) return;
    let cancelled = false;
    api.getOrder(orderId).then((r) => {
      if (cancelled) return;
      if (r.ok) setOrder(r.data);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [orderId, passed]);

  if (loading) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-24 text-center">
        <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-line border-t-coral" />
      </div>
    );
  }

  if (!order) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-20 text-center">
        <h1 className="font-display text-2xl font-semibold">Order not found</h1>
        <button type="button" onClick={() => navigate("/orders")} className="btn-primary mt-6">
          View your orders
        </button>
      </div>
    );
  }

  const status = String(order.status || "PENDING").toUpperCase();
  const paid = status === "PAID";
  const failed = status === "FAILED";

  // Payment node reflects the real status. Nothing invents a shipping state.
  const trackerStates = [
    "done",
    failed ? "error" : paid ? "done" : "active",
    "disabled",
  ];

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <OrbitTracker steps={CHECKOUT_LABELS} current={STEP.CONFIRMATION} />

      <div className="mt-6 text-center">
        <div
          className={`mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full text-white
            ${failed ? "bg-coral" : "bg-pine"}`}
        >
          {failed ? <AlertTriangle size={28} /> : <PartyPopper size={28} />}
        </div>
        <h1 className="font-display text-2xl font-semibold">
          {failed ? "Payment was not taken" : paid ? "Order confirmed" : "Awaiting payment result"}
        </h1>
        <p className="mt-1 text-sm text-muted">
          {failed
            ? "This order was recorded but will not ship."
            : paid
              ? "Your payment was captured."
              : "The payment result has not been recorded yet."}{" "}
          Order <span className="mono text-ink">#{order.id}</span>
        </p>
      </div>

      <div className="surface mt-8 grid gap-6 p-6 md:grid-cols-2">
        <div>
          <h3 className="mb-3 font-display text-sm uppercase tracking-wide text-muted">
            Delivery details
          </h3>
          {shipTo ? (
            <>
              <p className="text-sm">{shipTo.name}</p>
              <p className="text-sm text-ink/70">{shipTo.line1}</p>
              <p className="text-sm text-ink/70">
                {shipTo.city} {shipTo.pincode}
              </p>
            </>
          ) : (
            <p className="text-sm text-muted">
              Not recorded — this demo has no address service.
            </p>
          )}
          {paid && (
            <div className="mt-3 flex items-center gap-2 text-sm text-muted">
              <Truck size={15} /> Fulfilment is not tracked in this demo.
            </div>
          )}
        </div>

        <div>
          <h3 className="mb-3 font-display text-sm uppercase tracking-wide text-muted">
            Order total
          </h3>
          <div className="mono text-2xl font-semibold">{money(order.amount)}</div>
          <div className="mt-2">
            <StatusPill status={status} />
          </div>
          {Array.isArray(order.items) && order.items.length > 0 && (
            <ul className="mt-3 space-y-1 text-xs text-muted">
              {order.items.map((it, i) => (
                <li key={i} className="mono">
                  {getProduct(it.sku)?.name ?? it.sku} × {it.qty}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="mt-8 flex justify-center gap-3">
        {failed && (
          <button type="button" onClick={() => navigate("/cart")} className="btn-primary">
            Try again
          </button>
        )}
        <button type="button" onClick={() => navigate("/orders")} className="btn-outline">
          View orders
        </button>
        <button type="button" onClick={() => navigate("/")} className="btn-outline">
          Continue shopping
        </button>
      </div>
    </div>
  );
}
