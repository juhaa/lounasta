#!/usr/bin/env python3
"""Fetch a HUS lunch menu (https://menu.hus.fi/, CGI Aromi eMenus).

The public page is an Angular SPA sitting on a REST API. Three calls are
needed, all relative to a site root such as
`https://menu.hus.fi/HUSAromieMenus/FI/Default/HUS/Biomedicum`:

  1. GET  /api/Common/Page/GetPageInfo?currentpage=Restaurant
         -> the restaurant list with GUID ids
  2. GET  /api/GetRestaurantPublicDinerGroups?id=&startDate=&endDate=
         -> the diner group ("Lounasruokailijat") used as the menu filter
  3. POST /api/Common/Restaurant/RestaurantMeals?Id=&StartDate=&EndDate=
         with the diner group object as the JSON body -> the menu itself

Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
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

DEFAULT_SITE = "https://menu.hus.fi/HUSAromieMenus/{lang}/Default/HUS/Biomedicum"
DEFAULT_RESTAURANT = "Biomedicum lounasravintola"


# --------------------------------------------------------------------------- fetch


def site_url(base: str, lang: str) -> str:
    return base.format(lang=lang.upper()) if "{lang}" in base else base.rstrip("/")


def api_timestamp(date: dt.date, end_of_day: bool = False) -> str:
    time = "23:59:59.000Z" if end_of_day else "00:00:00.000Z"
    return f"{date.isoformat()}T{time}"


def strip_pictures(data):
    """Drop embedded base64 images; they are most of the payload."""
    if isinstance(data, dict):
        return {
            key: ("" if key in ("Data", "MenuDayComponentPicData") and isinstance(value, str)
                  else strip_pictures(value))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [strip_pictures(item) for item in data]
    return data


def get_page_info(site: str, *, use_cache: bool = True) -> dict:
    path = cache_path("hus", f"pageinfo-{site}")
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            return cached
    data = request_json(f"{site}/api/Common/Page/GetPageInfo?currentpage=Restaurant")
    write_cache(path, data)
    return data


def list_restaurants(page_info: dict) -> list[dict]:
    out = []
    for restaurant in page_info.get("Restaurants") or []:
        out.append(
            {
                "id": restaurant.get("Id"),
                "name": restaurant.get("Name") or restaurant.get("NameOrCode") or "",
                "type_id": restaurant.get("RestaurantTypeId"),
            }
        )
    out.sort(key=lambda r: r["name"])
    return out


def pick_restaurant(restaurants: list[dict], needle: str) -> dict:
    if not needle:
        raise ValueError("no restaurant given")
    exact = [r for r in restaurants if r["id"] == needle or r["name"].lower() == needle.lower()]
    hits = exact or [r for r in restaurants if matches(r["name"], [needle])]
    if not hits:
        raise ValueError(f"no restaurant matches {needle!r}; try --list-restaurants")
    if len(hits) > 1 and not exact:
        names = ", ".join(r["name"] for r in hits[:6])
        raise ValueError(f"{needle!r} matches several restaurants: {names}")
    return hits[0]


def get_meals(site: str, restaurant_id: str, start: dt.date, end: dt.date,
              *, use_cache: bool = True) -> list:
    key = f"meals-{site}-{restaurant_id}-{start}-{end}"
    path = cache_path("hus", key)
    if use_cache:
        cached = read_cache(path)
        if cached is not None:
            return cached

    window = f"startDate={api_timestamp(start)}&endDate={api_timestamp(end, True)}"
    groups = request_json(
        f"{site}/api/GetRestaurantPublicDinerGroups?id={restaurant_id}&{window}"
    )
    if not groups:
        return []

    # The API rejects a null SuitabilityDietIds; the SPA sends [] for "no filter".
    diner_group = dict(groups[0])
    diner_group["SuitabilityDietIds"] = diner_group.get("SuitabilityDietIds") or []

    meals_url = (
        f"{site}/api/Common/Restaurant/RestaurantMeals"
        f"?Id={restaurant_id}&StartDate={api_timestamp(start)}&EndDate={api_timestamp(end, True)}"
    )
    data = request_json(meals_url, data=json.dumps(diner_group).encode("utf-8"))
    data = strip_pictures(data)
    write_cache(path, data)
    return data


# --------------------------------------------------------------------------- shape


def parse_day_date(day: dict) -> dt.date | None:
    raw = day.get("Date") or day.get("DateOffset") or ""
    try:
        return dt.datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def collect(data: list, args, today: dt.date) -> list[dict]:
    wanted_date = resolve_date(args.date, today)
    days = []

    for day in data:
        date = parse_day_date(day)
        if wanted_date is not None and date != wanted_date:
            continue

        meals = []
        for meal in day.get("Meals") or []:
            name = meal.get("MealName") or ""
            if args.meal and not matches(name, args.meal):
                continue

            dishes = []
            for dish in meal.get("Dishes") or []:
                diets = [d.strip() for d in (dish.get("DietDetails") or "").split(",") if d.strip()]
                labels = [
                    p.get("FileName") or p.get("Name") or ""
                    for p in dish.get("RecipeLabelsPicture") or []
                ]
                dishes.append(
                    {
                        "name": (dish.get("DishName") or "").strip(),
                        "diets": diets,
                        "labels": [label for label in labels if label],
                        "ingredients": dish.get("DishIngredients") or dish.get("Ingredients") or "",
                    }
                )

            if args.diet:
                dishes = [d for d in dishes if any(matches(x, args.diet) for x in d["diets"])]
            if not dishes:
                continue
            meals.append({"name": name, "dishes": dishes})

        if not meals:
            continue
        days.append(
            {
                "date": date.isoformat() if date else None,
                "label": day.get("MenuDate") or "",
                "meals": meals,
            }
        )

    return days


# --------------------------------------------------------------------------- print


def render(restaurant: dict, days: list[dict], args) -> str:
    lines = [restaurant["name"], "=" * len(restaurant["name"])]
    if not days:
        lines.append("  (no menu)")
        return "\n".join(lines) + "\n"

    for day in days:
        date = dt.date.fromisoformat(day["date"]) if day["date"] else None
        lines.append(f"-- {format_date(date, args.lang.lower()) if date else day['label']}")
        for meal in day["meals"]:
            lines.append(f"   {meal['name']}")
            for dish in meal["dishes"]:
                tags = "/".join(dish["diets"])
                suffix = f"   [{tags}]" if tags and not args.no_diets else ""
                lines.append(f"     * {dish['name']}{suffix}")
                if args.verbose and dish["labels"]:
                    lines.append(f"         {', '.join(dish['labels'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------- aggregator


def menu_for(date, *, restaurant=DEFAULT_RESTAURANT, site=DEFAULT_SITE, lang="FI",
             meals=("lounas", "keitto", "salaatti", "lunch", "soup", "salad"),
             use_cache=True) -> list[dict]:
    """Normalised menu of one HUS restaurant for one date (for lounasta_web)."""
    argv = ["-d", date.isoformat(), "--lang", lang, "-r", restaurant, "--site", site]
    for meal in meals:
        argv += ["-m", meal]
    args = build_parser().parse_args(argv)

    root = site_url(site, lang)
    page_info = get_page_info(root, use_cache=use_cache)
    chosen = pick_restaurant(list_restaurants(page_info), restaurant)
    data = get_meals(root, chosen["id"], date, date, use_cache=use_cache)

    days = collect(data, args, date)
    day = days[0] if days else None
    sections = [
        {
            "name": meal["name"],
            "dishes": [
                {"name": dish["name"], "diets": dish["diets"], "price": None, "description": ""}
                for dish in meal["dishes"]
            ],
        }
        for meal in (day or {}).get("meals", [])
    ]
    return [
        {
            "id": f"hus-{chosen['id']}",
            "name": chosen["name"],
            "subtitle": "HUS",
            "url": f"{root}/Page/Restaurant",
            "hours": None,
            "note": None,
            "sections": sections,
        }
    ]

# ---------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch a HUS lunch menu (menu.hus.fi, Aromi eMenus).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  lounas_hus.py                            Biomedicum, today\n"
            "  lounas_hus.py --meal lounas -d tomorrow  lunch dishes only, tomorrow\n"
            "  lounas_hus.py -r meilahden -d all        another restaurant, whole period\n"
            "  lounas_hus.py --list-restaurants         names and GUIDs\n"
            "  lounas_hus.py --json | jq .              machine-readable output\n"
        ),
    )
    parser.add_argument("-r", "--restaurant", default=DEFAULT_RESTAURANT,
                        help=f"restaurant name substring or GUID (default: {DEFAULT_RESTAURANT})")
    parser.add_argument("-d", "--date", default=None,
                        help="today (default), tomorrow, a weekday, 2026-09-23, 23.09., or 'all'")
    parser.add_argument("-m", "--meal", action="append", default=[],
                        help="meal name substring, e.g. lounas, aamupala (repeatable)")
    parser.add_argument("--diet", action="append", default=[],
                        help="diet code substring, e.g. VEGA, G, M (repeatable)")
    parser.add_argument("--days", type=int, default=14,
                        help="how many days to request from the API (default: 14)")
    parser.add_argument("--lang", default="FI", choices=["FI", "EN", "SV", "fi", "en", "sv"],
                        help="site language (default: FI)")
    parser.add_argument("--site", default=DEFAULT_SITE,
                        help="Aromi site root; '{lang}' is substituted (default: HUS Biomedicum)")
    parser.add_argument("--no-diets", action="store_true", help="hide diet codes")
    parser.add_argument("-v", "--verbose", action="store_true", help="show recipe labels")
    parser.add_argument("--list-restaurants", action="store_true",
                        help="print the restaurants on this site, then exit")
    parser.add_argument("--json", action="store_true", help="output JSON instead of text")
    parser.add_argument("--no-cache", action="store_true", help="bypass the local cache")
    parser.add_argument("--raw", action="store_true", help="dump the unmodified API response")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    today = dt.date.today()
    site = site_url(args.site, args.lang)
    use_cache = not args.no_cache

    try:
        page_info = get_page_info(site, use_cache=use_cache)
        restaurants = list_restaurants(page_info)

        if args.list_restaurants:
            for restaurant in restaurants:
                print(f"{restaurant['name']:45} {restaurant['id']}")
            return 0

        chosen = pick_restaurant(restaurants, args.restaurant)
        wanted_date = resolve_date(args.date, today)
        if wanted_date is None:
            start, end = today, today + dt.timedelta(days=args.days)
        else:
            start = end = wanted_date
        data = get_meals(site, chosen["id"], start, end, use_cache=use_cache)
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

    days = collect(data, args, today)
    if args.json:
        json.dump({"restaurant": chosen, "days": days}, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        sys.stdout.write(render(chosen, days, args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
