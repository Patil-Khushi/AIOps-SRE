import { useState } from "react";
import { Link } from "react-router-dom";
import { CATALOG, CATEGORIES, FREE_DELIVERY_ABOVE } from "../data/catalog.js";
import { discountPct, money } from "../lib/format.js";
import { useCart } from "../state/CartContext.jsx";
import Tile from "../components/orbit/Tile.jsx";
import RatingRow from "../components/ui/RatingRow.jsx";

export default function Home() {
  const [category, setCategory] = useState("All");
  const { add } = useCart();
  const products = category === "All" ? CATALOG : CATALOG.filter((p) => p.category === category);

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div
        className="mb-6 rounded-3xl p-8 md:p-12"
        style={{ background: "linear-gradient(120deg, #1B1B2F, #2F6F5E)" }}
      >
        <p className="mono mb-2 text-[11px] uppercase tracking-widest text-white/60">
          This week&apos;s orbit
        </p>
        <h1 className="max-w-lg font-display text-3xl font-semibold leading-tight text-white md:text-4xl">
          Everyday gear, priced like it should be.
        </h1>
        <p className="mt-3 max-w-md text-sm text-white/70">
          Up to 35% off across audio, wearables and fitness — free delivery above{" "}
          {money(FREE_DELIVERY_ABOVE)}.
        </p>
      </div>

      <div className="-mx-1 mb-6 flex gap-2 overflow-x-auto px-1 pb-2">
        {CATEGORIES.map((cat) => (
          <button
            key={cat}
            type="button"
            onClick={() => setCategory(cat)}
            className={`chip ${category === cat ? "chip-active" : ""}`}
          >
            {cat}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-5 md:grid-cols-3 lg:grid-cols-4">
        {products.map((p) => (
          <div key={p.id} className="surface lift flex flex-col overflow-hidden">
            <Link to={`/product/${p.id}`} className="block">
              <Tile product={p} className="h-36" iconSize={44} />
            </Link>
            <div className="flex flex-1 flex-col gap-1.5 p-4">
              <span className="mono text-[10px] uppercase tracking-wider text-muted">{p.category}</span>
              <Link to={`/product/${p.id}`} className="font-display text-[15px] font-medium leading-snug hover:text-coral">
                {p.name}
              </Link>
              <RatingRow rating={p.rating} reviews={p.reviews} />
              <div className="mt-1 flex items-baseline gap-2">
                <span className="mono text-lg font-semibold">{money(p.price)}</span>
                <span className="mono text-xs text-muted line-through">{money(p.mrp)}</span>
              </div>
              <span className="text-xs font-medium text-pine">{discountPct(p.price, p.mrp)}% off</span>
              <button type="button" onClick={() => add(p.id)} className="btn-outline mt-2 w-full py-1.5">
                Add to cart
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
