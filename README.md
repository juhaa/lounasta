# lounasta

Today's lunch for a handful of Helsinki restaurants on one page, plus the
command-line fetcher behind each restaurant. Python 3.10+, standard library
only — nothing to install. Every fetch is cached under `~/.cache/lounasta/` for
6 hours; `--no-cache` forces a fresh request.

## The page

```
./lounasta_web.py                      # http://localhost:8000
./lounasta_web.py --port 9000 --open   # another port, opens a browser
./lounasta_web.py --build lounas.html  # write a static page instead
./lounasta_web.py --json               # the same data as JSON
```

While the server runs:

| URL | |
| --- | --- |
| `/` | today's menus |
| `/?date=2026-09-23` | another day (also `tomorrow`, `pe`, `23.09.`) |
| `/?refresh=1` | skip the cache for this load |
| `/api` | the same data as JSON (takes `date` too) |

Sources are fetched in parallel and each is isolated: a site that is down or
has been redesigned shows what went wrong on its own card while the rest of
the page still renders. A connection that is merely reset or timing out is
retried a couple of times first, and what survives that is reported in a
sentence — with a retry link and the technical detail folded away behind
"Tekninen tieto". Which restaurants appear is the `SOURCES` list at the top of
`lounasta_web.py` — each entry is one fetcher plus its options, so following a
different UniCafe or another Scandic hotel is a one-line edit. `"prices": False`
there hides per-dish prices for restaurants that charge one price for the whole
lunch; the command-line scripts always print them.

Every fetcher also exposes `menu_for(date)` returning the same normalised
shape the page uses, which is the seam to add a new restaurant on.

### Diet markings

Each site marks the same diets its own way — vegan is `Veg` at UniCafe, `VEGA`
at HUS and `V` at Scandic, Meiccu, Viisi Penniä and il Trio. The page maps them
onto the spelling that is already the most widely used one, and prints the list
it actually used in a "Merkinnät" box under the cards:

| | |
| --- | --- |
| `G` | gluteeniton |
| `L` | laktoositon |
| `VL` | vähälaktoosinen |
| `M` | maidoton |
| `Mu` | munaton |
| `V` | vegaani |
| `Kasvis` | kasvisruoka |
| `Kela` | Kela-ateriatuen kriteerit täyttävä (UniCafe) |
| `Ilmastovalinta` | ilmastovalinta (UniCafe) |

No abbreviation was invented: each canonical form is one a restaurant already
prints, so `Kasvis` and `Ilmastovalinta` stay long. Unknown markings pass
through unchanged rather than being dropped. The mapping lives in
`lounas_common.normalize_diets()`; the individual `lounas_*.py` scripts keep
each site's own spelling, so only the combined page unifies them.

| Script | Source |
| --- | --- |
| `lounasta_web.py` | the combined page and its local server |
| `lounas_unicafe.py` | https://menu.unicafe.fi/ (UniCafe / HYY) |
| `lounas_hus.py` | https://menu.hus.fi/ (HUS, CGI Aromi eMenus) |
| `lounas_scandic.py` | https://menu.scandichotels.com/ (Scandic hotels, PDF menus) |
| `lounas_meiccu.py` | https://meiccu.fi/lounas/ (Meiccu, plain WordPress page) |
| `lounas_lounastaja.py` | lounas.app widget feed — Viisi Penniä and any other restaurant on it |
| `lounas_iltrio.py` | https://iltrio.fi/lounas/ (il Trio, plain WordPress page) |
| `lounas_common.py` | shared HTTP, cache, date parsing, HTML flattening and filtering |
| `pdf_text.py` | minimal PDF text extraction, standard library only |

Common conventions: `-d/--date` accepts `today` (default), `tomorrow`,
`yesterday`, `all`, a weekday (`ke`, `wed`, `friday`), `2026-09-23` or `23.09.`;
`--json` prints machine-readable output, `--raw` dumps the untouched API
response, `--list-restaurants` shows what can be asked for.

## Hosting

The page is published at <https://juhaa.github.io/lounasta/>, built by
`.github/workflows/pages.yml` and served from GitHub Pages.

Because a static page cannot fetch anything itself, it is rebuilt on a
schedule instead, and which day it is built for follows the clock:

| Helsinki | UTC cron | builds |
| --- | --- | --- |
| 06:07-14:37, twice an hour | `7,37 4-11 * * *` | today |
| 16:12-23:12, hourly | `12 14-20 * * *` | tomorrow |

A restaurant that publishes its menu late in the morning therefore appears
within half an hour rather than the next day, and the page is never left
sitting overnight on a day that is already over. The two windows are kept
apart in UTC so they cannot overlap under either EET or EEST. The job also
runs on every push to `main` that touches something other than prose, and can
be started by hand from the Actions tab.

The odd minutes are deliberate. GitHub delays scheduled runs under load and
drops them outright when it is busy enough, and the top of the hour is its
busiest moment; an earlier `*/30` schedule, landing on `:00` and `:30`, lost
most of its runs. Odd minutes are asked for instead, and fewer of them. Even
so the schedule is best-effort and nothing more: if the page ever has to be
fresh to the minute, the reliable arrangement is an outside cron service
calling the `workflow_dispatch` API rather than GitHub's own scheduler.

The choice between today and tomorrow is made from the Helsinki hour at build
time — after 15:00 it is tomorrow — rather than from which cron entry fired,
so a manual or push-triggered run in the evening builds tomorrow too.

Before anything is published the built page is read back by `check_page.py`,
which is the difference between a page that renders and a page worth looking
at. A single restaurant being down does not stop the deploy — six cards beat
yesterday's seven — but a page that lost a card outright, or came back with no
menus at all, does. Whether every source truly answered is then reported by a
separate job that runs *after* the deploy, so a restaurant that has quietly
changed its site turns the run red and sends the usual failure mail without
ever holding up the page.

The build runs with `TZ=Europe/Helsinki`, so "today" is the Helsinki day and
not the runner's UTC one. Each run starts on a fresh runner with an empty
cache, so every build refetches all six sources; `--no-cache` is not needed.
Nothing is committed back to the repository — the page is uploaded as a Pages
artifact, so the history stays clean.

Only `/` exists on the hosted page. The query parameters and the JSON endpoint
in the table above belong to the local server, which still runs exactly as
before:

| | |
| --- | --- |
| hosted | one day's menus: today until mid-afternoon, then tomorrow |
| `./lounasta_web.py` | all of it — `?date=`, `?refresh=1`, `/api` |

One thing to know about the schedule: GitHub disables a repository's cron
workflows after 60 days without activity, and mails a warning first. A push,
or one press of "Run workflow", resets the clock.

## Development

`main` is the publish button: what is merged there is built and deployed by
`pages.yml`. Work therefore happens on a branch and arrives through a pull
request, where `ci.yml` runs the same build without deploying anything. It
compiles every module, builds both today's and tomorrow's page — two different
paths through `resolve_date` and through each fetcher's week handling — checks
both, and keeps them as an artifact to download and look at.

Nothing about the local loop changes, and it stays the more capable of the
two: the hosted page is the same program with one flag, so there is no second
implementation to drift.

```
./lounasta_web.py --open                    # ?date=, ?refresh=1, /api
./lounasta_web.py --build /tmp/x.html --date tomorrow   # what CI builds
./check_page.py /tmp/x.html                 # what CI checks
```

`check_page.py` reads a built page back and fails if a card is missing, if a
card is showing an error, or if no card has a menu at all. An empty card is
not a failure by itself: restaurants close on Mondays and at weekends.

```
./check_page.py site/index.html                     # strict
./check_page.py site/index.html --allow-errors 2    # tolerate a source being down
./check_page.py site/index.html --expect 7          # how many cards to insist on
```

Adding a restaurant means adding its entry to `SOURCES` and raising
`--expect`, which are the two places the count is written down.

Two settings on the repository are worth having, neither of which lives in
this tree: requiring the `ci` check on pull requests to `main`, and keeping
`main` protected so the publish button cannot be pressed by accident.

## lounas_unicafe.py

The site is a React SPA fed by one WordPress REST endpoint that returns every
restaurant and about two weeks of menus:

    https://unicafe.fi/wp-json/swiss/v1/restaurants?lang=<fi|en|sv>

```
./lounas_unicafe.py                          # today, every restaurant
./lounas_unicafe.py -r chemicum -r exactum   # only these restaurants
./lounas_unicafe.py -l kumpula -d tomorrow   # one campus, tomorrow
./lounas_unicafe.py --diet veg -d all        # vegan dishes for the whole period
./lounas_unicafe.py --lang en -v --allergens # English, hours, nutrition, allergens
```

Prices default to the student rate; `--price-group` picks another (`graduate`,
`contract`, `normal`, ...). Buffet restaurants publish no per-dish price, so
none is shown for them.

## lounas_hus.py

The site is an Angular SPA on top of the Aromi eMenus REST API. Three calls,
relative to a site root such as
`https://menu.hus.fi/HUSAromieMenus/FI/Default/HUS/Biomedicum`:

1. `GET /api/Common/Page/GetPageInfo?currentpage=Restaurant` — restaurant GUIDs
2. `GET /api/GetRestaurantPublicDinerGroups?id=&startDate=&endDate=` — the diner
   group ("Lounasruokailijat") that acts as the menu filter
3. `POST /api/Common/Restaurant/RestaurantMeals?Id=&StartDate=&EndDate=` with
   that diner group as the JSON body — the menu

```
./lounas_hus.py                             # Biomedicum, today
./lounas_hus.py -m lounas -d tomorrow       # lunch meals only, tomorrow
./lounas_hus.py -r 'Meilahden sairaala-alueen lounasravintola' -d all
./lounas_hus.py --lang EN -m lunch          # English (meal names translate too)
```

`-r` takes a name substring or a GUID and refuses ambiguous matches. `--site`
points the script at another Aromi eMenus deployment; `{lang}` in it is replaced
by `--lang`. Base64 dish images are stripped before caching — they are most of
the 1.4 MB payload. Per-dish nutrient values live behind a separate endpoint
(`GetRestaurentMealNutrients`) and are not fetched.

## lounas_scandic.py

Scandic publishes menus as PDFs behind a small service:

1. `GET /RestaurantMenuService/GetMenu?hotelId=&restId=&english=` — an HTML
   fragment listing the PDFs, e.g. "lounas viiko 39 (pdf)"
2. `GET /RestaurantMenuService/GetMenufile?hotelId=&restId=&fileId=` — the PDF

The weekly lunch PDF gets a new `fileId` every week, so the script picks the
lunch file whose title carries the current ISO week number, then extracts and
parses its text.

```
./lounas_scandic.py                      # this week's lunch, today's dishes
./lounas_scandic.py -d all               # the whole week
./lounas_scandic.py --english -d all     # the English menu
./lounas_scandic.py --diet V -d all      # vegan dishes
./lounas_scandic.py --list-menus         # every PDF this restaurant publishes
./lounas_scandic.py --menu 'group menu' --text   # another PDF, raw text
./lounas_scandic.py --hotel 663 --rest rest0     # another hotel/restaurant
```

Defaults to hotel 663 / `rest0` (Scandic Marski, Helsinki). `-v` prints the
small print from the PDF (serving time, price, diet legend); `--pdf PATH` saves
the file itself. Parsing is layout-based: weekday headings start a day, and the
three dish columns under them are read left to right, so a redesigned PDF may
need the heuristics in `parse_menu()` adjusted. `--text` always works as a
fallback.

## lounas_meiccu.py

No API and no PDF: a WordPress/Elementor page where the week's menu is ordinary
HTML — one heading per weekday ("Maanantai 21.9.", "Ti 22.9."), section
headings ("Buffet 15,00", "Keittiöstä") and one paragraph per dish. The script
flattens the page to headings and paragraphs in document order and reads that
sequence, splitting each dish line into name, diet codes and price.

```
./lounas_meiccu.py                    # today
./lounas_meiccu.py -d all             # the whole week
./lounas_meiccu.py --diet V -d all    # vegan dishes
./lounas_meiccu.py -s buffet          # only the buffet section
./lounas_meiccu.py -v -d all          # with the page notes and the set menu
./lounas_meiccu.py --text             # the page's headings and paragraphs
```

Weekday headings that carry a date win; ones that do not ("Ke") are placed in
the same week. As with the Scandic PDF the parsing is shape-based, so `--text`
is the fallback if the page is rebuilt.

## lounas_lounastaja.py

https://viisipennia.fi/lounas embeds the Lounastaja widget instead of
publishing the menu itself, and the widget reads a plain JSON feed:

    https://lounastaja.app/api/v1/week/<api key>/active?language=fi
    https://lounastaja.app/api/v1/week/<api key>/<year>/<week>?language=fi

The page carries the key in `data-api-key`, so any restaurant on the platform
works: pass `--api-key`, or point `--from-url` at a page that embeds the widget
and the key is read out of the HTML.

```
./lounas_lounastaja.py                   # Viisi Penniä, today
./lounas_lounastaja.py -d all            # the whole week
./lounas_lounastaja.py --diet V -d all   # vegan dishes
./lounas_lounastaja.py --lang en -v      # English, hours, the week's note, the PDF link
./lounas_lounastaja.py --week 2026/39    # a specific ISO week
./lounas_lounastaja.py --from-url https://example.fi/lounas   # another restaurant
```

A date outside the active week fetches that week automatically; weeks that are
not published yet answer 404 and the script says so.

## lounas_iltrio.py

Another API-less WordPress page, written by hand: a bold heading per weekday
("TIISTAI 22.9." followed by "KLO 11.00–15.00"), then one paragraph per dish
whose bold first line is the name and price and whose remaining lines are the
ingredients. The script reads that shape from the flattened page, using the
bold flag to tell dish names and headings from ingredient lines.

```
./lounas_iltrio.py                  # today
./lounas_iltrio.py -d all -v        # the whole week with ingredients
./lounas_iltrio.py -q pizza -d all  # search names and ingredients
./lounas_iltrio.py --diet V -d all  # dishes marked with a diet code
./lounas_iltrio.py --text           # the page's text blocks
```

Monday is closed and prints as such. Diet codes are rare on this page — the
legend is printed under the menu (`-v`) rather than per dish — so `--diet`
matches only the dishes that do carry one.

## pdf_text.py

A small PDF text extractor, used because no PDF library is assumed to be
installed: object scanning, object streams, Flate decoding, `/ToUnicode` CMaps
(with `/Encoding /Differences` as fallback) and the text operators, with output
ordered by position on the page rather than by emission order. Encrypted files,
CID fonts without a `/ToUnicode` map and scanned images are out of scope and
raise `PdfTextError`.

```python
import pdf_text
text = pdf_text.extract_text(open("menu.pdf", "rb").read())
pages = pdf_text.extract_pages(data)   # one list of lines per page
```
