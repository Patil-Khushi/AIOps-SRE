import { useNavigate } from "react-router-dom";
import { ArrowRight, Info } from "lucide-react";
import { CHECKOUT_LABELS, STEP, isAddressComplete } from "../../data/checkoutSteps.js";
import { useCheckout } from "../../state/CheckoutContext.jsx";
import OrbitTracker from "../../components/orbit/OrbitTracker.jsx";
import OrderSummary from "../../components/checkout/OrderSummary.jsx";
import Field from "../../components/ui/Field.jsx";

export default function Address() {
  const { address, setAddress } = useCheckout();
  const navigate = useNavigate();
  const complete = isAddressComplete(address);

  const set = (k) => (e) => setAddress({ ...address, [k]: e.target.value });
  const go = () => complete && navigate("/checkout/payment");

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <div className="mx-auto mb-8 max-w-3xl">
        <OrbitTracker steps={CHECKOUT_LABELS} current={STEP.ADDRESS} />
      </div>

      <div className="grid gap-8 md:grid-cols-[1fr_320px]">
        <div className="surface p-6">
          <h2 className="mb-1 font-display text-xl font-semibold">Delivery address</h2>
          <p className="mb-5 text-sm text-muted">Where should we send your order?</p>

          <div className="grid grid-cols-2 gap-4">
            <Field label="Full name" value={address.name} onChange={set("name")} placeholder="Khushi Patil" autoComplete="name" />
            <Field label="Phone number" type="tel" inputMode="tel" value={address.phone} onChange={set("phone")} placeholder="98xxxxxxxx" autoComplete="tel" />
            <Field className="col-span-2" label="Address line" value={address.line1} onChange={set("line1")} placeholder="Flat, street, area" autoComplete="street-address" />
            <Field label="City" value={address.city} onChange={set("city")} placeholder="Pune" />
            <Field label="Pincode" inputMode="numeric" value={address.pincode} onChange={set("pincode")} placeholder="411001" />
          </div>

          {/*
            Honesty line. This replaces Orbit's "mock login" disclaimer, which
            gets deleted because login IS real here. The address step is the
            part that stayed mock, so the disclaimer moves to it.
          */}
          <p className="mt-5 flex items-start gap-2 rounded-lg bg-ink/5 p-3 text-xs text-muted">
            <Info size={14} className="mt-px shrink-0" />
            <span>
              Display-only — this demo has no address service. Nothing entered here is transmitted
              or stored; the order request carries items and total only.
            </span>
          </p>

          <button onClick={go} disabled={!complete} className="btn-primary mt-6">
            Continue to payment <ArrowRight size={15} />
          </button>
        </div>

        <OrderSummary ctaLabel="Continue to payment" onCta={go} ctaDisabled={!complete} />
      </div>
    </div>
  );
}
