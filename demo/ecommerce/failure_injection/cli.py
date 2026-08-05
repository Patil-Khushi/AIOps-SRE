"""Command-line interface for the failure-injection toolkit."""
from __future__ import annotations

import argparse
import sys

from . import FAILURES
from . import _load
from . import _orchestrator


def _print_table(show_layers: bool = False) -> None:
    """Print all failures grouped by service.

    Groups explicitly rather than breaking on "service changed since the previous
    row": registration order interleaves app-layer and infra-only failures, so
    the running-`current` approach printed each service header more than once.
    """
    width = max(len(k) for k in FAILURES)
    by_service: dict[str, list] = {}
    for key, f in FAILURES.items():
        by_service.setdefault(key.split(".")[0], []).append((key, f))

    for svc in sorted(by_service):
        print(f"\n{svc}:")
        for key, f in by_service[svc]:
            layer_str = f"  [{f.layer.value}]" if show_layers else ""
            print(f"  {key.ljust(width)}  {f.title}{layer_str}")
    print()


_STATUS_GLYPH = {"ran": "OK", "skipped": "--", "unavailable": "!!", "error": "XX"}


def _print_layers(result: dict) -> None:
    """Render per-layer outcome. 'unavailable' is distinct from 'error'."""
    print(f"  mode: {result['mode']}  (failure declares {result['declared_layer']})")
    for layer, step in result["layers"].items():
        glyph = _STATUS_GLYPH.get(step["status"], "??")
        detail = step.get("error") or step.get("reason") or ""
        # Multi-line remedies (the CAP_NET_ADMIN hint) collapse to their first line.
        if detail:
            detail = f" — {detail.splitlines()[0]}"
        print(f"    [{glyph}] {layer}: {step['status']}{detail}")
    if result.get("degraded"):
        print("  note: injected, but one layer was unavailable in this environment")


def _resolve(key: str):
    if key not in FAILURES:
        print(f"unknown failure '{key}'. Run `list` to see all keys.", file=sys.stderr)
        sys.exit(2)
    return FAILURES[key]


def _run_load(failure, duration: float) -> None:
    hint = failure.load
    if not hint:
        print("(no load hint for this failure; observe a single request instead)")
        return
    print(f"driving load: {hint.method} {hint.url} for {duration}s ...")
    counts = _load.generate(hint.url, hint.method, hint.body, duration=duration, concurrency=6)
    pretty = ", ".join(f"{code or 'conn-err'}={n}" for code, n in sorted(counts.items()))
    print(f"status distribution: {pretty}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="failure_injection")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list all failures")
    p_list.add_argument("--show-layers", action="store_true",
                        help="show injection layer for each failure")

    p_inject = sub.add_parser("inject", help="inject a failure")
    p_inject.add_argument("key")
    p_inject.add_argument("--mode", choices=["application", "infrastructure", "hybrid"],
                          default=None, help="injection mode (default: FI_MODE env or 'hybrid')")
    p_inject.add_argument("--load", type=float, metavar="SECONDS", default=0,
                          help="after injecting, drive traffic for N seconds")

    p_recover = sub.add_parser("recover", help="recover a failure")
    p_recover.add_argument("key")
    p_recover.add_argument("--mode", choices=["application", "infrastructure", "hybrid"],
                           default=None, help="recovery mode (default: FI_MODE env or 'hybrid')")

    p_sig = sub.add_parser("signals", help="print L1/L2/RCA for a failure")
    p_sig.add_argument("key")

    p_load = sub.add_parser("load", help="drive traffic at a URL")
    p_load.add_argument("--url", required=True)
    p_load.add_argument("--method", default="GET")
    p_load.add_argument("--duration", type=float, default=20)
    p_load.add_argument("--concurrency", type=int, default=6)

    args = parser.parse_args(argv)

    if args.cmd == "list":
        show_layers = getattr(args, "show_layers", False)
        _print_table(show_layers=show_layers)
    elif args.cmd == "inject":
        f = _resolve(args.key)
        mode = args.mode or None
        print(f"injecting: {f.key} — {f.title} (layer: {f.layer.value})")
        result = _orchestrator.inject(f, mode=mode)
        if result["ok"]:
            _print_layers(result)
        else:
            print(f"  FAILED: {result}")
            return 1
        if args.load:
            _run_load(f, args.load)
        print(f"done. recover with: python -m failure_injection recover {f.key}")
    elif args.cmd == "recover":
        f = _resolve(args.key)
        mode = args.mode or None
        print(f"recovering: {f.key}")
        result = _orchestrator.recover(f, mode=mode)
        if result["ok"]:
            _print_layers(result)
        else:
            print(f"  FAILED: {result}")
            return 1
        print("done.")
    elif args.cmd == "signals":
        f = _resolve(args.key)
        print(f"{f.key} — {f.title}\n  L1:  {f.l1}\n  L2:  {f.l2}\n  RCA: {f.rca}")
    elif args.cmd == "load":
        counts = _load.generate(args.url, args.method, None, args.duration, args.concurrency)
        print({k or "conn-err": v for k, v in counts.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())