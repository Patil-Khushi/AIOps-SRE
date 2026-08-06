import { useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { ChevronLeft, MapPin, Package, Truck } from "lucide-react";
import { getProduct, FREE_DELIVERY_ABOVE } from "../data/catalog.js";
import { discountPct, money } from "../lib/format.js";
import { useCart } from "../state/CartContext.jsx";
import Tile from "../components/orbit/Tile.jsx";
import RatingRow from "../components/ui/RatingRow.jsx";
import QtyStepper from "../components/ui/QtyStepper.jsx";

export default function ProductDetail() {
  const { id } = useParams();
  const product = getProduct(id);
  const [qty, setQty] = useState(1);
  const { add } = useCart();
  const navigate = useNavigate();

  if (!product) return <Navigate to="/" replace />;

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <Link to="/" className="mb-5 inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
        <ChevronLeft size={16} /> Back to results
      </Link>

      <div className="grid gap-10 md:grid-cols-2">
        <Tile product={product} className="h-80 rounded-3xl md:h-[420px]" iconSize={96} />

        <div className="flex flex-col">
          <span className="mono text-xs uppercase tracking-wider text-muted">{product.category}</span>
          <h1 className="mt-1 font-display text-3xl font-semibold leading-tight">{product.name}</h1>
          <div className="mt-3">
            <RatingRow rating={product.rating} reviews={product.reviews} />
          </div>

          <div className="mt-5 flex items-baseline gap-3">
            <span className="mono text-3xl font-semibold">{money(product.price)}</span>
            <span className="mono text-base text-muted line-through">{money(product.mrp)}</span>
            <span className="text-sm font-medium text-pine">
              {discountPct(product.price, product.mrp)}% off
            </span>
          </div>
          <span className="mt-1 text-xs text-muted">Inclusive of all taxes</span>

          <p className="mt-5 max-w-md text-sm leading-relaxed text-ink/80">{product.desc}</p>

          <div className="mt-6 flex items-center gap-4">
            <span className="text-sm font-medium">Quantity</span>
            <QtyStepper qty={qty} onChange={setQty} />
          </div>

          <div className="mt-6 flex gap-3">
            <button type="button" onClick={() => add(product.id, qty)} className="btn-outline flex-1">
              Add to cart
            </button>
            <button
              type="button"
              onClick={() => {
                add(product.id, qty);
                navigate("/cart");
              }}
              className="btn-primary flex-1"
            >
              Buy now
            </button>
          </div>

          <div className="mt-8 flex flex-col gap-3 border-t border-line pt-5 text-sm">
            <div className="flex items-center gap-2 text-ink/80">
              <MapPin size={16} className="shrink-0 text-pine" />
              <span>Delivery to Pune 411001</span>
            </div>
            <div className="flex items-center gap-2 text-ink/80">
              <Truck size={16} className="shrink-0 text-pine" />
              <span>Free delivery on orders above {money(FREE_DELIVERY_ABOVE)}</span>
            </div>
            <div className="flex items-center gap-2 text-ink/80">
              <Package size={16} className="shrink-0 text-pine" />
              <span>7-day easy returns</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
