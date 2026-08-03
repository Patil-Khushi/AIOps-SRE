"""Command-line interface for the failure-injection toolkit."""
from __future__ import annotations

import argparse
import sys

from . import FAILURES
from . import _load


def _print_table() -> None:
    width = max(len(k) for k in FAILURES)
    current = None
    for key, f in FAILURES.items():
        svc = key.split(".")[0]
        if svc != current:
            current = svc
            print(f"\n{svc}:")
        print(f"  {key.ljust(width)}  {f.title}")
    print()


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

    sub.add_parser("list", help="list all failures")

    p_inject = sub.add_parser("inject", help="inject a failure")
    p_inject.add_argument("key")
    p_inject.add_argument("--load", type=float, metavar="SECONDS", default=0,
                          help="after injecting, drive traffic for N seconds")

    p_recover = sub.add_parser("recover", help="recover a failure")
    p_recover.add_argument("key")

    p_sig = sub.add_parser("signals", help="print L1/L2/RCA for a failure")
    p_sig.add_argument("key")

    p_load = sub.add_parser("load", help="drive traffic at a URL")
    p_load.add_argument("--url", required=True)
    p_load.add_argument("--method", default="GET")
    p_load.add_argument("--duration", type=float, default=20)
    p_load.add_argument("--concurrency", type=int, default=6)

    args = parser.parse_args(argv)

    if args.cmd == "list":
        _print_table()
    elif args.cmd == "inject":
        f = _resolve(args.key)
        print(f"injecting: {f.key} — {f.title}")
        f.inject()
        if args.load:
            _run_load(f, args.load)
        print("done. recover with: "
              f"python -m failure_injection recover {f.key}")
    elif args.cmd == "recover":
        f = _resolve(args.key)
        print(f"recovering: {f.key}")
        f.recover()
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