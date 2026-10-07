# dish-finder

Find restaurants nearby whose website or PDF menu mentions a specific dish or drink.

OpenStreetMap only (Nominatim + Overpass) — no API keys, no costs.

## Usage

```bash
uv run dish-finder Tafelspitz --near "Wien Neubau" --radius 600
uv run dish-finder "Club Mate" Mate --coords 48.2 16.35 --whole-word -v
```

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
4. `matching.py` — case/umlaut-insensitive search (`Spätzle` == `spaetzle`), prints a snippet per hit.

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
