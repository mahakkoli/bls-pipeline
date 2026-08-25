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
- [x] `app/streamlit_app.py` — "US Labor Market Explorer" chat UI backed by Claude
  - Talks to the DB tools over the *real* MCP protocol: spawns `mcp_server/server.py` as a stdio subprocess per question via the `mcp` Python client (not a direct function import) — a fresh session per turn, since keeping an async subprocess/session alive across Streamlit's rerun-per-interaction model is fragile
  - `st.session_state` holds both the full Claude message history (so follow-ups have context) and the display history; UI shows only the last 3 exchanges (older ones are still in Claude's context, just not rendered) with a caption noting that when truncated
  - Claude's final turn is always a forced `format_response` tool call (`{answer, chart, followups}`), not free text, so the UI never has to parse prose — model is capped to 2 follow-up questions, enforced both in the tool's JSON schema and defensively in code
  - Charts render with Plotly only when the model actually returns chart data (never forced) — bar for comparisons, line for trends
  - Custom CSS: card-style assistant bubbles, pill/chip-style buttons for starter and follow-up questions, off-white background instead of default Streamlit gray
  - Bug caught and fixed: Streamlit's markdown renderer treats bare `$...$` as inline LaTeX math, which was mangling currency figures like "$174,900" in answers (rendering the text between two `$` as a math/code span) — fixed by backslash-escaping markdown-special characters before rendering, plus instructing the model not to use markdown in the `answer` field
  - Verified with real Playwright browser automation (not just calling the async functions directly): starter question → chart renders → exactly 2 follow-up buttons → click one → multi-turn follow-up correctly uses prior context → zero console errors
- [ ] Deploy backend to Railway
- [ ] Deploy frontend to Streamlit Cloud

## Round 2 — CPI

- [ ] Not started

## Notes

- BLS API key lives in `.env` as `BLS_API_KEY` (registered key: 50 series/request, 20-year span/request, 500 requests/day)
- Both ingestion scripts dedupe on rerun (`DELETE` + `INSERT` keyed on series identity + time period), so re-running is safe
- Series ID formats were verified against BLS's own flat-file definitions at `download.bls.gov/pub/time.series/{oe,ce}/` rather than trusted from memory, since the OEWS national area-type bug showed those are easy to get subtly wrong
