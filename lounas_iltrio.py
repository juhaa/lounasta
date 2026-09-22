#!/usr/bin/env python3
"""Fetch the lunch menu of il Trio (https://iltrio.fi/lounas/).

A WordPress page with no API. The week is written straight into the page: a
bold heading per weekday ("TIISTAI 22.9." + "KLO 11.00-15.00"), then one
paragraph per dish whose bold first line is the name and price and whose
remaining lines are the ingredients.

Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.error

from lounas_common import (
    WEEKDAYS,
    cache_path,
    html_blocks,
    matches,
    read_cache,
    request_text,
    resolve_date,
    write_cache,
)

URL = "https://iltrio.fi/lounas/"

DAY_RE = re.compile(
    r"^(maanantai|tiistai|keskiviikko|torstai|perjantai|lauantai|sunnuntai)\b"
    r"\s*(?:(\d{1,2})\.(\d{1,2})\.?)?\s*(.*)$",
    re.I,
)
DAY_INDEX = {
    "maanantai": 0, "tiistai": 1, "keskiviikko": 2, "torstai": 3,
    "perjantai": 4, "lauantai": 5, "sunnuntai": 6,
}

HOURS_RE = re.compile(r"^klo\b|^\d{1,2}[.:]\d{2}\s*[-–]", re.I)
CLOSED_RE = re.compile(r"^(suljettu|closed)\b", re.I)
PRICE_RE = re.compile(r"(\d{1,3},\d{2})\s*(?:€|e)?\s*$")
DIET_CODES = {"L", "VL", "M", "G", "V", "VEG", "MU", "A"}
DIET_RE = re.compile(r"[\s,]((?:(?:%s)[\s,.]*)+)$" % "|".join(sorted(DIET_CODES, key=len, reverse=True)), re.I)

# The week ends at the diet legend; the page footer follows it.
END_RE = re.compile(r"=\s*(vegaani|vähälaktoosinen)|lounaan hintaan|aukioloajat", re.I)
FOOTER_RE = re.compile(r"©|copyright|rekisteriseloste", re.I)


# --------------------------------------------------------------------------- fetch


def fetch_page(url: str, *, use_cache: bool = True) -> str:
    path = cache_path("iltrio", url)
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            return cached
    page = request_text(url)
    write_cache(path, page)
    return page


# --------------------------------------------------------------------------- parse


def split_dish(text: str) -> tuple[str, list[str], str | None]:
    """Split "POLLO AL PARMIGIANO 15,90" into name, diet codes and price."""
    price = None
    price_match = PRICE_RE.search(text)
    if price_match:
        price = price_match.group(1)
        text = text[: price_match.start()].strip(" ,.:")

    diets: list[str] = []
    while True:
        diet_match = DIET_RE.search(text)
        if not diet_match:
            break
        found = [c.upper() for c in re.split(r"[\s,.]+", diet_match.group(1)) if c.upper() in DIET_CODES]
        if not found:
            break
        diets = found + diets
        text = text[: diet_match.start()].strip(" ,.:")

    return text.strip(" ,.:"), diets, price


def parse_week(blocks: list[dict], today: dt.date) -> tuple[list[dict], list[str]]:
    """Return the weekday menus and the notes printed under them."""
    days: list[dict] = []
    notes: list[str] = []
    current: dict | None = None
    dish: dict | None = None
    finished = False

    for block in blocks:
        line = block["text"]
        if set(line) <= {"*", "-", "_", " "}:
            continue
        if FOOTER_RE.search(line):
            break
        if finished:
            if line not in notes:  # the page repeats its opening hours
                notes.append(line)
            continue

        day_match = DAY_RE.match(line)
        if day_match and block["bold"]:
            current = {
                "weekday": DAY_INDEX[day_match.group(1).lower()],
                "heading": line,
                "date": heading_date(day_match, today),
                "hours": None,
                "closed": False,
                "dishes": [],
            }
            days.append(current)
            dish = None
            rest = (day_match.group(4) or "").strip()
            if rest and HOURS_RE.match(rest):
                current["hours"] = rest
            continue

        if current is None:
            continue  # page chrome above the menu

        if HOURS_RE.match(line):
            current["hours"] = line
            continue
        if CLOSED_RE.match(line):
            current["closed"] = True
            continue
        if END_RE.search(line):
            finished = True
            notes.append(line)
            continue

        if block["bold"]:
            name, diets, price = split_dish(line)
            if name:
                dish = {"name": name, "diets": diets, "price": price, "description": ""}
                current["dishes"].append(dish)
            continue

        if dish is not None:
            # Ingredient lines wrap across several <br>-separated lines; keep
            # their punctuation, only lift diet codes off the end.
            text = line.strip()
            diet_match = DIET_RE.search(text)
            if diet_match:
                found = [c.upper() for c in re.split(r"[\s,.]+", diet_match.group(1))
                         if c.upper() in DIET_CODES]
                if found:
                    if not dish["diets"]:
                        dish["diets"] = found
                    text = text[: diet_match.start()].strip()
            if text:
                dish["description"] = f"{dish['description']} {text}".strip()

    fill_missing_dates(days, today)
    return days, notes


def heading_date(day_match: re.Match, today: dt.date) -> str | None:
    day, month = day_match.group(2), day_match.group(3)
    if not (day and month):
        return None
    date = dt.date(today.year, int(month), int(day))
    if (date - today).days < -180:
        date = date.replace(year=today.year + 1)
    return date.isoformat()


def fill_missing_dates(days: list[dict], today: dt.date) -> None:
    known = [(d["weekday"], dt.date.fromisoformat(d["date"])) for d in days if d["date"]]
    if known:
        weekday, date = known[0]
        monday = date - dt.timedelta(days=weekday)
    else:
        monday = today - dt.timedelta(days=today.weekday())
    for day in days:
        if not day["date"]:
            day["date"] = (monday + dt.timedelta(days=day["weekday"])).isoformat()


# --------------------------------------------------------------------------- print


def render(days: list[dict], notes: list[str], args, parsed_any: bool) -> str:
    lines = ["il Trio", "======="]
    if not days:
        lines.append("  (no menu for that date)" if parsed_any else "  (no menu parsed)")
        return "\n".join(lines) + "\n"

    for day in days:
        date = dt.date.fromisoformat(day["date"])
        label = f"{WEEKDAYS['fi'][day['weekday']]} {date:%d.%m.%Y}"
        if day["hours"]:
            label += f" ({day['hours'].lower()})"
        lines.append(f"-- {label}")
        if day["closed"] and not day["dishes"]:
            lines.append("   (suljettu)")
            lines.append("")
            continue

        for dish in day["dishes"]:
            tags = "/".join(dish["diets"])
            suffix = "  ".join(part for part in (f"[{tags}]" if tags else "",
                                                 f"{dish['price']} €" if dish["price"] else "") if part)
            lines.append(f"   * {dish['name']}" + (f"   {suffix}" if suffix else ""))
            if args.verbose and dish["description"]:
                lines.append(f"       {dish['description']}")
        lines.append("")

    if args.verbose and notes:
        lines.extend(notes)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------- aggregator


def menu_for(date, *, url=URL, use_cache=True) -> list[dict]:
    """Normalised il Trio menu for one date (for lounasta_web)."""
    blocks = html_blocks(fetch_page(url, use_cache=use_cache), tags="h[1-6]|p")
    days, _notes = parse_week(blocks, date)
    day = next((d for d in days if d["date"] == date.isoformat()), None)

    dishes = [
        {
            "name": dish["name"],
            "diets": dish["diets"],
            "price": f"{dish['price']} €" if dish["price"] else None,
            "description": dish["description"],
        }
        for dish in (day or {}).get("dishes", [])
    ]
    return [
        {
            "id": "iltrio",
            "name": "il Trio",
            "subtitle": "Ristorante",
            "url": url,
            "hours": (day or {}).get("hours"),
            "note": "Suljettu" if (day or {}).get("closed") and not dishes else None,
            "sections": [{"name": None, "dishes": dishes}] if dishes else [],
        }
    ]

# ---------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch the il Trio lunch menu (iltrio.fi/lounas).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  lounas_iltrio.py                 today\n"
            "  lounas_iltrio.py -d all -v       the whole week with ingredients\n"
            "  lounas_iltrio.py -q pizza -d all search the week's dishes\n"
            "  lounas_iltrio.py --text          the page's text blocks\n"
            "  lounas_iltrio.py --json | jq .   machine-readable output\n"
        ),
    )
    parser.add_argument("-d", "--date", default=None,
                        help="today (default), tomorrow, a weekday, 2026-09-23, 23.09., or 'all'")
    parser.add_argument("-q", "--query", action="append", default=[],
                        help="only dishes whose name or ingredients match (repeatable)")
    parser.add_argument("--diet", action="append", default=[],
                        help="diet code, e.g. V, G, L (repeatable)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show ingredients and the notes under the menu")
    parser.add_argument("--url", default=URL, help=f"page to read (default: {URL})")
    parser.add_argument("--json", action="store_true", help="output JSON instead of text")
    parser.add_argument("--text", action="store_true", help="print the page's text blocks")
    parser.add_argument("--no-cache", action="store_true", help="bypass the local cache")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    today = dt.date.today()

    try:
        page = fetch_page(args.url, use_cache=not args.no_cache)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: cannot fetch page: {exc}", file=sys.stderr)
        return 1

    blocks = html_blocks(page, tags="h[1-6]|p")
    if args.text:
        for block in blocks:
            print(block["text"])
        return 0

    days, notes = parse_week(blocks, today)
    parsed_any = bool(days)

    try:
        wanted_date = resolve_date(args.date, today)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if wanted_date is not None:
        days = [day for day in days if day["date"] == wanted_date.isoformat()]

    if args.query:
        for day in days:
            day["dishes"] = [
                d for d in day["dishes"]
                if matches(d["name"], args.query) or matches(d["description"], args.query)
            ]
    if args.diet:
        wanted = [code.lower() for code in args.diet]
        for day in days:
            day["dishes"] = [d for d in day["dishes"]
                             if any(c.lower() in wanted for c in d["diets"])]
    if args.query or args.diet:
        days = [day for day in days if day["dishes"]]

    if args.json:
        json.dump({"days": days, "notes": notes}, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        sys.stdout.write(render(days, notes, args, parsed_any))
    return 0


if __name__ == "__main__":
    sys.exit(main())
