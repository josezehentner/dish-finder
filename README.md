# dish-finder

Find restaurants nearby whose website or PDF menu mentions a specific dish or drink.

OpenStreetMap only (Nominatim + Overpass) — no API keys, no costs.

## Usage

```bash
uv run main.py Tafelspitz --near "Wien Neubau" --radius 600
uv run main.py "Club Mate" Mate --coords 48.2 16.35 --whole-word -v
```

Several terms = any of them counts. `-v` also lists places without hits,
`--debug` logs every fetched URL and why pages were skipped. All settings
(timeouts, mirrors, menu keywords, defaults) live in `config.py`.

## How it works

1. `osm.py` — geocodes the place and fetches all restaurants/cafés/bars/pubs in the radius with a `website` tag.
2. `crawler.py` — per restaurant: homepage → links that look like menus (*Karte, Menü, Speise, Getränke, pdf, …*) → up to 2 levels deep. Reads HTML text and PDF text, respects `robots.txt`.
   The same page or PDF under several URLs (http/https, `www.`, trailing slash, mirrors) is only counted once.
3. `matching.py` — case/umlaut-insensitive search (`Spätzle` == `spaetzle`), prints a snippet per hit.

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
