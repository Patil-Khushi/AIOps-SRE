// The signature Orbit element: a domed-arc stepper, used for checkout
// progress and for order status. Deliberately not a linear breadcrumb —
// the arc is the point of differentiation.

const VB_W = 720;
const VB_H = 150;
const CX = 360;
const CY = 118;
const RX = 330;
const RY = 82;

function nodePoints(n) {
  return Array.from({ length: n }, (_, i) => {
    const deg = 180 - i * (180 / (n - 1));
    const rad = (deg * Math.PI) / 180;
    return { x: CX + RX * Math.cos(rad), y: CY - RY * Math.sin(rad) };
  });
}

/**
 * @param steps    array of labels
 * @param current  index of the active step
 * @param states   optional per-node override: 'done' | 'active' | 'error' | 'todo' | 'disabled'
 */
export default function OrbitTracker({ steps, current = 0, states }) {
  const n = steps.length;
  const pts = nodePoints(n);

  const stateOf = (i) => {
    if (states?.[i]) return states[i];
    if (i < current) return "done";
    if (i === current) return "active";
    return "todo";
  };

  const fillFor = { done: "#2F6F5E", active: "#FF5A36", error: "#FF5A36", todo: "#FFFFFF", disabled: "#FFFFFF" };
  const strokeFor = { done: "#2F6F5E", active: "#FF5A36", error: "#E14D26", todo: "#D9D5CB", disabled: "#E5E1D8" };
  const labelFor = { done: "#2F6F5E", active: "#FF5A36", error: "#E14D26", todo: "#6B7280", disabled: "#B9B4A8" };

  // Progress line stops at the last node that is done or active.
  const progressEnd = pts.slice(0, Math.max(0, current) + 1);

  return (
    <div className="w-full">
      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="h-auto w-full" style={{ maxHeight: 130 }}>
        <path
          d={pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ")}
          fill="none"
          stroke="#E5E1D8"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        {progressEnd.length > 1 && (
          <path
            d={progressEnd.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ")}
            fill="none"
            stroke="#2F6F5E"
            strokeWidth="3"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        )}
        {pts.map((p, i) => {
          const st = stateOf(i);
          return (
            <g key={i}>
              {(st === "active" || st === "error") && (
                <circle cx={p.x} cy={p.y} r="16" fill="none" stroke="#FF5A36" strokeOpacity="0.25" strokeWidth="6" />
              )}
              <circle
                cx={p.x}
                cy={p.y}
                r="10"
                fill={fillFor[st]}
                stroke={strokeFor[st]}
                strokeWidth="2"
                strokeDasharray={st === "disabled" ? "3 3" : undefined}
              />
              {st === "done" && (
                <path
                  d={`M ${p.x - 4} ${p.y} L ${p.x - 1} ${p.y + 3} L ${p.x + 4.5} ${p.y - 4}`}
                  stroke="#fff"
                  strokeWidth="2"
                  fill="none"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              )}
              {st === "error" && (
                <path
                  d={`M ${p.x - 3.5} ${p.y - 3.5} L ${p.x + 3.5} ${p.y + 3.5} M ${p.x + 3.5} ${p.y - 3.5} L ${p.x - 3.5} ${p.y + 3.5}`}
                  stroke="#fff"
                  strokeWidth="2"
                  strokeLinecap="round"
                />
              )}
            </g>
          );
        })}
      </svg>

      {/*
        ⚠️ Labels are positioned absolutely, NOT with flex justify-between.
        The node x-positions (30, 195, 525, 690 for n=4) are deliberately
        uneven — that unevenness is what makes the arc read as a dome. A
        justify-between row would space them 0/240/480/720 and the two middle
        labels would miss their nodes by ~45px. Percentages keep them pinned
        at every viewport width, while the text itself stays a fixed size
        (unlike SVG <text>, which scales with the viewBox and goes illegible
        on mobile).
      */}
      <div className="relative -mt-1 h-8">
        {pts.map((p, i) => (
          <span
            key={steps[i]}
            className="mono absolute -translate-x-1/2 whitespace-nowrap text-[11px] uppercase tracking-wider"
            style={{ left: `${(p.x / VB_W) * 100}%`, color: labelFor[stateOf(i)] }}
          >
            {steps[i]}
          </span>
        ))}
      </div>
    </div>
  );
}
