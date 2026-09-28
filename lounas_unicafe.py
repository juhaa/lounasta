#!/usr/bin/env python3
"""Fetch the UniCafe lunch menu (https://menu.unicafe.fi/).

The public site is a React SPA that reads a single WordPress REST endpoint
containing every restaurant and roughly two weeks of menus:

    https://unicafe.fi/wp-json/swiss/v1/restaurants?lang=<fi|en|sv>

The response is ~1.2 MB, so it is cached locally for a few hours.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.error

from lounas_common import (
    cache_path,
    format_date,
    matches,
    read_cache,
    request_json,
    resolve_date,
    write_cache,
)

API_URL = "https://unicafe.fi/wp-json/swiss/v1/restaurants?lang={lang}"

PRICE_GROUPS = ["student", "student_hyy", "graduate", "graduate_hyy", "contract", "normal"]

LUNCH_LABEL = {"fi": "Lounas", "en": "Lunch", "sv": "Lunch"}


# --------------------------------------------------------------------------- fetch


def fetch(lang: str, *, use_cache: bool = True) -> list:
    path = cache_path("unicafe", lang)
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            return cached

    data = request_json(API_URL.format(lang=lang))
    if not isinstance(data, list):
        raise ValueError("unexpected API response: expected a list of restaurants")
    write_cache(path, data)
    return data


# ---------------------------------------------------------------------------- dates


def parse_menu_date(label: str, today: dt.date) -> dt.date | None:
    """Turn an API label such as "Ma 21.09." into a real date.

    The API omits the year, so resolve it against today, allowing the menu to
    span a new year in either direction.
    """
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.", label)
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            continue
        if abs((candidate - today).days) <= 180:
            return candidate
    return None


# --------------------------------------------------------------------------- shape


def collect(data: list, args, today: dt.date) -> list[dict]:
    """Normalise the API payload into a list of restaurants with menu days."""
    wanted_date = resolve_date(args.date, today)
    result = []

    for restaurant in data:
        title = restaurant.get("title") or ""
        slug = restaurant.get("slug") or ""
        locations = [loc.get("name", "") for loc in restaurant.get("location") or []]

        if args.restaurant and not (matches(title, args.restaurant) or matches(slug, args.restaurant)):
            continue
        if args.location and not any(matches(loc, args.location) for loc in locations):
            continue

        menu_data = restaurant.get("menuData") or {}
        days = []
        for menu in menu_data.get("menus") or []:
            label = menu.get("date") or ""
            date = parse_menu_date(label, today)
            if wanted_date is not None and date != wanted_date:
                continue

            items = []
            for item in menu.get("data") or []:
                meta = item.get("meta") or {}
                price = item.get("price") or {}
                values = price.get("value")
                items.append(
                    {
                        "name": (item.get("name") or "").strip(),
                        "category": price.get("name") or "",
                        "prices": values if isinstance(values, dict) else {},
                        "diets": list(meta.get("0") or []),
                        "allergens": list(meta.get("1") or []),
                        "ingredients": item.get("ingredients") or "",
                        "nutrition": item.get("nutrition") or "",
                    }
                )

            if args.diet:
                items = [it for it in items if any(matches(d, args.diet) for d in it["diets"])]
            if not items and not menu.get("message"):
                continue

            days.append(
                {
                    "date": date.isoformat() if date else None,
                    "label": label.strip(),
                    "message": menu.get("message"),
                    "items": items,
                }
            )

        if not days and not args.all_restaurants:
            continue

        result.append(
            {
                "title": title,
                "slug": slug,
                "locations": locations,
                "address": restaurant.get("address") or menu_data.get("address") or "",
                "url": restaurant.get("permalink") or "",
                "opening_hours": opening_hours(menu_data),
                "days": days,
            }
        )

    result.sort(key=lambda r: (r["locations"][0] if r["locations"] else "", r["title"]))
    return result


def opening_hours(menu_data: dict) -> list[str]:
    hours = (menu_data.get("visitingHours") or {}).get("lounas")
    if not isinstance(hours, dict):
        return []
    out = []
    for item in hours.get("items") or []:
        label, value = item.get("label") or "", item.get("hours") or ""
        out.append(" ".join(part for part in (label, value) if part))
    return out


# --------------------------------------------------------------------------- print


def price_text(prices: dict, group: str) -> str:
    if not prices:
        return ""
    value = prices.get(group)
    if value is None:
        for fallback in PRICE_GROUPS:
            value = prices.get(fallback)
            if value is not None:
                group = fallback
                break
    if value is None:
        return ""
    return f"{value} €"


def render(restaurants: list[dict], args, today: dt.date) -> str:
    lines = []
    for restaurant in restaurants:
        header = restaurant["title"]
        if restaurant["locations"]:
            header += f" ({', '.join(restaurant['locations'])})"
        lines.append(header)
        lines.append("=" * len(header))
        if args.verbose:
            if restaurant["address"]:
                lines.append(restaurant["address"])
            if restaurant["opening_hours"]:
                label = LUNCH_LABEL.get(args.lang, "Lunch")
                lines.append(f"{label}: " + "; ".join(restaurant["opening_hours"]))
            lines.append("")

        if not restaurant["days"]:
            lines.append("  (no menu)")
            lines.append("")
            continue

        for day in restaurant["days"]:
            date = dt.date.fromisoformat(day["date"]) if day["date"] else None
            lines.append(f"-- {format_date(date, args.lang) if date else day['label']}")
            if day["message"]:
                lines.append(f"   {day['message']}")

            for item in day["items"]:
                price = price_text(item["prices"], args.price_group)
                tags = "/".join(item["diets"])
                suffix = "  ".join(part for part in (f"[{tags}]" if tags else "", price) if part)
                category = f"{item['category']}: " if item["category"] and args.verbose else ""
                lines.append(f"   * {category}{item['name']}" + (f"   {suffix}" if suffix else ""))
                if args.allergens and item["allergens"]:
                    lines.append(f"       allergens: {', '.join(item['allergens'])}")
                if args.verbose and item["nutrition"]:
                    lines.append(f"       {item['nutrition']}")
            lines.append("")
    if not lines:
        lines.append("Nothing matched.")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------- aggregator


def menu_for(date, *, restaurants=("Meilahti", "Terkko"), names=None, lang="fi",
             price_group="student", prices=True, use_cache=True) -> list[dict]:
    """Normalised menu of the given restaurants for one date (for lounasta_web).

    `names` renames a restaurant on the card, e.g. {"Meilahti": "Unicafe"}.
    """
    argv = ["-d", date.isoformat(), "--lang", lang, "--price-group", price_group]
    for name in restaurants:
        argv += ["-r", name]
    args = build_parser().parse_args(argv)

    data = fetch(lang, use_cache=use_cache)
    cards = []
    for restaurant in collect(data, args, date):
        day = restaurant["days"][0] if restaurant["days"] else None
        sections: dict[str, list] = {}
        for item in (day or {}).get("items", []):
            sections.setdefault(item["category"] or "", []).append(
                {
                    "name": item["name"],
                    "diets": item["diets"],
                    "price": (price_text(item["prices"], price_group) or None) if prices else None,
                    "description": "",
                }
            )
        cards.append(
            {
                "id": f"unicafe-{restaurant['slug']}",
                "name": (names or {}).get(restaurant["title"], restaurant["title"]),
                "subtitle": " · ".join(["UniCafe"] + restaurant["locations"]),
                "url": restaurant["url"] or "https://menu.unicafe.fi/",
                "hours": "; ".join(restaurant["opening_hours"]) or None,
                "note": (day or {}).get("message"),
                "sections": [{"name": name or None, "dishes": dishes}
                             for name, dishes in sections.items()],
            }
        )
    return cards

# ---------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch the UniCafe lunch menu (menu.unicafe.fi).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  lounas.py                              today, every restaurant\n"
            "  lounas.py -r chemicum -r exactum       only these restaurants\n"
            "  lounas.py -l kumpula -d tomorrow       one campus, tomorrow\n"
            "  lounas.py --diet veg --date all        vegan dishes, whole period\n"
            "  lounas.py --json | jq .                machine-readable output\n"
        ),
    )
    parser.add_argument("-r", "--restaurant", action="append", default=[],
                        help="restaurant name substring (repeatable)")
    parser.add_argument("-l", "--location", action="append", default=[],
                        help="campus/area substring, e.g. Kumpula, Keskusta (repeatable)")
    parser.add_argument("-d", "--date", default=None,
                        help="today (default), tomorrow, a weekday, 2026-09-23, 23.09., or 'all'")
    parser.add_argument("--diet", action="append", default=[],
                        help="diet tag substring, e.g. Veg, G, M (repeatable)")
    parser.add_argument("--lang", default="fi", choices=["fi", "en", "sv"], help="menu language")
    parser.add_argument("--price-group", default="student", choices=PRICE_GROUPS,
                        help="which price to show (default: student)")
    parser.add_argument("--allergens", action="store_true", help="show allergen list")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show address, opening hours, category and nutrition")
    parser.add_argument("--all-restaurants", action="store_true",
                        help="also list restaurants that have no menu for the date")
    parser.add_argument("--list-restaurants", action="store_true",
                        help="print restaurant names and locations, then exit")
    parser.add_argument("--json", action="store_true", help="output JSON instead of text")
    parser.add_argument("--no-cache", action="store_true", help="bypass the local cache")
    parser.add_argument("--raw", action="store_true", help="dump the unmodified API response")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    today = dt.date.today()

    try:
        data = fetch(args.lang, use_cache=not args.no_cache)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"error: cannot fetch menu: {exc}", file=sys.stderr)
        return 1

    if args.raw:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    if args.list_restaurants:
        for restaurant in sorted(data, key=lambda r: r.get("title") or ""):
            locations = ", ".join(loc.get("name", "") for loc in restaurant.get("location") or [])
            print(f"{restaurant.get('title', ''):40} {locations:12} {restaurant.get('slug', '')}")
        return 0

    try:
        restaurants = collect(data, args, today)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(restaurants, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        sys.stdout.write(render(restaurants, args, today))
    return 0


if __name__ == "__main__":
    sys.exit(main())
