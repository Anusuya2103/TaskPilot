#!/usr/bin/env python
"""Enforce the coverage gate on ``coverage.xml``.

Why this is a script and not a heredoc in the workflow: the previous inline
version had two bugs that made it decorative.

1. It ended in ``sys.exit(0)``. It printed numbers and then always passed, so the
   gate could never fail a build.
2. It matched packages with ``filename.startswith("app/agent")``, but coverage
   writes paths *relative to the source root* (``source = ["app"]``), so every
   filename is ``agent/executor.py``. The prefix never matched, both packages
   reported "no data", and the loop exited without checking anything.

Together those meant the "80% on the agent and tool layers" requirement was never
actually enforced, and the real numbers were never surfaced.

Usage::

    python scripts/coverage_gate.py --xml coverage.xml \\
        --package app.agent:80 --package app.tools:80

Exits non-zero when a threshold is missed, so CI fails loudly.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path


def _candidates(package: str) -> list[str]:
    """Path prefixes that may identify ``package`` inside coverage.xml.

    Coverage records paths *relative to the configured source root*
    (``source = ["app"]``), so the package ``app.tools.registry`` is written as
    ``tools/registry.py`` — the leading ``app/`` is already stripped. The old
    inline gate compared against ``app/agent``, which never matched anything.

    We therefore try the fully-qualified form and the root-relative form, so the
    script keeps working if ``source`` is ever widened to the repo root.
    """
    tail = package.replace(".", "/").strip("/")
    out = [f"{tail}/", tail]
    _, _, rest = tail.partition("/")
    if rest:
        out += [f"{rest}/", rest]
    return out


def _package_path(filename: str, package: str) -> str:
    """Return the matching path prefix for ``package``, or "" if it is elsewhere."""
    for prefix in _candidates(package):
        if prefix and filename.startswith(prefix):
            return prefix
    return ""


def package_coverage(root: ET.Element, package: str) -> tuple[int, int]:
    """Return ``(hit, total)`` statement lines for one package.

    A file is counted once even if it somehow matched twice.
    """
    hit = total = 0
    seen: set[str] = set()
    for cls in root.iter("class"):
        filename = cls.attrib.get("filename", "")
        if filename in seen or not _package_path(filename, package):
            continue
        seen.add(filename)
        for line in cls.iter("line"):
            total += 1
            hit += int(line.attrib.get("hits", "0")) > 0
    return hit, total


def parse_threshold(spec: str) -> tuple[str, float]:
    """Parse a ``package:pct`` argument."""
    package, _, pct = spec.rpartition(":")
    if not package or not pct:
        raise argparse.ArgumentTypeError(f"expected package:pct, got {spec!r}")
    try:
        return package, float(pct)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"bad percentage in {spec!r}") from exc


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", default="coverage.xml", help="path to coverage.xml")
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        metavar="PKG:PCT",
        help="minimum line coverage for a package; repeatable",
    )
    parser.add_argument(
        "--total",
        type=float,
        default=None,
        help="minimum total line coverage percentage",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    xml_path = Path(args.xml)
    if not xml_path.exists():
        print(f"coverage gate: {xml_path} not found", file=sys.stderr)
        return 2

    # coverage.xml is written by our own pytest run a moment earlier, so the
    # untrusted-input concern behind S314 does not apply here.
    root = ET.parse(xml_path).getroot()  # noqa: S314
    total_rate = float(root.attrib.get("line-rate", 0)) * 100
    print(f"total line coverage: {total_rate:.1f}%")

    failures: list[str] = []
    for spec in args.package:
        package, minimum = parse_threshold(spec)
        hit, total = package_coverage(root, package)
        if not total:
            # A silently-skipped package is how the old gate passed forever.
            failures.append(f"{package}: no coverage data found for this package")
            print(f"{package}: !! no coverage data found (check the source root)")
            continue
        pct = hit / total * 100
        verdict = "ok" if pct >= minimum else "FAIL"
        print(f"{package}: {pct:.1f}% ({hit}/{total}) need {minimum:.0f}% [{verdict}]")
        if pct < minimum:
            failures.append(f"{package}: {pct:.1f}% is below the {minimum:.0f}% floor")

    if args.total is not None:
        verdict = "ok" if total_rate >= args.total else "FAIL"
        print(f"total: {total_rate:.1f}% need {args.total:.0f}% [{verdict}]")
        if total_rate < args.total:
            failures.append(f"total: {total_rate:.1f}% is below the {args.total:.0f}% floor")

    if failures:
        print("\ncoverage gate FAILED:", file=sys.stderr)
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1

    print("coverage gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
