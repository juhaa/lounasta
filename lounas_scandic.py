#!/usr/bin/env python3
"""Fetch a Scandic hotel restaurant lunch menu (menu.scandichotels.com).

Scandic publishes its menus as PDFs behind a small service:

  GET /RestaurantMenuService/GetMenu?hotelId=&restId=&english=
      -> an HTML fragment listing the PDFs, e.g. "lounas viiko 39 (pdf)"
  GET /RestaurantMenuService/GetMenufile?hotelId=&restId=&fileId=
      -> the PDF itself

The weekly lunch PDF gets a new fileId every week, so by default the script
picks the lunch file whose title carries the current ISO week number. The PDF
has no text layer problems but no library is needed either: `pdf_text.py`
extracts the text with the standard library only.

Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import urllib.error

import pdf_text
from lounas_common import (
    cache_path,
    read_cache,
    request_bytes,
    request_text,
    resolve_date,
    write_cache,
)

SERVICE = "https://menu.scandichotels.com/RestaurantMenuService"
DEFAULT_HOTEL = "663"  # Scandic Marski, Helsinki
DEFAULT_REST = "rest0"

WEEKDAY_HEADINGS = {
    "maanantai": 0, "monday": 0, "måndag": 0, "mandag": 0,
    "tiistai": 1, "tuesday": 1, "tisdag": 1,
    "keskiviikko": 2, "wednesday": 2, "onsdag": 2,
    "torstai": 3, "thursday": 3, "torsdag": 3,
    "perjantai": 4, "friday": 4, "fredag": 4,
    "lauantai": 5, "saturday": 5, "lördag": 5, "lordag": 5,
    "sunnuntai": 6, "sunday": 6, "söndag": 6, "sondag": 6,
}

WEEKDAY_TITLES = {
    "fi": ["Maanantai", "Tiistai", "Keskiviikko", "Torstai", "Perjantai", "Lauantai", "Sunnuntai"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}

DIET_CODES = {"L", "VL", "M", "G", "V", "A", "VEG", "LL"}

# Everything from the first of these rows on is small print, not food.
FOOTER_RE = re.compile(
    r"(\b[A-ZÄÖÅ]{1,2}\s*=)"                     # the diet legend, "L=Laktoositon"
    r"|scandic friends|muutokset|changes to the|sisältyv|included in the",
    re.I,
)


# --------------------------------------------------------------------------- fetch


def list_menu_files(hotel: str, rest: str, english: bool, *, use_cache: bool = True) -> list[dict]:
    """Return the PDFs the service lists for this restaurant."""
    key = f"files-{hotel}-{rest}-{'en' if english else 'fi'}"
    path = cache_path("scandic", key)
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            return cached

    url = f"{SERVICE}/GetMenu?hotelId={hotel}&restId={rest}&english={'true' if english else ''}"
    page = request_text(url)

    files = []
    for match in re.finditer(r"href='([^']*fileId=(\d+)[^']*)'[^>]*>([^<]*)<", page):
        title = html.unescape(match.group(3)).strip()
        files.append(
            {
                "file_id": match.group(2),
                "title": re.sub(r"\s*\(pdf\)\s*$", "", title, flags=re.I).strip(),
                "url": html.unescape(match.group(1)),
            }
        )
    write_cache(path, files)
    return files


def pick_file(files: list[dict], wanted: str | None, week: int, english: bool) -> dict:
    """Choose the lunch PDF: an explicit --menu match, else this week's file."""
    if wanted:
        hits = [f for f in files if wanted.lower() in f["title"].lower() or f["file_id"] == wanted]
        if not hits:
            raise ValueError(f"no menu matches {wanted!r}; try --list-menus")
        return hits[0]

    keyword = "lunch" if english else "lounas"
    lunch = [f for f in files if keyword in f["title"].lower()]
    if not lunch:
        # Some hotels only publish a single "MENU" file.
        lunch = [f for f in files if "menu" in f["title"].lower()] or files
    if not lunch:
        raise ValueError("the service listed no menu files")

    this_week = [f for f in lunch if re.search(rf"\b0?{week}\b", f["title"])]
    return (this_week or lunch)[0]


def fetch_pdf(hotel: str, rest: str, file_id: str, *, use_cache: bool = True) -> bytes:
    path = cache_path("scandic", f"pdf-{hotel}-{rest}-{file_id}")
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            return bytes.fromhex(cached)

    url = f"{SERVICE}/GetMenufile?hotelId={hotel}&restId={rest}&fileId={file_id}"
    data = request_bytes(url, accept="application/pdf,*/*")
    write_cache(path, data.hex())
    return data


# --------------------------------------------------------------------------- parse


def pdf_rows(data: bytes, tolerance: float = 4.0) -> list[list[str]]:
    """Text of the PDF as visual rows, each row split into its columns."""
    doc = pdf_text.Document(data)
    rows: list[list[str]] = []
    for page in doc.pages():
        grouped: list[list[tuple[float, float, str]]] = []
        for fragment in sorted(pdf_text.page_fragments(doc, page), key=lambda f: (-f[1], f[0])):
            for row in grouped:
                if abs(row[0][1] - fragment[1]) <= tolerance:
                    row.append(fragment)
                    break
            else:
                grouped.append([fragment])
        for row in grouped:
            row.sort(key=lambda f: f[0])
            cells = [re.sub(r"\s+", " ", text).strip() for _x, _y, text in row]
            cells = [cell for cell in cells if cell]
            if cells:
                rows.append(cells)
    return rows


def split_diets(text: str) -> tuple[str, list[str]]:
    """Split "Perunamuusi L, G" into the dish name and its diet codes."""
    match = re.search(r"[\s,]((?:(?:%s)[\s,.]*)+)$" % "|".join(sorted(DIET_CODES, key=len, reverse=True)), text)
    if not match:
        return text.strip(" ,.:"), []
    codes = [c for c in re.split(r"[\s,.]+", match.group(1)) if c in DIET_CODES]
    return text[: match.start()].strip(" ,.:"), codes


def is_footer(text: str) -> bool:
    if FOOTER_RE.search(text):
        return True
    letters = [ch for ch in text if ch.isalpha()]
    upper = sum(1 for ch in letters if ch.isupper())
    return len(text) > 35 and letters and upper / len(letters) > 0.9


def parse_menu(rows: list[list[str]]) -> tuple[list[dict], list[str]]:
    """Turn the PDF rows into per-weekday dish lists plus the leftover notes."""
    days: list[dict] = []
    notes: list[str] = []
    current: dict | None = None
    in_footer = False

    for row in rows:
        joined = " ".join(row).strip()
        heading = WEEKDAY_HEADINGS.get(joined.lower().strip(" :"))
        if heading is not None:
            current = {"weekday": heading, "heading": joined.strip(), "dishes": []}
            days.append(current)
            in_footer = False
            continue

        if in_footer or (current is not None and is_footer(joined)):
            in_footer = True
            notes.append(joined)
            continue

        if current is None:
            notes.append(joined)
            continue

        for cell in row:
            for part in re.split(r"\s{3,}", cell):
                part = part.strip()
                if not part:
                    continue
                course = None
                course_match = re.match(r"^([A-Za-zÄÖÅäöå]+ruoka|Jälkiruoka|Dessert|Starter|Soup)\s*:\s*(.+)$", part)
                if course_match:
                    course, part = course_match.group(1), course_match.group(2)
                name, diets = split_diets(part)
                # The PDF sometimes wraps a dish's diet codes onto the next
                # column; hand such a stray prefix back to the previous dish.
                stray = re.match(r"^((?:(?:%s)[\s,.]+)+)(?=[A-ZÄÖÅ])" % "|".join(DIET_CODES), name)
                if stray and current["dishes"]:
                    codes = [c for c in re.split(r"[\s,.]+", stray.group(1)) if c in DIET_CODES]
                    previous = current["dishes"][-1]
                    if not previous["diets"]:
                        previous["diets"] = codes
                    name = name[stray.end():].strip(" ,.:")
                if name:
                    current["dishes"].append({"name": name, "diets": diets, "course": course})

    return days, notes


def menu_week_dates(days: list[dict], today: dt.date) -> None:
    """Attach real dates, assuming the PDF covers the week it was fetched for."""
    monday = today - dt.timedelta(days=today.weekday())
    for day in days:
        day["date"] = (monday + dt.timedelta(days=day["weekday"])).isoformat()


# --------------------------------------------------------------------------- print


def render(menu_file: dict, days: list[dict], notes: list[str], args, parsed_any: bool) -> str:
    title = menu_file["title"] or "Menu"
    lines = [title, "=" * len(title)]
    if not days:
        lines.append("  (no menu for that date)" if parsed_any
                     else "  (no dishes parsed; try --text)")
        return "\n".join(lines) + "\n"

    for day in days:
        names = WEEKDAY_TITLES["en" if args.english else "fi"]
        label = f"{names[day['weekday']]} {dt.date.fromisoformat(day['date']):%d.%m.%Y}"
        lines.append(f"-- {label}")
        for dish in day["dishes"]:
            tags = "/".join(dish["diets"])
            prefix = f"{dish['course']}: " if dish["course"] else ""
            lines.append(f"   * {prefix}{dish['name']}" + (f"   [{tags}]" if tags else ""))
        lines.append("")

    if args.verbose and notes:
        lines.extend(notes)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------- aggregator


def menu_for(date, *, hotel=DEFAULT_HOTEL, rest=DEFAULT_REST, name="Scandic",
             english=False, use_cache=True) -> list[dict]:
    """Normalised menu of one Scandic restaurant for one date (for lounasta_web)."""
    files = list_menu_files(hotel, rest, english, use_cache=use_cache)
    menu_file = pick_file(files, None, date.isocalendar().week, english)
    data = fetch_pdf(hotel, rest, menu_file["file_id"], use_cache=use_cache)

    days, notes = parse_menu(pdf_rows(data))
    menu_week_dates(days, date)
    day = next((d for d in days if d["date"] == date.isoformat()), None)

    sections: dict[str, list] = {}
    for dish in (day or {}).get("dishes", []):
        sections.setdefault(dish["course"] or "", []).append(
            {"name": dish["name"], "diets": dish["diets"], "price": None, "description": ""}
        )
    return [
        {
            "id": f"scandic-{hotel}-{rest}",
            "name": name,
            "subtitle": menu_file["title"],
            "url": menu_file["url"],
            "hours": next((n for n in notes if "klo" in n.lower()), None),
            "note": None,
            "sections": [{"name": key or None, "dishes": dishes}
                         for key, dishes in sections.items()],
        }
    ]

# ---------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch a Scandic restaurant lunch menu (PDF) and print it as text.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  lounas_scandic.py                     this week's lunch, today's dishes\n"
            "  lounas_scandic.py -d all              the whole week\n"
            "  lounas_scandic.py --english -d all    the English menu\n"
            "  lounas_scandic.py --list-menus        every PDF the hotel publishes\n"
            "  lounas_scandic.py --menu 'group menu' --text   another PDF, raw text\n"
            "  lounas_scandic.py --hotel 663 --rest rest0     another hotel/restaurant\n"
        ),
    )
    parser.add_argument("-d", "--date", default=None,
                        help="today (default), tomorrow, a weekday, 2026-09-23, or 'all'")
    parser.add_argument("--diet", action="append", default=[],
                        help="diet code, e.g. V, G, L (repeatable)")
    parser.add_argument("--hotel", default=DEFAULT_HOTEL, help=f"hotelId (default: {DEFAULT_HOTEL})")
    parser.add_argument("--rest", default=DEFAULT_REST, help=f"restId (default: {DEFAULT_REST})")
    parser.add_argument("--menu", default=None,
                        help="title substring or fileId of the PDF to read (default: this week's lunch)")
    parser.add_argument("--english", action="store_true", help="ask the service for English titles")
    parser.add_argument("-v", "--verbose", action="store_true", help="also print the notes on the PDF")
    parser.add_argument("--list-menus", action="store_true", help="list the published PDFs, then exit")
    parser.add_argument("--text", action="store_true", help="print the raw extracted PDF text")
    parser.add_argument("--json", action="store_true", help="output JSON instead of text")
    parser.add_argument("--pdf", metavar="PATH", help="save the PDF to PATH as well")
    parser.add_argument("--no-cache", action="store_true", help="bypass the local cache")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    today = dt.date.today()
    use_cache = not args.no_cache

    try:
        files = list_menu_files(args.hotel, args.rest, args.english, use_cache=use_cache)
        if args.list_menus:
            for entry in files:
                print(f"{entry['file_id']:>8}  {entry['title']}")
            return 0

        menu_file = pick_file(files, args.menu, today.isocalendar().week, args.english)
        data = fetch_pdf(args.hotel, args.rest, menu_file["file_id"], use_cache=use_cache)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: cannot fetch menu: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.pdf:
        with open(args.pdf, "wb") as fh:
            fh.write(data)

    try:
        rows = pdf_rows(data)
        if args.text:
            print(pdf_text.extract_text(data))
            return 0
    except pdf_text.PdfTextError as exc:
        print(f"error: cannot read the PDF: {exc}", file=sys.stderr)
        return 1

    days, notes = parse_menu(rows)
    menu_week_dates(days, today)
    parsed_any = bool(days)

    try:
        wanted_date = resolve_date(args.date, today)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if wanted_date is not None:
        days = [day for day in days if day["date"] == wanted_date.isoformat()]

    if args.diet:
        for day in days:
            day["dishes"] = [
                dish for dish in day["dishes"]
                if any(code.lower() in [d.lower() for d in dish["diets"]] for code in args.diet)
            ]
        days = [day for day in days if day["dishes"]]

    if args.json:
        json.dump({"menu": menu_file, "days": days, "notes": notes},
                  sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        sys.stdout.write(render(menu_file, days, notes, args, parsed_any))
    return 0


if __name__ == "__main__":
    sys.exit(main())
