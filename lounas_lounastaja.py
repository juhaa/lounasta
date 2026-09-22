#!/usr/bin/env python3
"""Fetch a lunch menu served by the Lounastaja widget (lounas.app).

Viisi Penniä (https://viisipennia.fi/lounas) embeds the widget rather than
publishing the menu itself:

    <div data-lounastaja-widget-id="..." data-api-key="8865e81e-..."></div>
    <script defer src="https://lounastaja.app/widget/base.min.js"></script>

The widget reads a plain JSON feed, which is what this script uses:

    https://lounastaja.app/api/v1/week/<api key>/active?language=fi
    https://lounastaja.app/api/v1/week/<api key>/<year>/<week>?language=fi

Any restaurant on the same platform works: pass its key with --api-key, or
point --from-url at a page that embeds the widget and let the script read the
key out of the HTML.

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
    matches,
    read_cache,
    request_json,
    request_text,
    resolve_date,
    write_cache,
)

API = "https://lounastaja.app/api/v1"
DEFAULT_KEY = "8865e81e-8368-4daf-a162-2e2f1c4cd066"  # Viisi Penniä, Helsinki
DEFAULT_SITE = "https://viisipennia.fi/lounas"


# --------------------------------------------------------------------------- fetch


def api_key_from_url(url: str, *, use_cache: bool = True) -> str:
    """Read the widget's api key out of a page that embeds it."""
    path = cache_path("lounastaja", f"key-{url}")
    if use_cache:
        cached = read_cache(path)
        if cached:
            return cached

    page = request_text(url)
    match = re.search(r'data-api-key=["\']([0-9a-fA-F-]{16,})["\']', page)
    if not match:
        raise ValueError(f"no Lounastaja widget found on {url}")
    write_cache(path, match.group(1))
    return match.group(1)


def fetch_week(key: str, week: tuple[int, int] | None, lang: str,
               *, use_cache: bool = True) -> dict:
    which = f"{week[0]}/{week[1]}" if week else "active"
    path = cache_path("lounastaja", f"week-{key}-{which}-{lang}")
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            return cached

    url = f"{API}/week/{key}/{which}?language={lang}"
    try:
        payload = request_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError(f"no menu published for week {which}") from exc
        raise
    if not payload.get("success"):
        raise ValueError(payload.get("message") or "the API returned an error")

    data = payload.get("data") or {}
    write_cache(path, data)
    return data


# --------------------------------------------------------------------------- shape


def text_of(value, lang: str) -> str:
    """Pick a language out of a {"fi": ..., "en": ...} field."""
    if isinstance(value, dict):
        return (value.get(lang) or value.get("fi") or value.get("en") or "").strip()
    return (value or "").strip() if isinstance(value, str) else ""


def price_of(lunch: dict, lang: str) -> str:
    normal = lunch.get("normalPrice") or {}
    price = (normal.get("price") or "").strip()
    if not price:
        return ""
    unit = text_of(normal.get("unit"), lang)
    return f"{price} {unit}".strip()


def collect(data: dict, args, lang: str) -> list[dict]:
    days = []
    for day in (data.get("week") or {}).get("days") or []:
        lunches = []
        for lunch in day.get("lunches") or []:
            allergens = [
                text_of(a.get("abbreviation"), lang) or text_of(a.get("title"), lang)
                for a in lunch.get("allergens") or []
            ]
            lunches.append(
                {
                    "name": text_of(lunch.get("title"), lang),
                    "description": text_of(lunch.get("description"), lang),
                    "diets": [a for a in allergens if a],
                    "tags": [text_of(t.get("label") or t.get("title"), lang)
                             for t in lunch.get("tags") or []],
                    "price": price_of(lunch, lang),
                    "extra_prices": [
                        f"{(p.get('price') or '').strip()} {text_of(p.get('unit'), lang)}".strip()
                        for p in lunch.get("extraPrices") or []
                    ],
                }
            )

        if args.diet:
            lunches = [l for l in lunches if any(matches(d, args.diet) for d in l["diets"])]

        days.append(
            {
                "date": day.get("dateString"),
                "name": text_of(day.get("dayName"), lang),
                "closed": bool(day.get("isClosed")),
                "hidden": bool(day.get("isHidden")),
                "closed_text": text_of(day.get("closedText"), lang),
                "lunches": lunches,
            }
        )

    days.sort(key=lambda d: d["date"] or "")
    return days


def week_of(date: dt.date) -> tuple[int, int]:
    iso = date.isocalendar()
    return iso.year, iso.week


# --------------------------------------------------------------------------- print


def render(data: dict, days: list[dict], args, lang: str) -> str:
    location = data.get("location") or {}
    week = data.get("week") or {}
    title = location.get("name") or "Lounas"
    lines = [title, "=" * len(title)]

    if args.verbose:
        message = text_of(week.get("messageOfTheWeek"), lang)
        if message:
            lines.append(message)
        if week.get("dateRange"):
            lines.append(f"Viikko {week.get('week')}: {week['dateRange']}")
        lines.extend(lunch_hours(location, lang))
        pdf = text_of(week.get("pdfUrl"), lang)
        if pdf:
            lines.append(pdf)
        lines.append("")

    if not days:
        lines.append("  (no menu for that date)")
        return "\n".join(lines) + "\n"

    for day in days:
        date = dt.date.fromisoformat(day["date"]) if day["date"] else None
        label = day["name"] or (WEEKDAYS["fi"][date.weekday()] if date else "")
        if date:
            label = f"{label} {date:%d.%m.%Y}"
        lines.append(f"-- {label}")
        if not day["lunches"]:
            lines.append(f"   ({day['closed_text'] or 'suljettu'})" if day["closed"]
                         else "   (no dishes)")
            lines.append("")
            continue

        for lunch in day["lunches"]:
            tags = "/".join(lunch["diets"])
            suffix = "  ".join(part for part in (f"[{tags}]" if tags else "", lunch["price"]) if part)
            lines.append(f"   * {lunch['name']}" + (f"   {suffix}" if suffix else ""))
            if args.verbose and lunch["description"]:
                lines.append(f"       {lunch['description']}")
            if args.verbose and lunch["extra_prices"]:
                lines.append(f"       {', '.join(lunch['extra_prices'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def lunch_hours(location: dict, lang: str) -> list[str]:
    out = []
    for entry in location.get("businessHours") or []:
        hours = entry.get("lunchHours") or []
        if not entry.get("lunchAvailable") or not hours:
            continue
        weekday = (int(entry.get("dayNumber", 0)) + 6) % 7  # the API starts at Sunday
        spans = ", ".join(
            "%02d:%02d-%02d:%02d"
            % (
                (h.get("start") or {}).get("hours", 0), (h.get("start") or {}).get("minutes", 0),
                (h.get("end") or {}).get("hours", 0), (h.get("end") or {}).get("minutes", 0),
            )
            for h in hours
        )
        out.append(f"{WEEKDAYS['fi'][weekday]} lounas {spans}")
    return out


# ------------------------------------------------------------------- aggregator


def menu_for(date, *, key=DEFAULT_KEY, lang="fi", use_cache=True) -> list[dict]:
    """Normalised menu of one Lounastaja restaurant for one date (for lounasta_web)."""
    args = build_parser().parse_args(["-d", date.isoformat(), "--lang", lang])
    week = None if week_of(date) == week_of(dt.date.today()) else week_of(date)
    data = fetch_week(key, week, lang, use_cache=use_cache)

    days = collect(data, args, lang)
    day = next((d for d in days if d["date"] == date.isoformat()), None)
    location = data.get("location") or {}
    dishes = [
        {
            "name": lunch["name"],
            "diets": lunch["diets"],
            "price": lunch["price"] or None,
            "description": lunch["description"],
        }
        for lunch in (day or {}).get("lunches", [])
    ]
    hours = lunch_hours(location, lang)
    weekday = WEEKDAYS["fi"][date.weekday()]
    return [
        {
            "id": f"lounastaja-{key[:8]}",
            "name": location.get("name") or "Lounas",
            "subtitle": text_of((data.get("week") or {}).get("messageOfTheWeek"), lang),
            "url": location.get("websiteUrl") or "https://lounas.app/",
            "hours": next((h for h in hours if h.startswith(weekday)), None),
            "note": (day or {}).get("closed_text") if (day or {}).get("closed") else None,
            "sections": [{"name": None, "dishes": dishes}] if dishes else [],
        }
    ]

# ---------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch a lunch menu from the Lounastaja widget feed (lounas.app).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  lounas_lounastaja.py                  Viisi Penniä, today\n"
            "  lounas_lounastaja.py -d all           the whole week\n"
            "  lounas_lounastaja.py --diet V -d all  vegan dishes\n"
            "  lounas_lounastaja.py --lang en -v     English, with hours and the week's note\n"
            "  lounas_lounastaja.py --week 2026/39   a specific ISO week\n"
            "  lounas_lounastaja.py --from-url https://example.fi/lounas   another restaurant\n"
        ),
    )
    parser.add_argument("-d", "--date", default=None,
                        help="today (default), tomorrow, a weekday, 2026-09-23, 23.09., or 'all'")
    parser.add_argument("--diet", action="append", default=[],
                        help="allergen/diet abbreviation, e.g. V, G, L (repeatable)")
    parser.add_argument("--week", default=None,
                        help="ISO week to fetch: '39' or '2026/39' (default: the active week)")
    parser.add_argument("--lang", default="fi", choices=["fi", "en"], help="menu language")
    parser.add_argument("--api-key", default=None,
                        help="Lounastaja api key (default: Viisi Penniä)")
    parser.add_argument("--from-url", default=None,
                        help="read the api key from a page that embeds the widget")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show the week's note, lunch hours, the PDF link and descriptions")
    parser.add_argument("--json", action="store_true", help="output JSON instead of text")
    parser.add_argument("--raw", action="store_true", help="dump the unmodified API response")
    parser.add_argument("--no-cache", action="store_true", help="bypass the local cache")
    return parser


def parse_week_arg(value: str, today: dt.date) -> tuple[int, int]:
    match = re.fullmatch(r"(?:(\d{4})[/-])?(\d{1,2})", value.strip())
    if not match:
        raise ValueError(f"cannot parse week: {value!r}")
    year = int(match.group(1)) if match.group(1) else today.isocalendar().year
    return year, int(match.group(2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    today = dt.date.today()
    use_cache = not args.no_cache

    try:
        wanted_date = resolve_date(args.date, today)
        key = args.api_key
        if key is None:
            key = api_key_from_url(args.from_url, use_cache=use_cache) if args.from_url else DEFAULT_KEY

        week = parse_week_arg(args.week, today) if args.week else None
        if week is None and wanted_date is not None and week_of(wanted_date) != week_of(today):
            week = week_of(wanted_date)  # a date outside the active week
        data = fetch_week(key, week, args.lang, use_cache=use_cache)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: cannot fetch menu: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.raw:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    days = collect(data, args, args.lang)
    if wanted_date is not None:
        days = [day for day in days if day["date"] == wanted_date.isoformat()]
    else:
        days = [day for day in days if not day["hidden"] or day["lunches"]]
    if args.diet:
        days = [day for day in days if day["lunches"]]

    if args.json:
        json.dump({"location": data.get("location"), "week": (data.get("week") or {}).get("week"),
                   "days": days}, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        sys.stdout.write(render(data, days, args, args.lang))
    return 0


if __name__ == "__main__":
    sys.exit(main())
