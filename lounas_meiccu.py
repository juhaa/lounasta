#!/usr/bin/env python3
"""Fetch the lunch menu of Meiccu (https://meiccu.fi/lounas/).

A WordPress/Elementor page with no API: the week's menu is plain HTML, one
heading per weekday ("Maanantai 21.9.", "Ti 22.9.") followed by section
headings ("Buffet 15,00", "Keittiöstä") and one paragraph per dish. The script
flattens the page to headings and paragraphs in document order and reads that
sequence.

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

URL = "https://meiccu.fi/lounas/"

DAY_RE = re.compile(
    r"^(maanantai|tiistai|keskiviikko|torstai|perjantai|lauantai|sunnuntai|ma|ti|ke|to|pe|la|su)\b"
    r"\s*\.?\s*(?:(\d{1,2})\.(\d{1,2})\.?)?\s*(.*)$",
    re.I,
)
DAY_INDEX = {
    "maanantai": 0, "ma": 0, "tiistai": 1, "ti": 1, "keskiviikko": 2, "ke": 2,
    "torstai": 3, "to": 3, "perjantai": 4, "pe": 4, "lauantai": 5, "la": 5,
    "sunnuntai": 6, "su": 6,
}

SECTION_RE = re.compile(r"^(buffet|keittiöstä|keittiosta|à la carte|a la carte|grillistä)\b", re.I)
PRICE_RE = re.compile(r"(\d{1,3},\d{2})\s*(?:€|e)?\s*$")
DIET_CODES = {"L", "VL", "M", "G", "V", "A", "VEG"}
DIET_RE = re.compile(r"[\s,]((?:(?:%s)[\s,.]*)+)$" % "|".join(sorted(DIET_CODES, key=len, reverse=True)))

# The weekday menus end where the served-to-table set menu begins, and the
# menu content itself ends at the disclaimer; the site footer follows.
END_RE = re.compile(r"tarjoiltu lounas|pöytiin|pidätämme oikeu", re.I)
LAST_RE = re.compile(r"pidätämme oikeu", re.I)


# --------------------------------------------------------------------------- fetch


def fetch_page(url: str, *, use_cache: bool = True) -> str:
    path = cache_path("meiccu", url)
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            return cached
    page = request_text(url)
    write_cache(path, page)
    return page


# --------------------------------------------------------------------------- parse


def split_dish(text: str) -> tuple[str, list[str], str | None]:
    """Split "Wieninleike L 20,50" into name, diet codes and price."""
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
        found = [c for c in re.split(r"[\s,.]+", diet_match.group(1)) if c in DIET_CODES]
        if not found:
            break
        diets = found + diets
        text = text[: diet_match.start()].strip(" ,.:")

    seen: list[str] = []
    for code in diets:  # the page repeats codes now and then
        if code not in seen:
            seen.append(code)
    return text.strip(" ,.:"), seen, price


def parse_week(blocks: list[tuple[str, str]], today: dt.date) -> tuple[list[dict], list[str], list[str]]:
    """Return the weekday menus, the notes above them and the trailing extras."""
    days: list[dict] = []
    notes: list[str] = []
    extras: list[str] = []
    current: dict | None = None
    section: str | None = None
    finished = False
    done = False

    for tag, line in blocks:
        if done:
            continue
        if finished:
            extras.append(line)
            done = bool(LAST_RE.search(line))
            continue
        if set(line) <= {"*", " "}:
            continue

        day_match = DAY_RE.match(line) if tag.startswith("h") else None
        if day_match and (day_match.group(2) or len(line) < 40):
            weekday = DAY_INDEX[day_match.group(1).lower()]
            day, month = day_match.group(2), day_match.group(3)
            date = None
            if day and month:
                year = today.year
                date = dt.date(year, int(month), int(day))
                if (date - today).days < -180:
                    date = date.replace(year=year + 1)
            current = {
                "weekday": weekday,
                "heading": line,
                "date": date.isoformat() if date else None,
                "note": (day_match.group(4) or "").strip(" .,-") or None,
                "dishes": [],
            }
            days.append(current)
            section = None
            continue

        if current is None:
            notes.append(line)
            continue

        if END_RE.search(line):
            finished = True
            extras.append(line)
            done = bool(LAST_RE.search(line))
            continue

        section_match = SECTION_RE.match(line)
        if section_match:
            # Section lines carry a price and, on occasion, stray diet codes
            # left over from editing; keep the name and the price only.
            price = re.search(r"\d{1,3},\d{2}", line)
            section = section_match.group(1).strip()
            if price:
                section = f"{section} {price.group()}"
            continue

        name, diets, price = split_dish(line)
        if name:
            current["dishes"].append(
                {"name": name, "diets": diets, "price": price, "section": section}
            )

    fill_missing_dates(days, today)
    return days, notes, extras


def fill_missing_dates(days: list[dict], today: dt.date) -> None:
    """Some headings carry no date ("Ke"); place them in the same week."""
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


def render(days: list[dict], notes: list[str], extras: list[str], args, parsed_any: bool) -> str:
    lines = ["Meiccu", "======"]
    if args.verbose and notes:
        lines.extend(notes)
        lines.append("")

    if not days:
        lines.append("  (no menu for that date)" if parsed_any else "  (no menu parsed)")
        return "\n".join(lines) + "\n"

    for day in days:
        date = dt.date.fromisoformat(day["date"])
        label = f"{WEEKDAYS['fi'][day['weekday']]} {date:%d.%m.%Y}"
        if day["note"]:
            label += f" ({day['note']})"
        lines.append(f"-- {label}")
        section = None
        for dish in day["dishes"]:
            if dish["section"] != section:
                section = dish["section"]
                if section:
                    lines.append(f"   {section}")
            tags = "/".join(dish["diets"])
            suffix = "  ".join(part for part in (f"[{tags}]" if tags else "",
                                                 f"{dish['price']} €" if dish["price"] else "") if part)
            lines.append(f"     * {dish['name']}" + (f"   {suffix}" if suffix else ""))
        lines.append("")

    if args.verbose and extras:
        lines.extend(extras)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------- aggregator


def menu_for(date, *, url=URL, use_cache=True) -> list[dict]:
    """Normalised Meiccu menu for one date (for lounasta_web)."""
    blocks = [(b["tag"], b["text"]) for b in html_blocks(fetch_page(url, use_cache=use_cache),
                                                         tags="h[1-6]|p")]
    days, notes, _extras = parse_week(blocks, date)
    day = next((d for d in days if d["date"] == date.isoformat()), None)

    sections: dict[str, list] = {}
    for dish in (day or {}).get("dishes", []):
        sections.setdefault(dish["section"] or "", []).append(
            {
                "name": dish["name"],
                "diets": dish["diets"],
                "price": f"{dish['price']} €" if dish["price"] else None,
                "description": "",
            }
        )
    return [
        {
            "id": "meiccu",
            "name": "Meiccu",
            "subtitle": "Pihlajatie 34",
            "url": url,
            "hours": next((n for n in notes if "tarjoill" in n.lower() and "klo" in n.lower()),
                          next((n for n in notes if "klo" in n.lower()), None)),
            "note": (day or {}).get("note"),
            "sections": [{"name": key or None, "dishes": dishes}
                         for key, dishes in sections.items()],
        }
    ]

# ---------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch the Meiccu lunch menu (meiccu.fi/lounas).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  lounas_meiccu.py                 today\n"
            "  lounas_meiccu.py -d all          the whole week\n"
            "  lounas_meiccu.py --diet V        vegan dishes today\n"
            "  lounas_meiccu.py -s buffet -d all   only the buffet sections\n"
            "  lounas_meiccu.py -v -d all       with the notes and the set menu\n"
            "  lounas_meiccu.py --json | jq .   machine-readable output\n"
        ),
    )
    parser.add_argument("-d", "--date", default=None,
                        help="today (default), tomorrow, a weekday, 2026-09-23, 23.09., or 'all'")
    parser.add_argument("--diet", action="append", default=[],
                        help="diet code, e.g. V, G, L (repeatable)")
    parser.add_argument("-s", "--section", action="append", default=[],
                        help="section name substring, e.g. buffet, keittiöstä (repeatable)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also print the page notes and the served-to-table set menu")
    parser.add_argument("--url", default=URL, help=f"page to read (default: {URL})")
    parser.add_argument("--json", action="store_true", help="output JSON instead of text")
    parser.add_argument("--text", action="store_true", help="print the page's headings and paragraphs")
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

    blocks = [(b["tag"], b["text"]) for b in html_blocks(page, tags="h[1-6]|p")]
    if args.text:
        for _tag, line in blocks:
            print(line)
        return 0

    days, notes, extras = parse_week(blocks, today)
    parsed_any = bool(days)

    try:
        wanted_date = resolve_date(args.date, today)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if wanted_date is not None:
        days = [day for day in days if day["date"] == wanted_date.isoformat()]

    if args.section:
        for day in days:
            day["dishes"] = [d for d in day["dishes"] if matches(d["section"] or "", args.section)]
    if args.diet:
        wanted = [code.lower() for code in args.diet]
        for day in days:
            day["dishes"] = [
                d for d in day["dishes"] if any(c.lower() in wanted for c in d["diets"])
            ]
    if args.section or args.diet:
        days = [day for day in days if day["dishes"]]

    if args.json:
        json.dump({"days": days, "notes": notes, "extras": extras},
                  sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        sys.stdout.write(render(days, notes, extras, args, parsed_any))
    return 0


if __name__ == "__main__":
    sys.exit(main())
