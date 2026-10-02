#!/usr/bin/env python3
"""Check a built page before it is published.

`lounasta_web.py --build` always writes a page: a source that is down gets an
error card and the rest of the page renders around it. That is the right
behaviour for a reader and the wrong one for a deploy, which would happily
publish a page that quietly lost a restaurant weeks ago. This script reads the
built HTML back and fails if something is missing:

  * every expected card is there          (--expect, default 7)
  * no card is showing an error           (--allow-errors to permit some)
  * at least one card actually has a menu (a date bug empties all of them)

A card with no dishes is not a failure on its own: restaurants close on
Mondays. At a weekend they all do, and UniCafe drops its cards from the page
altogether rather than showing them empty, so on a Saturday or a Sunday —
read from the page's own date — neither the count nor the emptiness is
checked, and only the errors are.

    ./check_page.py site/index.html
    ./check_page.py site/index.html --expect 7 --allow-errors 1

Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sys

CARD = re.compile(r'<section class="card">(.*?)</section>', re.S)
NAME = re.compile(r"<h2>(?:<a[^>]*>)?(.*?)(?:</a>)?</h2>", re.S)
ERROR = re.compile(r'<p class="error">(.*?)</p>', re.S)
EMPTY = re.compile(r'<p class="empty">')
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def text(raw: str) -> str:
    """The visible text of a fragment, tags and entities resolved."""
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def inspect(page: str) -> tuple[str, list[dict]]:
    title = text(TITLE.search(page).group(1)) if TITLE.search(page) else ""
    cards = []
    for body in CARD.findall(page):
        name = NAME.search(body)
        error = ERROR.search(body)
        cards.append(
            {
                "name": text(name.group(1)) if name else "(nimetön)",
                "error": text(error.group(1)) if error else None,
                "empty": bool(EMPTY.search(body)),
            }
        )
    return title, cards


def page_date(title: str) -> dt.date | None:
    """The day the page is for, read back out of its own title."""
    found = DATE.search(title)
    if not found:
        return None
    day, month, year = (int(part) for part in found.groups())
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def check(page: str, *, expect: int, allow_errors: int) -> list[str]:
    """Every complaint about the page, empty if it is fit to publish."""
    title, cards = inspect(page)
    failures = []

    if not title:
        failures.append("the page has no <title>")

    # At a weekend every restaurant is closed and UniCafe leaves the page
    # rather than showing an empty card, so a short page with no menus on it
    # is the correct answer and not something to withhold. A page whose date
    # cannot be read is treated as a weekday, which is the stricter reading.
    date = page_date(title)
    weekend = date is not None and date.weekday() >= 5

    if weekend:
        if not cards:
            failures.append("the page has no cards at all")
    else:
        if len(cards) < expect:
            failures.append(f"{len(cards)} cards, expected at least {expect}")
        if cards and all(c["empty"] or c["error"] for c in cards):
            failures.append("no card has a menu at all")

    broken = [c for c in cards if c["error"]]
    if len(broken) > allow_errors:
        for card in broken:
            failures.append(f"{card['name']}: {card['error']}")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail if a built page is not fit to publish.")
    parser.add_argument("path", help="the built HTML file")
    parser.add_argument("--expect", type=int, default=7,
                        help="how many cards the page must have (default: 7)")
    parser.add_argument("--allow-errors", type=int, default=0, metavar="N",
                        help="tolerate up to N failed sources (default: 0)")
    args = parser.parse_args(argv)

    with open(args.path, encoding="utf-8") as fh:
        page = fh.read()

    title, cards = inspect(page)
    date = page_date(title)
    when = " (viikonloppu)" if date and date.weekday() >= 5 else ""
    print(f"{title or '(no title)'} — {len(cards)} cards{when}")
    for card in cards:
        state = card["error"] or ("ei lounasta" if card["empty"] else "ok")
        print(f"  {'FAIL' if card['error'] else '    '} {card['name']}: {state}")

    failures = check(page, expect=args.expect, allow_errors=args.allow_errors)
    if failures:
        print("\nnot publishable:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("\nok to publish")
    return 0


if __name__ == "__main__":
    sys.exit(main())
