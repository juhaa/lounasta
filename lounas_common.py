"""Shared helpers for the per-chain lunch menu fetchers.

Each `lounas_<chain>.py` script talks to a different backend, but they all need
the same things: a cached HTTP fetch, loose date parsing for `--date`, and
substring filtering.
"""

from __future__ import annotations

import datetime as dt
import gzip
import html as html_module
import io
import json
import os
import re
import time
import urllib.request

CACHE_TTL = 6 * 3600  # seconds
USER_AGENT = "lounasta/1.0"

WEEKDAYS = {
    "fi": ["Ma", "Ti", "Ke", "To", "Pe", "La", "Su"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "sv": ["Mån", "Tis", "Ons", "Tors", "Fre", "Lör", "Sön"],
}

WEEKDAY_NAMES = {
    "ma": 0, "mon": 0, "monday": 0, "maanantai": 0, "man": 0,
    "ti": 1, "tue": 1, "tuesday": 1, "tiistai": 1, "tis": 1,
    "ke": 2, "wed": 2, "wednesday": 2, "keskiviikko": 2, "ons": 2,
    "to": 3, "thu": 3, "thursday": 3, "torstai": 3, "tors": 3,
    "pe": 4, "fri": 4, "friday": 4, "perjantai": 4, "fre": 4,
    "la": 5, "sat": 5, "saturday": 5, "lauantai": 5, "lor": 5,
    "su": 6, "sun": 6, "sunday": 6, "sunnuntai": 6, "son": 6,
}


# --------------------------------------------------------------------------- http


def cache_path(chain: str, key: str) -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in key)
    return os.path.join(base, "lounasta", f"{chain}-{safe}.json")


def read_cache(path: str, ttl: int = CACHE_TTL):
    try:
        if time.time() - os.path.getmtime(path) < ttl:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    except (OSError, ValueError):
        pass
    return None


def write_cache(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except OSError:
        pass  # cache is best effort


def request_bytes(url: str, *, data: bytes | None = None, accept: str = "*/*",
                  timeout: int = 60) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw


def request_text(url: str, *, timeout: int = 60) -> str:
    return request_bytes(url, accept="text/html,*/*", timeout=timeout).decode("utf-8", "replace")


def request_json(url: str, *, data: bytes | None = None, timeout: int = 60):
    raw = request_bytes(url, data=data, accept="application/json", timeout=timeout)
    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------- dates


def resolve_date(value: str | None, today: dt.date) -> dt.date | None:
    """Resolve a --date argument. "all" (or None where the caller wants it)
    means "every date" and is returned as None."""
    if value is None:
        return today
    text = value.strip().lower()
    if text in ("all", "kaikki"):
        return None
    if text in ("today", "tanaan", "tänään", "idag"):
        return today
    if text in ("tomorrow", "huomenna", "imorgon"):
        return today + dt.timedelta(days=1)
    if text in ("yesterday", "eilen", "igar", "igår"):
        return today - dt.timedelta(days=1)

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.", "%d.%m"):
        try:
            parsed = dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if fmt in ("%d.%m.", "%d.%m"):
            parsed = parsed.replace(year=today.year)
            if (parsed - today).days < -180:
                parsed = parsed.replace(year=today.year + 1)
        return parsed

    if text in WEEKDAY_NAMES:
        ahead = (WEEKDAY_NAMES[text] - today.weekday()) % 7
        return today + dt.timedelta(days=ahead)

    raise ValueError(f"cannot parse date: {value!r}")


def format_date(date: dt.date, lang: str) -> str:
    return f"{WEEKDAYS.get(lang, WEEKDAYS['en'])[date.weekday()]} {date:%d.%m.%Y}"


# ---------------------------------------------------------------------------- diets

# Every site marks the same handful of diets, each with its own spelling:
# vegan is "Veg" at UniCafe, "VEGA" at HUS and "V" everywhere else. These are
# the spellings already in use, with the most widely used one as the canonical
# form; nothing new is invented.
DIET_NAMES = {
    "G": "gluteeniton",
    "L": "laktoositon",
    "VL": "vähälaktoosinen",
    "M": "maidoton",
    "Mu": "munaton",
    "V": "vegaani",
    "Kasvis": "kasvisruoka",
    "Kela": "Kela-ateriatuen kriteerit täyttävä",
    "Ilmastovalinta": "ilmastovalinta",
}

DIET_ALIASES = {
    "g": "G", "gluteeniton": "G", "gluten-free": "G", "gluteenit√∂n": "G",
    "l": "L", "laktoositon": "L", "lactose-free": "L",
    "vl": "VL", "vähälaktoosinen": "VL", "low-lactose": "VL",
    "m": "M", "maidoton": "M", "dairy-free": "M", "milk-free": "M",
    "mu": "Mu", "munaton": "Mu", "egg-free": "Mu",
    "v": "V", "veg": "V", "vega": "V", "vegaani": "V", "vegaaninen": "V", "vegan": "V",
    "kasvis": "Kasvis", "kasvisruoka": "Kasvis", "vegetarian": "Kasvis",
    "kela": "Kela",
    "ilmastovalinta": "Ilmastovalinta", "climate choice": "Ilmastovalinta",
}


def normalize_diets(codes) -> list[str]:
    """Map each site's diet markings onto the shared spellings, in order.

    Unknown markings are kept as they are: better an unexplained code than a
    dropped one.
    """
    out: list[str] = []
    for code in codes or []:
        text = str(code).strip().strip(".,")
        if not text:
            continue
        canonical = DIET_ALIASES.get(text.lower(), text)
        if canonical not in out:
            out.append(canonical)
    return out


# ---------------------------------------------------------------------------- html

BOLD_MARK = "\x01"


def html_blocks(page: str, tags: str = "h[1-6]|p|li") -> list[dict]:
    """Flatten an HTML page to its text blocks, in document order.

    Each block is {"tag", "text", "bold"}; a `<br>` starts a new block, and
    "bold" says whether the line was wrapped in `<strong>`/`<b>`, which pages
    typically use for dish names and headings.
    """
    body = page[page.find("<body") :] or page
    body = re.sub(r"(?is)<(script|style|nav)\b.*?</\1>", " ", body)

    blocks = []
    for match in re.finditer(r"(?is)<(%s)\b[^>]*>(.*?)</\1>" % tags, body):
        inner = re.sub(r"(?is)<br\s*/?>", "\n", match.group(2))
        inner = re.sub(r"(?is)</?(strong|b)\s*>", BOLD_MARK, inner)
        text = html_module.unescape(re.sub(r"(?s)<[^>]+>", "", inner))
        for line in text.split("\n"):
            line = re.sub(r"[\s\xa0]+", " ", line).strip()
            if not line:
                continue
            bold = line.startswith(BOLD_MARK)
            line = line.replace(BOLD_MARK, "").strip()
            if line:
                blocks.append({"tag": match.group(1).lower(), "text": line, "bold": bold})
    return blocks


# -------------------------------------------------------------------------- filter


def matches(text: str, needles: list[str]) -> bool:
    lowered = (text or "").lower()
    return any(needle.lower() in lowered for needle in needles)
