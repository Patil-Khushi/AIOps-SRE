import { useState } from "react";

/**
 * Studio-shot product tile.
 *
 * Renders real photography when the product has an `image`, and falls back to
 * the lucide icon treatment when it doesn't — or when the file 404s. Both
 * paths share the same gradient backdrop, brand halo and floor shadow, so a
 * photo and an icon read as the same design system rather than two.
 *
 * ⚠️ `product.tile` is a runtime hex string. It MUST go through inline
 * `style`, never into a class name. Tailwind's JIT scans source text for
 * literal class names, so `className={`bg-[${product.tile}]`}` compiles to
 * nothing at all and fails silently — no error, just an invisible halo.
 */
export default function Tile({ product, className = "", iconSize = 44, inset = "10%" }) {
  const Icon = product.icon;
  const [broken, setBroken] = useState(false);
  const showPhoto = Boolean(product.image) && !broken;

  return (
    <div className={`tile-bg relative flex items-center justify-center overflow-hidden ${className}`}>
      {/* brand halo — ~10% alpha via the 8-digit hex suffix */}
      <div
        className="absolute rounded-full"
        style={{ width: "58%", height: "58%", backgroundColor: `${product.tile}1A` }}
      />
      <div className="tile-floor" />

      {showPhoto ? (
        <img
          src={product.image}
          alt={product.name}
          loading="lazy"
          decoding="async"
          onError={() => setBroken(true)}
          className="relative z-10 h-full w-full rounded-xl object-cover"
          style={{
            padding: inset,
            filter: "drop-shadow(0 10px 14px rgba(27,27,47,0.18))",
          }}
        />
      ) : (
        Icon && (
          <Icon
            size={iconSize}
            strokeWidth={1.25}
            className="relative z-10"
            style={{ color: product.tile, filter: "drop-shadow(0 8px 10px rgba(27,27,47,0.18))" }}
          />
        )
      )}
    </div>
  );
}
