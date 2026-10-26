#!/usr/bin/env python3
"""Check every HTTP(S) link in the project's Markdown documents.

Usage:
    python scripts/check_links.py
    python scripts/check_links.py README.md --timeout 5
    STOCKSENATE_SKIP_LINK_CHECK=1 python scripts/check_links.py

The check follows redirects and accepts any final 2xx response. Hosts that
reject HEAD are retried with GET. Exit code is 0 when every link resolves (or
when the check is skipped with --offline / STOCKSENATE_SKIP_LINK_CHECK=1), and
1 when a file is missing, a link is broken, or a request cannot be completed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = ROOT / "README.md"
URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
TRAILING_PUNCTUATION = ".,;:!?、。；：）)]}"
USER_AGENT = "StockSenate-link-check/1.0 (+https://github.com/QuotaFuse-ai/StockSenate)"


def extract_links(path: Path) -> list[str]:
    """Return unique HTTP(S) URLs in file order."""
    links: list[str] = []
    for match in URL_PATTERN.findall(path.read_text(encoding="utf-8")):
        link = match.rstrip(TRAILING_PUNCTUATION)
        if link and link not in links:
            links.append(link)
    return links


def _fetch(url: str, timeout: float, method: str) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        method=method,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), response.geturl()


def probe(url: str, timeout: float) -> tuple[bool, str]:
    """Return (ok, detail) for one URL; redirects are followed."""
    target = url.split("#", 1)[0]
    last_error = "no response"
    for method in ("HEAD", "GET"):
        try:
            status, final_url = _fetch(target, timeout, method)
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code} {error.reason}"
            if method == "HEAD" and error.code in {403, 405, 501}:
                continue
            return False, last_error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            return False, f"unreachable: {reason}"
        if 200 <= status < 400:
            detail = str(status) if final_url == target else f"{status} -> {final_url}"
            return True, detail
        last_error = f"HTTP {status}"
    return False, last_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check HTTP(S) links in Markdown documents.")
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help=f"Markdown files to scan (default: {DEFAULT_TARGET.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="Per-request timeout in seconds"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not make network requests; report the links that would be checked",
    )
    args = parser.parse_args(argv)

    targets = args.files or [DEFAULT_TARGET]
    links: list[str] = []
    for path in targets:
        if not path.is_file():
            print(f"MISSING FILE {path}")
            return 1
        found = extract_links(path)
        print(f"{path}: {len(found)} link(s)")
        links.extend(link for link in found if link not in links)

    skipped_offline = args.offline or os.environ.get("STOCKSENATE_SKIP_LINK_CHECK") == "1"
    if skipped_offline:
        print(f"offline mode: skipped {len(links)} link(s), no network request made")
        return 0

    broken: list[str] = []
    for link in links:
        ok, detail = probe(link, args.timeout)
        print(f"  {'OK  ' if ok else 'FAIL'} {detail:<40} {link}")
        if not ok:
            broken.append(link)

    print(f"checked {len(links)} link(s): {len(links) - len(broken)} ok, {len(broken)} broken")
    if broken:
        for link in broken:
            print(f"broken: {link}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
