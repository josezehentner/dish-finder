# dish-finder

Find restaurants nearby whose website or PDF menu mentions a specific dish or drink.

OpenStreetMap only (Nominatim + Overpass) — no API keys, no costs.

## Usage

```bash
uv run dish-finder Tafelspitz --near "Wien Neubau" --radius 600
uv run dish-finder "Club Mate" Mate --coords 48.2 16.35 --whole-word -v
uv run dish-finder Gulasch --near "Wien Westbahnhof" --radius 2000 --limit 30
```

Example output of the first command above (colored in a terminal, plain when piped or with `NO_COLOR`):

```
📍  Wien Neubau → 48.20226, 16.34912
🍴  306 places in 600 m, 189 with a website
💾  187 websites from cache, crawling 0

Tafelspitz found at 9 of 189 places
⋮
⏺ Kriterium · restaurant · 451 m
  ⎿  Google Maps  https://www.google.com/maps/search/?api=1&query=48.2013751,16.3550566
     OSM          https://www.openstreetmap.org/node/442997737
     Website      https://kriterium.at

     HTML  https://kriterium.at/speisekarte
           …mit Braterdäpfel und Fächergurkerl (A,M) 25,90 Tafelspitz mit Apfelkren, Schnittlauchsauce und Röstkartoffeln…

⏺ Witwe Bolte · restaurant · 494 m
  ⎿  Google Maps  https://www.google.com/maps/search/?api=1&query=48.2032608,16.3556243
     OSM          https://www.openstreetmap.org/node/443025473
     Website      https://www.witwebolte.at

     PDF   https://www.witwebolte.at/pdf/karte.pdf?_=1791407627
           …Parsley Potatoes 18,9 Tafelspitz vom Alpenrind Apfelkren / Schnittlauchsauce / Cremespinat…  ×2
⋮
```

Matches are listed nearest first; snippets show the menu's own spelling.

`--limit N` only searches the N nearest places that have a website (nearest first),
which keeps big radii fast.

Several terms = any of them counts. `-v` also lists places without hits,
`--debug` logs every fetched URL and why pages were skipped. All settings
(timeouts, mirrors, menu keywords, defaults) live in `config.py`.

### Cache

Everything fetched is stored in `~/.cache/dish-finder/cache.sqlite3`: place lookups,
OSM results and the extracted text of every crawled website. The first search in an
area crawls (~1–2 min); every further search there — for any dish — takes well under a second.

- Entries expire after 14 days, failed crawls (dead domain, timeout) after 1 day.
- `--refresh` fetches everything again and updates the cache.
- `--no-cache` neither reads nor writes it.
- To wipe it: `rm ~/.cache/dish-finder/cache.sqlite3`.

## How it works

The code lives in `src/dish_finder/`.

1. `osm.py` — geocodes the place and fetches all restaurants/cafés/bars/pubs in the radius with a `website` tag.
2. `crawler.py` — per website: homepage → links that look like menus (*Karte, Menü, Speise, Getränke, pdf, …*) → up to 2 levels deep. Reads HTML text and PDF text, respects `robots.txt`.
   The same page or PDF under several URLs (http/https, `www.`, trailing slash, mirrors) is only counted once.
3. `cache.py` — SQLite cache (tables `lookups`, `sites`, `documents`). Crawled text is stored independent of the search term.
4. `matching.py` — case/umlaut-insensitive search (`Spätzle` == `spaetzle`), keeps a snippet in the original spelling.
5. `output.py` — renders the report: Google Maps / OSM / website links, matches nearest first.

## Development

```bash
uv sync                          # installs runtime + dev dependencies
uv run pre-commit install        # once: run all checks before every commit

uv run pytest                    # tests (fully offline: fake HTTP, generated PDFs)
uv run ruff check --fix .        # lint
uv run ruff format .             # format
uv run mypy                      # type check (strict)
uv run pre-commit run --all-files
```

## Limits

- Places without a `website` tag in OSM are skipped.
- JavaScript-only websites (Wix etc.) and scanned/image PDFs yield no text.

## License

MIT — see [LICENSE](LICENSE).
