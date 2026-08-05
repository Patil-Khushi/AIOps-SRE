import { useNavigate } from "react-router-dom";
import { ShoppingCart, X } from "lucide-react";
import { money } from "../lib/format.js";
import { useCart } from "../state/CartContext.jsx";
import { useAuth } from "../state/AuthContext.jsx";
import { CHECKOUT_LABELS, STEP } from "../data/checkoutSteps.js";
import OrbitTracker from "../components/orbit/OrbitTracker.jsx";
import OrderSummary from "../components/checkout/OrderSummary.jsx";
import Tile from "../components/orbit/Tile.jsx";
import QtyStepper from "../components/ui/QtyStepper.jsx";

export default function Cart() {
  const { lines, setQty, remove } = useCart();
  const { status } = useAuth();
  const navigate = useNavigate();

  // Cart is public — the auth wall is here, at the first checkout step. That
  // is the standard flow and it gives RequireAuth's `from` a real job.
  function proceed() {
    if (status === "authed") navigate("/checkout/address");
    else navigate("/login", { state: { from: { pathname: "/checkout/address" } } });
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <div className="mx-auto mb-8 max-w-3xl">
        <OrbitTracker steps={CHECKOUT_LABELS} current={STEP.CART} />
      </div>

      <h1 className="mb-5 font-display text-2xl font-semibold">Your cart</h1>

      {lines.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-line py-20 text-center">
          <ShoppingCart size={36} className="mx-auto mb-3 text-muted" />
          <p className="mb-4 text-muted">Your cart is empty.</p>
          <button type="button" onClick={() => navigate("/")} className="btn-primary">
            Browse products
          </button>
        </div>
      ) : (
        <div className="grid gap-8 md:grid-cols-[1fr_320px]">
          <div className="flex flex-col gap-4">
            {lines.map(({ product, qty, lineTotal }) => (
              <div key={product.id} className="surface flex items-center gap-4 p-4">
                <Tile product={product} className="h-20 w-20 shrink-0 rounded-xl" iconSize={28} />
                <div className="min-w-0 flex-1">
                  <span className="mono text-[10px] uppercase text-muted">{product.category}</span>
                  <h4 className="truncate font-display text-sm font-medium">{product.name}</h4>
                  <span className="mono text-sm font-semibold">{money(lineTotal)}</span>
                </div>
                <QtyStepper qty={qty} onChange={(v) => setQty(product.id, v)} />
                <button
                  type="button"
                  onClick={() => remove(product.id)}
                  aria-label={`Remove ${product.name}`}
                  className="p-1 text-muted transition hover:text-coral"
                >
                  <X size={17} />
                </button>
              </div>
            ))}
          </div>

          <OrderSummary ctaLabel="Proceed to checkout" onCta={proceed} />
        </div>
      )}
    </div>
  );
}
