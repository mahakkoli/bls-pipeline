# Progress

Agentic BLS labor market data pipeline. Python + DuckDB + MCP server + Streamlit, hosted on Railway (backend) / Streamlit Cloud (frontend).

## Round 1 — OEWS + CES

- [x] Project scaffold: `ingestion/`, `mcp_server/`, `app/`, `data/`, `.env`, `.gitignore`, `requirements.txt`
- [x] `ingestion/bls_client.py` — shared BLS Public Data API v2 client (batching, retries, 50-series/20-year limits)
- [x] `ingestion/db.py` — shared DuckDB connection helper (`data/bls.duckdb`)
- [x] `ingestion/oews.py` — OEWS occupational wages by metro, loads `oews_wages` table
  - 13 areas (national + 12 metros) x 10 occupations x 5 metrics = 650 series
  - Fixed bug: national rows need BLS area-type code `"N"`, not `"U"` (verified against BLS's `oe.areatype`/`oe.series` reference files)
  - Verified end-to-end against live API: 650 rows loaded, spot-checked against published wage figures
  - Current-year snapshot only (2025) — BLS's timeseries API doesn't retain OEWS history under stable series IDs (every series in `oe.series` has `begin_year = end_year` = latest vintage); confirmed via API, decided not worth pulling historical vintages from BLS's separate flat files for now
- [x] `ingestion/ces.py` — CES monthly employment by industry, loads `ces_employment` table
  - 14 national supersectors x 4 metrics (employment, avg weekly hours, avg hourly earnings, avg weekly earnings) = 56 series, seasonally adjusted
  - Defaults to a 6-year pull (`current_year - 5` through `current_year`)
  - Verified end-to-end against live API: 3,350 rows loaded, 2021-2026 (6 series legitimately empty — BLS doesn't publish hours/earnings for "Total nonfarm" or "Government" level)
- [x] `mcp_server/server.py` — MCP server "bls-analyst" exposing `oews_wages`/`ces_employment` to agent clients
  - Built on the `mcp` v2 SDK (`mcp.server.mcpserver.MCPServer`) — note the package was 2.x at install time, which renamed `FastMCP` to `MCPServer` from the older 1.x API most examples online still show
  - Single persistent read-only DuckDB connection to `data/bls.duckdb`, opened once at startup; stdio transport for local dev
  - Loads `ANTHROPIC_API_KEY` from `.env` at startup
  - Three tools: `search_metadata` (live schema + column descriptions + 3 sample values/column), `get_series_values` (fuzzy ILIKE lookup of real DB values, validated against a live column allowlist), `query_database` (arbitrary SELECT, wrapped in a subquery to enforce a 100-row cap and reject non-SELECT statements)
  - Every tool wraps its body in try/except and returns a descriptive error string instead of raising, so a client LLM can self-correct
  - Verified with a real stdio `ClientSession` round-trip (not just calling the Python functions directly): server boots, lists exactly 3 tools with the exact required descriptions, and `call_tool` returns correct structured results
  - Analyst system prompt embedded via the MCP `instructions` field (the standard mechanism for a server to hand a connecting LLM its operating guidance on `initialize`) — verified it round-trips over the real protocol
- [x] `mcp_server/prompts.py` — `SYSTEM_PROMPT` extracted into its own side-effect-free module so both `mcp_server/server.py` and `app/streamlit_app.py` can import it without the app also opening a second DuckDB connection
  - Added a DATA CAPABILITIES section plus NEVER DO/ALWAYS DO rules: no dataset supports an occupation-specific employment *trend* (OEWS has no time dimension, CES has no occupation dimension) — the model must neither answer nor suggest follow-ups implying one exists. Verified live: a direct ask for "employment trend for registered nurses" now gets an honest explanation instead of a fabricated answer, and follow-up suggestions stay within OEWS (occupation x area snapshot) or CES (industry-level trend) boundaries
- [x] `app/streamlit_app.py` — "US Labor Market Explorer" chat UI backed by Claude
  - Talks to the DB tools over the *real* MCP protocol: spawns `mcp_server/server.py` as a stdio subprocess per question via the `mcp` Python client (not a direct function import) — a fresh session per turn, since keeping an async subprocess/session alive across Streamlit's rerun-per-interaction model is fragile
  - `st.session_state` holds both the full Claude message history (so follow-ups have context) and the display history; UI shows only the last 3 exchanges (older ones are still in Claude's context, just not rendered) with a caption noting that when truncated
  - Claude's final turn is always a forced `format_response` tool call (`{answer, chart, followups}`), not free text, so the UI never has to parse prose — model is capped to 2 follow-up questions, enforced both in the tool's JSON schema and defensively in code
  - Charts render with Plotly only when the model actually returns chart data (never forced) — bar for comparisons, line for trends
  - Custom CSS: card-style assistant bubbles, pill/chip-style buttons for starter and follow-up questions, off-white background instead of default Streamlit gray
  - Bug caught and fixed: Streamlit's markdown renderer treats bare `$...$` as inline LaTeX math, which was mangling currency figures like "$174,900" in answers (rendering the text between two `$` as a math/code span) — fixed by backslash-escaping markdown-special characters before rendering, plus instructing the model not to use markdown in the `answer` field
  - Verified with real Playwright browser automation (not just calling the async functions directly): starter question → chart renders → exactly 2 follow-up buttons → click one → multi-turn follow-up correctly uses prior context → zero console errors
- [x] Railway deployment scaffolding: `Procfile`, `railway.toml`, `build.sh`
  - `mcp_server/server.py` now supports dual transport, chosen via `MCP_TRANSPORT` env var (default `stdio`, unchanged for local dev): `MCP_TRANSPORT=sse` binds an HTTP/SSE server on `0.0.0.0:$PORT` instead, since Railway can't pipe stdin/stdout to a remote process the way a local parent process can. Both `Procfile` and `railway.toml`'s `startCommand` set `MCP_TRANSPORT=sse`
  - `app/streamlit_app.py`'s MCP connection is now dual-mode too: unset `MCP_SERVER_URL` (local dev) spawns the stdio subprocess as before; setting it to the deployed Railway SSE endpoint (`https://<app>.up.railway.app/sse`) connects over HTTP instead. Nothing else in the app changed — chart rendering, prompts, CSS, tool schemas are untouched
  - `railway.toml`'s `[build]` now has `buildCommand = "bash build.sh"` — without it Nixpacks never runs `build.sh` at all, so `data/bls.duckdb` wouldn't regenerate on deploy
  - Caught and fixed a bug in `build.sh` itself: running `python ingestion/oews.py` directly (script form) fails with `ModuleNotFoundError: No module named 'ingestion'`, since only the script's own directory lands on `sys.path`, not the project root. Fixed to `python -m ingestion.oews && python -m ingestion.ces` (module form), which resolves correctly from the repo root. Verified by running `build.sh` for real — both tables reload successfully
  - Verified both transport paths for real: started the server locally with `MCP_TRANSPORT=sse`, connected the Streamlit app to it via `MCP_SERVER_URL=http://localhost:8765/sse`, got a correct real answer back — and confirmed the default stdio path still works unchanged
  - Hit the same bug again on an actual Railway deploy: `python mcp_server/server.py` (script form, in both `Procfile` and `railway.toml`) threw `ModuleNotFoundError: No module named 'mcp_server'` from the `from mcp_server.prompts import SYSTEM_PROMPT` line, for the identical reason as the `build.sh` bug — the script's own directory lands on `sys.path`, not `/app`. Fixed both to `python -m mcp_server.server` (module form) and reproduced/re-verified locally with the exact same invocation shape Railway uses
- [x] Backend deployed to Railway (`MCP_TRANSPORT=sse`), frontend deployed to Streamlit Cloud (`MCP_SERVER_URL` pointed at the Railway SSE endpoint) — full stack is live end-to-end

## Round 2 — CPI

- [x] `ingestion/cpi.py` — CPI-U monthly price index, loads `cpi_data` table
  - 4 fixed headline series pulled directly (not built from component codes like OEWS/CES): `CUUR0000SA0` All Items, `CUUR0000SAH` Shelter, `CUUR0000SAM` Medical Care, `CUUR0000SAF` Food — verified against the live API before writing any code, same diligence that caught real bugs in OEWS/CES
  - Monthly, 2019 to present by default (`--start-year 2019`, `--end-year` = current year); dedupes on rerun keyed by `(series_id, year, month)`
  - Verified end-to-end against live API: 364 rows loaded (4 series x 91 months), confirmed idempotent on rerun (still 364 rows, no duplicates), 4 suppressed/null values handled correctly
  - Wired into `mcp_server/server.py` and `mcp_server/prompts.py` — see below
- [x] `ingestion/oews_historical.py` — backfills multi-year OEWS history from BLS's downloadable "National" Excel workbooks (`data/oews_historical/national_M<year>_dl.xlsx`, 2019-2024), loads `oews_historical` table
  - `ingestion/oews.py`'s timeseries-API approach only ever exposes the current published vintage (see the OEWS entry above) — this is a separate source (local flat files, not the API) specifically to get real year-over-year history
  - Caught a real error in the given spec before writing any code, verified against all 6 files: national area code is `99` (int), not `'000001'` as specified — every row in every file is `area == 99`, so filtering on the literal spec value would have silently produced zero rows in every run. Used `area == 99` instead
  - Column casing differs across years (2019-2023 files are already lowercase, 2024 is uppercase) — confirmed the "lowercase all columns" step in the spec is load-bearing, not just style
  - Year is parsed from each filename via regex rather than hardcoding a 2019-2024 range, so adding a new year's file later needs no code change
  - Verified end-to-end: 4,901 rows loaded across 2019-2024, 861 distinct occupations, confirmed idempotent on rerun (dedup keyed on `(occ_code, year)`, verified unique per year before relying on it), 29 suppressed wage values (BLS's `*`/`#` markers) correctly coerced to NULL
  - Wired into `mcp_server/server.py` and `mcp_server/prompts.py` — see below
- [x] `mcp_server/prompts.py` rewritten for 4 datasets: added a DATASETS AVAILABLE section (per-table purpose + geography scope) and CROSS-TABLE RULES, a 5-step real-wage calculation procedure (`oews_historical` + `cpi_data`) in ALWAYS DO, and NEVER DO rules for table misuse (`oews_historical`/`ces_employment` for metro questions, `oews_historical` for *current* wages, conflating nominal vs. real wage growth). Also fixed a real contradiction the additions would otherwise have created: the old prompt flatly said no dataset gives an occupation-specific trend, which became false once `oews_historical` existed — narrowed to the actual remaining gaps (no metro-level trend, no monthly granularity, nothing past 2024)
- [x] `mcp_server/server.py` wired up to actually serve `oews_historical`/`cpi_data`, not just have the prompt reference them:
  - `DATA_TABLES` extended from 2 to 4 tables; `COLUMN_DESCRIPTIONS` extended with entries for both new tables' columns, explicitly noting `oews_historical.occ_code` keeps the SOC dash (`"15-1252"`) while `oews_wages.occupation_code` has it stripped (`"151252"`) — a real formatting mismatch between the two occupation tables worth flagging so the model doesn't assume they're directly joinable strings
  - Caught and fixed a real bug while extending this: `get_series_values` hardcoded exactly 2 SQL placeholders (`WHERE table_name IN (?, ?)`) for what was always meant to scale with `DATA_TABLES` — going to 4 tables would have thrown a parameter-count mismatch on every call. Fixed to build the placeholder string dynamically from `len(DATA_TABLES)`
  - Verified for real, not just read: `search_metadata` now lists all 4 tables with correct column counts; `get_series_values` correctly resolves fields against all 4 without the placeholder bug (tested a field unique to each new table); `query_database` pulls correctly from both. Then ran a full live query through the actual Claude tool-use loop asking for software developers' real wage change 2021-2024 — it correctly pulled median wages from `oews_historical` for both years, CPI from `cpi_data`, applied the exact 5-step formula, and reported nominal (+10.2%) vs. real (-4.8%) wage change distinctly, with a chart

## Notes

- BLS API key lives in `.env` as `BLS_API_KEY` (registered key: 50 series/request, 20-year span/request, 500 requests/day)
- Both ingestion scripts dedupe on rerun (`DELETE` + `INSERT` keyed on series identity + time period), so re-running is safe
- Series ID formats were verified against BLS's own flat-file definitions at `download.bls.gov/pub/time.series/{oe,ce}/` rather than trusted from memory, since the OEWS national area-type bug showed those are easy to get subtly wrong
