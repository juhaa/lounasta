#!/usr/bin/env python3
"""Today's lunch for every restaurant on one page.

Runs a small local web server that calls each `lounas_*.py` fetcher, or writes
the same page to a static HTML file:

    ./lounasta_web.py                 # http://localhost:8000
    ./lounasta_web.py --build out.html

Sources are fetched in parallel and each one is isolated: if a site is down or
changes shape, its card shows the error and the rest of the page still works.
Responses come from the shared six-hour cache unless --no-cache is given or the
page is loaded with ?refresh=1.

Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import functools
import html
import http.server
import json
import socket
import socketserver
import sys
import time
import traceback
import urllib.error
import urllib.parse
import webbrowser

import lounas_hus
import lounas_iltrio
import lounas_lounastaja
import lounas_meiccu
import lounas_scandic
import lounas_unicafe
from lounas_common import DIET_NAMES, WEEKDAYS, normalize_diets, resolve_date

# Which restaurants the page shows, in the order they appear on the page. Each
# entry is a fetcher plus its options; edit this list to follow a different set
# of restaurants.
SOURCES = [
    ("HUS", lounas_hus.menu_for, {}),
    ("UniCafe", lounas_unicafe.menu_for,
     {"restaurants": ("Meilahti", "Terkko"), "names": {"Meilahti": "Unicafe"},
      "prices": False}),
    ("Scandic", lounas_scandic.menu_for, {}),
    ("Meiccu", lounas_meiccu.menu_for, {}),
    ("Viisi Penniä", lounas_lounastaja.menu_for, {}),
    ("il Trio", lounas_iltrio.menu_for, {}),
]


# ---------------------------------------------------------------------------- data

ATTEMPTS = 3          # a reset connection is usually gone by the next try
RETRY_PAUSE = 0.6     # seconds, doubled between attempts


def is_transient(exc: BaseException) -> bool:
    """Whether retrying the same request straight away is worth it."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 425, 429, 500, 502, 503, 504)
    return isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError,
                            socket.timeout, socket.gaierror))


def describe_error(exc: BaseException) -> tuple[str, str]:
    """A sentence for the reader and the technical detail behind it."""
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return "Ravintolan sivua ei löytynyt.", detail
        if exc.code in (401, 403):
            return "Ravintolan sivu ei anna lukea listaa.", detail
        return f"Ravintolan sivu vastasi virheellä (HTTP {exc.code}).", detail
    if is_transient(exc):
        return "Ravintolan sivuun ei juuri nyt saatu yhteyttä.", detail
    return "Lounaslistaa ei voitu lukea — sivun rakenne on ehkä muuttunut.", detail


def fetch_all(date: dt.date, *, use_cache: bool = True) -> list[dict]:
    """Run every source in parallel; a failing source becomes an error card."""
    def run(entry):
        label, function, options = entry
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return list(function(date, use_cache=use_cache, **options))
            except Exception as exc:  # one broken site must not take the page down
                if attempt < ATTEMPTS and is_transient(exc):
                    time.sleep(RETRY_PAUSE * attempt)
                    continue
                message, detail = describe_error(exc)
                return [
                    {
                        "id": f"error-{label.lower()}",
                        "name": label,
                        "subtitle": "",
                        "url": "",
                        "hours": None,
                        "note": None,
                        "sections": [],
                        "error": message,
                        "error_detail": detail,
                    }
                ]

    cards: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        for result in pool.map(run, SOURCES):
            cards.extend(result)

    # Each site spells the diets its own way; unify them for the page.
    for card in cards:
        for section in card.get("sections") or []:
            for dish in section["dishes"]:
                dish["diets"] = normalize_diets(dish.get("diets"))
    return cards


def diets_used(cards: list[dict]) -> list[str]:
    """The diet codes the page actually shows, known ones first."""
    used = {code for card in cards for section in card.get("sections") or []
            for dish in section["dishes"] for code in dish["diets"]}
    known = [code for code in DIET_NAMES if code in used]
    return known + sorted(used - set(known))


# -------------------------------------------------------------------------- render


STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f6f5f2; --card: #fff; --ink: #1c1b19; --muted: #6b6862;
  --line: #e4e1da; --accent: #b4532a; --chip: #efece5;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #17181a; --card: #1f2124; --ink: #ecebe8; --muted: #9a978f;
          --line: #2e3135; --accent: #e0854f; --chip: #2a2d31; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px 64px; background: var(--bg); color: var(--ink);
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
header { max-width: 1100px; margin: 0 auto 20px; display: flex; flex-wrap: wrap;
         align-items: baseline; gap: 8px 16px; }
h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
.date { color: var(--muted); }
nav { margin-left: auto; display: flex; gap: 12px; }
nav a { color: var(--accent); text-decoration: none; }
nav a:hover { text-decoration: underline; }
main { max-width: 1100px; margin: 0 auto; display: grid; gap: 16px;
       grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
        padding: 16px 18px; }
.card h2 { font-size: 17px; margin: 0 0 2px; }
.card h2 a { color: inherit; text-decoration: none; }
.card h2 a:hover { color: var(--accent); }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 12px; }
.section { margin-bottom: 12px; }
.section h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
              color: var(--muted); margin: 0 0 6px; font-weight: 600; }
ul { list-style: none; margin: 0; padding: 0; }
li { padding: 4px 0; border-bottom: 1px solid var(--line); }
li:last-child { border-bottom: 0; }
.dish { display: flex; gap: 10px; align-items: baseline; justify-content: space-between; }
.price { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.diets { margin-left: 6px; font-size: 11px; color: var(--muted); }
.desc { color: var(--muted); font-size: 13px; }
.empty { color: var(--muted); font-style: italic; }
.error { color: var(--accent); font-size: 14px; margin: 0 0 6px; }
.error-actions { font-size: 13px; color: var(--muted); }
.error-actions a { color: var(--accent); }
.error-actions details { display: inline; }
.error-actions summary { display: inline; cursor: pointer; list-style: none; }
.error-actions summary::-webkit-details-marker { display: none; }
.error-actions code { display: block; margin-top: 6px; font-size: 12px;
                      word-break: break-word; color: var(--muted); }
footer { max-width: 1100px; margin: 28px auto 0; color: var(--muted); font-size: 13px; }
.legend { max-width: 1100px; margin: 24px auto 0; padding: 14px 18px; background: var(--card);
          border: 1px solid var(--line); border-radius: 12px; }
.legend h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
             color: var(--muted); margin: 0 0 8px; font-weight: 600; }
.legend dl { display: flex; flex-wrap: wrap; gap: 4px 18px; margin: 0; }
.legend div { display: flex; gap: 6px; align-items: baseline; }
.legend dt { font-weight: 600; }
.legend dd { margin: 0; color: var(--muted); }
@media (max-width: 420px) { body { padding: 16px 12px 48px; } }
"""


def esc(value) -> str:
    return html.escape(str(value or ""))


def render_error(card: dict, *, live: bool, date: dt.date | None = None) -> str:
    """A failed source explains itself and offers a retry, not a traceback."""
    parts = [f'<p class="error">{esc(card["error"])}</p>', '<p class="error-actions">']
    if live:
        query = f"?date={date}&amp;refresh=1" if date else "?refresh=1"
        parts.append(f'<a href="{query}">Yritä uudelleen</a>')
    if card.get("error_detail"):
        parts.append(' · ' if live else '')
        parts.append('<details><summary>Tekninen tieto</summary>'
                     f'<code>{esc(card["error_detail"])}</code></details>')
    parts.append("</p>")
    return "".join(parts)


def render_card(card: dict, *, live: bool = True, date: dt.date | None = None) -> str:
    parts = ['<section class="card">']
    name = esc(card["name"])
    parts.append(f'<h2><a href="{esc(card["url"])}">{name}</a></h2>' if card.get("url")
                 else f"<h2>{name}</h2>")

    meta = " · ".join(p for p in (card.get("subtitle"), card.get("hours")) if p)
    if meta:
        parts.append(f'<p class="meta">{esc(meta)}</p>')
    if card.get("error"):
        parts.append(render_error(card, live=live, date=date))
    if card.get("note"):
        parts.append(f'<p class="meta">{esc(card["note"])}</p>')

    dishes = sum(len(section["dishes"]) for section in card.get("sections") or [])
    if not dishes and not card.get("error"):
        parts.append('<p class="empty">Ei lounasta tänään.</p>')

    for section in card.get("sections") or []:
        if not section["dishes"]:
            continue
        parts.append('<div class="section">')
        if section.get("name"):
            parts.append(f'<h3>{esc(section["name"])}</h3>')
        parts.append("<ul>")
        for dish in section["dishes"]:
            diets = ", ".join(dish.get("diets") or [])
            price = esc(dish["price"]) if dish.get("price") else ""
            parts.append(
                "<li><div class=\"dish\"><span>%s%s</span>%s</div>%s</li>"
                % (
                    esc(dish["name"]),
                    f'<span class="diets">{esc(diets)}</span>' if diets else "",
                    f'<span class="price">{price}</span>' if price else "",
                    f'<div class="desc">{esc(dish["description"])}</div>'
                    if dish.get("description") else "",
                )
            )
        parts.append("</ul></div>")

    parts.append("</section>")
    return "\n".join(parts)


def render_legend(cards: list[dict]) -> str:
    codes = diets_used(cards)
    if not codes:
        return ""
    items = "".join(
        f"<div><dt>{esc(code)}</dt><dd>{esc(DIET_NAMES.get(code, '?'))}</dd></div>"
        for code in codes
    )
    return f'<aside class="legend"><h2>Merkinnät</h2><dl>{items}</dl></aside>'


def render_page(date: dt.date, cards: list[dict], *, live: bool = True) -> str:
    weekday = WEEKDAYS["fi"][date.weekday()]
    heading = f"{weekday} {date:%d.%m.%Y}"
    day = dt.timedelta(days=1)
    nav = (
        f'<nav><a href="?date={date - day}">&larr; edellinen</a>'
        f'<a href="?date={dt.date.today()}">tänään</a>'
        f'<a href="?date={date + day}">seuraava &rarr;</a>'
        f'<a href="?date={date}&amp;refresh=1">päivitä</a></nav>'
        if live else ""
    )
    body = "\n".join(render_card(card, live=live, date=date) for card in cards)
    # A page built the evening before is read the next morning, and a page
    # the scheduler never rebuilt can be older still, so the stamp carries a
    # date as well as a time.
    stamp = dt.datetime.now().strftime("%d.%m.%Y klo %H:%M")
    return f"""<!doctype html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lounas {heading}</title>
<style>{STYLE}</style>
</head>
<body>
<header><h1>Lounas</h1><span class="date">{esc(heading)}</span>{nav}</header>
<main>
{body}
</main>
{render_legend(cards)}
<footer>Haettu {stamp}. Lähteet: menu.unicafe.fi, menu.hus.fi, menu.scandichotels.com,
meiccu.fi, lounas.app, iltrio.fi.</footer>
</body>
</html>
"""


# -------------------------------------------------------------------------- server


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "lounasta/1.0"

    def do_GET(self) -> None:  # noqa: N802 - required name
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path not in ("/", "/api", "/api.json"):
            self.send_error(404, "not found")
            return

        today = dt.date.today()
        try:
            date = resolve_date(query.get("date", [None])[0], today) or today
        except ValueError as exc:
            self.respond(400, "text/plain; charset=utf-8", str(exc).encode())
            return

        use_cache = "refresh" not in query and not self.server.no_cache
        try:
            cards = fetch_all(date, use_cache=use_cache)
        except Exception:  # pragma: no cover - the per-source guard normally wins
            traceback.print_exc()
            self.respond(500, "text/plain; charset=utf-8", b"internal error")
            return

        if parsed.path == "/":
            body = render_page(date, cards).encode("utf-8")
            self.respond(200, "text/html; charset=utf-8", body)
        else:
            payload = {"date": date.isoformat(), "restaurants": cards}
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.respond(200, "application/json; charset=utf-8", body)

    def respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, *, no_cache: bool):
        self.no_cache = no_cache
        super().__init__(address, handler)


# ---------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show today's lunch for every configured restaurant on one page.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  ./lounasta_web.py                     serve on http://localhost:8000\n"
            "  ./lounasta_web.py --port 9000 --open  another port, open a browser\n"
            "  ./lounasta_web.py --build lounas.html write the page to a file\n"
            "  ./lounasta_web.py --json              print the same data as JSON\n"
            "\nwhile serving:\n"
            "  /                 today's page       /?date=2026-09-23  another day\n"
            "  /?refresh=1       skip the cache     /api               the data as JSON\n"
        ),
    )
    parser.add_argument("-p", "--port", type=int, default=8000, help="port to serve on")
    parser.add_argument("--host", default="127.0.0.1", help="address to bind")
    parser.add_argument("-d", "--date", default=None,
                        help="date for --build/--json (default: today)")
    parser.add_argument("--build", metavar="PATH", help="write the page to a file and exit")
    parser.add_argument("--json", action="store_true", help="print the data as JSON and exit")
    parser.add_argument("--open", action="store_true", help="open the page in a browser")
    parser.add_argument("--no-cache", action="store_true", help="always refetch every source")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    today = dt.date.today()

    try:
        date = resolve_date(args.date, today) or today
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.build or args.json:
        cards = fetch_all(date, use_cache=not args.no_cache)
        if args.json:
            json.dump({"date": date.isoformat(), "restaurants": cards},
                      sys.stdout, ensure_ascii=False, indent=2)
            print()
        if args.build:
            with open(args.build, "w", encoding="utf-8") as fh:
                fh.write(render_page(date, cards, live=False))
            print(f"wrote {args.build}", file=sys.stderr)
        return 0

    handler = functools.partial(Handler)
    with Server((args.host, args.port), handler, no_cache=args.no_cache) as server:
        url = f"http://{args.host}:{args.port}/"
        print(f"serving {url} (ctrl-c to stop)", file=sys.stderr)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
