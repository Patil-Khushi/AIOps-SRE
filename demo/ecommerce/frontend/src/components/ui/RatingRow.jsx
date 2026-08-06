import { Star } from "lucide-react";
import { count } from "../../lib/format.js";

export default function RatingRow({ rating, reviews }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="mono flex items-center gap-0.5 rounded bg-pine px-1.5 py-0.5 text-[11px] text-white">
        {rating}
        <Star size={10} fill="#fff" strokeWidth={0} />
      </span>
      <span className="mono text-xs text-muted">({count(reviews)})</span>
    </div>
  );
}
