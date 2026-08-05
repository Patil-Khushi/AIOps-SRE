// Frontend-only product catalog.
//
// There is no product service in this stack — order-service accepts an
// arbitrary `sku` string and never validates it against anything. So the
// catalog is a static module, and `id` becomes the `sku` we POST verbatim.
// Slug ids (not integers) are deliberate: they stay greppable in Postgres
// rows and Loki log lines during a demo.
//
// `image` points at a bundled CC0 / public-domain photo in public/products/
// (see public/products/CREDITS.md). Drop a replacement file at the same path
// to swap any product's photo — no code change needed.
//
// `icon` is the FALLBACK, used when `image` is absent or the file fails to
// load. It holds an actual lucide component reference rather than a name
// string, so <p.icon size={72}/> works and tree-shaking survives. The
// consequence is that catalog objects are NOT JSON-serialisable — which is
// why the cart stores only { id, qty } and re-derives everything else.

import {
  Activity,
  Backpack,
  Camera,
  Footprints,
  Gamepad2,
  Headphones,
  Lamp,
  Speaker,
  Watch,
} from "lucide-react";

export const CATALOG = [
  {
    id: "pulse-wireless-earbuds",
    image: "/products/pulse-wireless-earbuds.jpg",
    name: "Pulse Wireless Earbuds",
    category: "Audio",
    price: 2499,
    mrp: 3999,
    rating: 4.3,
    reviews: 1284,
    icon: Headphones,
    tile: "#FF5A36",
    desc: "True wireless earbuds with active noise cancellation and 32-hour battery life across the case.",
  },
  {
    id: "aria-over-ear-headphones",
    image: "/products/aria-over-ear-headphones.jpg",
    name: "Aria Over-Ear Headphones",
    category: "Audio",
    price: 5999,
    mrp: 7499,
    rating: 4.5,
    reviews: 862,
    icon: Headphones,
    tile: "#2F6F5E",
    desc: "Studio-tuned over-ear headphones with plush memory-foam cushions for all-day listening.",
  },
  {
    id: "flux-smartwatch",
    image: "/products/flux-smartwatch.jpg",
    name: "Flux Smartwatch",
    category: "Wearables",
    price: 3299,
    mrp: 4599,
    rating: 4.1,
    reviews: 2043,
    icon: Watch,
    tile: "#1B1B2F",
    desc: "AMOLED smartwatch with 14-day battery, sleep tracking, and 100+ workout modes.",
  },
  {
    id: "trail-runner-sneakers",
    image: "/products/trail-runner-sneakers.jpg",
    name: "Trail Runner Sneakers",
    category: "Footwear",
    price: 2899,
    mrp: 3599,
    rating: 4.4,
    reviews: 731,
    icon: Footprints,
    tile: "#FFC857",
    desc: "Lightweight trail sneakers with responsive cushioning and a grippy multi-terrain sole.",
  },
  {
    id: "nimbus-canvas-backpack",
    image: "/products/nimbus-canvas-backpack.jpg",
    name: "Nimbus Canvas Backpack",
    category: "Bags",
    price: 1799,
    mrp: 2299,
    rating: 4.2,
    reviews: 495,
    icon: Backpack,
    tile: "#6B7280",
    desc: "Water-resistant canvas backpack with a padded 15-inch laptop sleeve and hidden pocket.",
  },
  {
    id: "lumen-desk-lamp",
    image: "/products/lumen-desk-lamp.jpg",
    name: "Lumen Desk Lamp",
    category: "Home",
    price: 1299,
    mrp: 1699,
    rating: 4.0,
    reviews: 318,
    icon: Lamp,
    tile: "#FF5A36",
    desc: "Adjustable LED desk lamp with three warmth settings and a USB charging port at the base.",
  },
  {
    id: "zoom-action-camera",
    image: "/products/zoom-action-camera.jpg",
    name: "Zoom Action Camera",
    category: "Cameras",
    price: 6499,
    mrp: 8999,
    rating: 4.6,
    reviews: 176,
    icon: Camera,
    tile: "#1B1B2F",
    desc: "4K action camera with in-body stabilization, waterproof to 10m without a housing.",
  },
  {
    id: "nova-gaming-mouse",
    image: "/products/nova-gaming-mouse.jpg",
    name: "Nova Gaming Mouse",
    category: "Gaming",
    price: 1499,
    mrp: 1999,
    rating: 4.3,
    reviews: 902,
    icon: Gamepad2,
    tile: "#2F6F5E",
    desc: "Ultra-light gaming mouse with a 26k DPI sensor and swappable side grips.",
  },
  {
    id: "pulse-fitness-band",
    image: "/products/pulse-fitness-band.jpg",
    name: "Pulse Fitness Band",
    category: "Fitness",
    price: 1999,
    mrp: 2599,
    rating: 4.1,
    reviews: 1567,
    icon: Activity,
    tile: "#FFC857",
    desc: "Slim fitness band with continuous heart-rate tracking and 10-day battery life.",
  },
  {
    id: "echo-bluetooth-speaker",
    image: "/products/echo-bluetooth-speaker.jpg",
    name: "Echo Bluetooth Speaker",
    category: "Audio",
    price: 2299,
    mrp: 2999,
    rating: 4.4,
    reviews: 654,
    icon: Speaker,
    tile: "#2F6F5E",
    desc: "Portable speaker with 360-degree sound and an IP67 rating for outdoor use.",
  },
];

export const CATEGORIES = ["All", ...Array.from(new Set(CATALOG.map((p) => p.category)))];

const byId = new Map(CATALOG.map((p) => [p.id, p]));

/** Look up a product by id/sku. Returns null for unknown ids — orders placed
 *  by the previous UI carry skus like "widget" that are not in this catalog. */
export function getProduct(id) {
  return byId.get(String(id)) ?? null;
}

/** Free delivery threshold, in rupees. Display only — never sent. */
export const FREE_DELIVERY_ABOVE = 2999;
export const DELIVERY_FEE = 99;
