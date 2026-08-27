"""Streamlit frontend for exploring BLS labor market data.

A chat UI backed by Claude. The three data tools (search_metadata,
get_series_values, query_database) are called over the real MCP
protocol against mcp_server/server.py — this app never touches DuckDB
directly. Locally that server is spawned as a stdio subprocess; in
production (MCP_SERVER_URL set) it instead connects over SSE to the
server already running on Railway, since Streamlit Cloud and Railway
are separate hosts with no way to spawn/pipe across them. Either way, a
fresh MCP session is opened per question and closed when the turn
finishes, which keeps the async lifecycle simple under Streamlit's
rerun-per-interaction model (no long-lived subprocess/session has to
survive across reruns).

Claude's final answer is always a structured `format_response` tool
call, not free text, so the UI can reliably render an answer, an
optional chart, and follow-up questions:

    {"answer": str, "chart": {...} | None, "followups": [str, ...]}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import anthropic
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server.prompts import SYSTEM_PROMPT  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Local dev: spawn mcp_server/server.py as a stdio subprocess (default).
# Production (Streamlit Cloud -> Railway): set MCP_SERVER_URL to the
# deployed server's SSE endpoint (e.g. "https://<app>.up.railway.app/sse")
# and this connects over HTTP instead — nothing on Streamlit Cloud can
# spawn or pipe to a process running on a separate Railway host.
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL")

MCP_SERVER_PARAMS = StdioServerParameters(
    command=sys.executable,
    args=["-m", "mcp_server.server"],
    cwd=str(PROJECT_ROOT),
)

MODEL = "claude-sonnet-5"
MAX_TOOL_ITERATIONS = 6
MAX_VISIBLE_EXCHANGES = 3  # keep the UI from getting cluttered on a long chat
MAX_QUESTIONS_PER_SESSION = 10  # cap Anthropic/BLS API usage per browser session

# One per dataset, so the buttons themselves communicate what the app can
# do: current metro wages (oews_wages), national industry trends
# (ces_employment), multi-year occupation wage history (oews_historical),
# and inflation-adjusted real wages (oews_historical + cpi_data).
STARTER_QUESTIONS = [
    "How do software developer salaries in Seattle compare to the national average?",
    "What's the employment trend in the information sector over the past year?",
    "How has Registered Nurses' pay changed nationally from 2019 to 2024?",
    "Have Retail Salespersons' wages kept up with inflation since 2021?",
]

# (number, label) — note "2019-2024", not "2019-2025": oews_historical's
# 6 years of data run through 2024, and oews_wages' 2025 snapshot is a
# separate single-year table, not part of that 6-year trend span.
STATS = [
    ("861", "Occupations"),
    ("6", "Years of Data (2019-2024)"),
    ("12", "Metro Areas"),
    ("4", "BLS Datasets"),
]

# A plain triple-quoted block with single newlines won't do here — CommonMark
# collapses single line breaks within a paragraph, so the header and its
# detail lines would all run together on one line. Each line needs an
# explicit forced break ("  \n"), built here rather than as invisible
# trailing whitespace in source (which editors/formatters tend to strip).
_ABOUT_DATA_SECTIONS = [
    (
        "OEWS — Current Occupational Wages (2025)",
        [
            "650 occupation-metro combinations across 12 major metros + national.",
            "Wages: mean, median, 10th/25th/75th/90th percentiles.",
            "Source: BLS Occupational Employment & Wage Statistics",
        ],
    ),
    (
        "OEWS Historical — Wage Trends (2019-2024)",
        [
            "861 occupations at national level, 6 years of history.",
            "Use for: how has pay in X occupation changed over time?",
            "Source: BLS OEWS annual flat files",
        ],
    ),
    (
        "CES — Employment Trends (2021-2026)",
        [
            "Monthly job totals by industry, national level.",
            "Use for: is hiring growing or shrinking in X industry?",
            "Source: BLS Current Employment Statistics",
        ],
    ),
    (
        "CPI — Inflation Index (2019-2026)",
        [
            "Monthly price index across 4 categories: All Items, Shelter, Medical Care, Food.",
            "Use for: have wages kept up with inflation?",
            "Source: BLS Consumer Price Index",
        ],
    ),
]

ABOUT_DATA_MARKDOWN = "\n\n".join(
    f"**{header}**  \n" + "  \n".join(lines) for header, lines in _ABOUT_DATA_SECTIONS
)

FOOTER_HTML = (
    '<div class="app-footer">'
    "Data sourced from Bureau of Labor Statistics (BLS.gov) &middot; "
    "Built with Claude + MCP &middot; github.com/mahakkoli/bls-pipeline"
    "</div>"
)

FULL_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + "\n\nRESPONSE FORMAT:\n"
    "When you are ready to give your final answer, call the `format_response` "
    "tool exactly once with your answer, an optional chart, and 0-2 natural "
    "follow-up questions. Do not reply with plain text — format_response is "
    "the only way to end the conversation turn. Write the `answer` field as "
    "plain prose with no markdown formatting (no backticks, asterisks, "
    "bullet points, or headers) — it is displayed as-is in a chat bubble."
)

# These three schemas mirror the tools mcp_server/server.py exposes over
# MCP (same names/descriptions/parameters) so Claude sees the identical
# tool surface regardless of transport.
SEARCH_METADATA_TOOL: dict[str, Any] = {
    "name": "search_metadata",
    "description": (
        "Use this tool FIRST before writing any SQL query. Returns the full schema of "
        "available BLS tables including table names, column names, column descriptions, "
        "and example values. Call this whenever you are unsure what tables or columns "
        "exist, what valid values look like, or how the data is structured."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

GET_SERIES_VALUES_TOOL: dict[str, Any] = {
    "name": "get_series_values",
    "description": (
        "Use this tool to look up the exact BLS label for a location, occupation, or "
        "industry before querying. Users will type natural language like 'Chicago' or "
        "'data engineer' — this tool maps those to the exact values stored in the "
        "database. Always call this before filtering by location, occupation, or "
        "industry in a query_database call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "field": {
                "type": "string",
                "description": "Column name to search, e.g. 'area_name', 'occupation_title', 'industry_name'.",
            },
            "search_term": {
                "type": "string",
                "description": "What the user typed, e.g. 'Chicago' or 'data engineer'.",
            },
        },
        "required": ["field", "search_term"],
    },
}

QUERY_DATABASE_TOOL: dict[str, Any] = {
    "name": "query_database",
    "description": (
        "Executes a SQL query against the BLS DuckDB database and returns results as "
        "structured data. Use this to retrieve wages, employment figures, trends, and "
        "rankings. You may call this multiple times per user question to build a "
        "complete answer. Always use search_metadata and get_series_values before "
        "constructing your SQL."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A DuckDB SELECT query."},
        },
        "required": ["sql"],
    },
}

# Client-side only — not part of the MCP server, this is how Claude hands
# its final answer back to the UI in a shape it can reliably render.
FORMAT_RESPONSE_TOOL: dict[str, Any] = {
    "name": "format_response",
    "description": (
        "Deliver your final answer to the user. Call this exactly once, as your last "
        "action, after gathering whatever data you need via search_metadata / "
        "get_series_values / query_database."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": "The written answer to show the user, following the ANSWER STYLE rules in your instructions.",
            },
            "chart": {
                "type": ["object", "null"],
                "description": (
                    "A chart backing your answer, or null if a chart wouldn't add "
                    "anything (e.g. a single-number answer with nothing to compare)."
                ),
                "properties": {
                    "type": {"type": "string", "enum": ["bar", "line"]},
                    "title": {"type": "string"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "labels": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Category names for a bar chart (e.g. metro areas), or time periods for a line chart (e.g. 'Jan 2025').",
                            },
                            "values": {"type": "array", "items": {"type": "number"}},
                            "value_label": {
                                "type": "string",
                                "description": "What the values represent, e.g. 'Annual mean wage ($)'.",
                            },
                        },
                        "required": ["labels", "values", "value_label"],
                    },
                },
                "required": ["type", "title", "data"],
            },
            "followups": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 2,
                "description": "0-2 natural follow-up questions the user might want to ask next.",
            },
        },
        "required": ["answer", "chart", "followups"],
    },
}

ALL_TOOLS = [
    SEARCH_METADATA_TOOL,
    GET_SERIES_VALUES_TOOL,
    QUERY_DATABASE_TOOL,
    FORMAT_RESPONSE_TOOL,
]

CUSTOM_CSS = """
<style>
.stApp {
    background: #f5f6fa;
}
h1 {
    color: #111827;
    font-weight: 800;
}
[data-testid="stCaptionContainer"] {
    color: #4b5563;
}
.app-subtitle {
    color: #4b5563;
    font-size: 1.25rem;
    margin: 0 0 0.5rem 0;
}
[data-testid="stChatMessageContent"] {
    border-radius: 16px !important;
    padding: 14px 18px !important;
}
[data-testid="stChatMessageContent"][aria-label="Chat message from assistant"] {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
}
[data-testid="stChatMessageContent"][aria-label="Chat message from user"] {
    background: #eef2ff;
    border: 1px solid #e0e7ff;
}
div[data-testid="stButton"] button {
    border-radius: 14px;
    border: 1px solid #c7d2fe;
    background: #eef2ff;
    color: #3730a3;
    padding: 0.5rem 1.1rem;
    font-size: 0.88rem;
    font-weight: 500;
    transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
}
div[data-testid="stButton"] button:hover {
    background: #4f46e5;
    border-color: #4f46e5;
    color: #ffffff;
}
div[data-testid="stChatInput"] {
    border-radius: 20px;
    margin-top: 24px;
    box-shadow: 0 2px 10px rgba(15, 23, 42, 0.08);
}
textarea[data-testid="stChatInputTextArea"] {
    font-size: 1.1rem !important;
    padding: 16px 18px !important;
}
button[data-testid="stChatInputSubmitButton"]:not(:disabled) {
    background: #4f46e5 !important;
    border-radius: 10px !important;
}
button[data-testid="stChatInputSubmitButton"]:not(:disabled) svg {
    fill: #ffffff !important;
}
.stat-card {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 14px;
    padding: 18px 12px;
    text-align: center;
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
}
.stat-card .stat-number {
    font-size: 1.9rem;
    font-weight: 800;
    color: #4f46e5;
    line-height: 1.2;
}
.stat-card .stat-label {
    font-size: 0.85rem;
    color: #6b7280;
    margin-top: 4px;
}
.app-footer {
    text-align: center;
    color: #9ca3af;
    font-size: 0.8rem;
    margin-top: 40px;
    padding: 16px 0 4px 0;
}
</style>
"""


async def call_mcp_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await session.call_tool(name, arguments)
    except Exception as exc:
        return {"error": f"MCP tool call failed: {exc}"}

    if result.structured_content is not None:
        return result.structured_content

    for block in result.content:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return {"error": block.text}

    return {"error": f"{name} returned no content"}


def _normalize_result(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "answer": raw.get("answer") or "I wasn't able to put together an answer for that.",
        "chart": raw.get("chart"),
        "followups": (raw.get("followups") or [])[:2],
    }


async def ask_analyst(
    client: anthropic.AsyncAnthropic,
    session: ClientSession,
    messages: list[dict[str, Any]],
    question: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one turn of the tool-use loop. Returns the updated message
    history (plain Anthropic message dicts) and the structured result."""
    messages = [*messages, {"role": "user", "content": question}]

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=FULL_SYSTEM_PROMPT,
            messages=messages,
            tools=ALL_TOOLS,
        )
        messages = [*messages, {"role": "assistant", "content": response.content}]

        format_result: dict[str, Any] | None = None
        tool_result_blocks: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "format_response":
                format_result = block.input
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Response delivered to user.",
                    }
                )
            else:
                tool_output = await call_mcp_tool(session, block.name, block.input)
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(tool_output, default=str),
                    }
                )

        if tool_result_blocks:
            messages = [*messages, {"role": "user", "content": tool_result_blocks}]

        if format_result is not None:
            return messages, _normalize_result(format_result)

        if response.stop_reason != "tool_use":
            break

    # Model didn't call format_response on its own within the budget; force it.
    response = await client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=FULL_SYSTEM_PROMPT,
        messages=messages,
        tools=[FORMAT_RESPONSE_TOOL],
        tool_choice={"type": "tool", "name": "format_response"},
    )
    messages = [*messages, {"role": "assistant", "content": response.content}]
    for block in response.content:
        if block.type == "tool_use" and block.name == "format_response":
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "Response delivered to user.",
                        }
                    ],
                },
            ]
            return messages, _normalize_result(block.input)

    return messages, _normalize_result({})


def _mcp_connection():
    if MCP_SERVER_URL:
        return sse_client(MCP_SERVER_URL)
    return stdio_client(MCP_SERVER_PARAMS)


async def run_turn(
    messages: list[dict[str, Any]], question: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Connect to the MCP server, run one full turn against it, and tear
    the connection back down. Locally this spawns mcp_server/server.py as
    a stdio subprocess; in production (MCP_SERVER_URL set) it connects to
    the already-running Railway deployment over SSE instead. Either way, a
    fresh connection per turn avoids having to keep an async
    subprocess/session alive across Streamlit's rerun-per-interaction
    script model."""
    async with _mcp_connection() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            return await ask_analyst(client, session, messages, question)


def render_stats_bar() -> None:
    cols = st.columns(len(STATS))
    for col, (number, label) in zip(cols, STATS):
        col.markdown(
            f'<div class="stat-card"><div class="stat-number">{number}</div>'
            f'<div class="stat-label">{label}</div></div>',
            unsafe_allow_html=True,
        )


def _escape_markdown(text: str) -> str:
    # Streamlit's markdown renderer treats bare $...$ as inline LaTeX math,
    # which mangles ordinary currency figures like "$174,900" in the
    # model's prose, and backticks/asterisks trigger code/emphasis
    # formatting. Escape markdown-special characters so text renders as
    # plain text regardless of what punctuation the model uses.
    return (
        text.replace("\\", "\\\\")
        .replace("$", "\\$")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
    )


def render_answer_text(answer: str) -> None:
    st.markdown(_escape_markdown(answer))


def render_chart(chart: dict[str, Any] | None) -> None:
    if not chart:
        return
    data = chart.get("data") or {}
    labels = data.get("labels") or []
    values = data.get("values") or []
    if not labels or not values:
        return
    value_label = data.get("value_label", "Value")

    if chart.get("type") == "line":
        fig = go.Figure(
            go.Scatter(
                x=labels,
                y=values,
                mode="lines+markers",
                line=dict(color="#4f46e5", width=3),
                marker=dict(size=7, color="#4f46e5"),
            )
        )
    else:
        fig = go.Figure(go.Bar(x=labels, y=values, marker=dict(color="#4f46e5")))

    # The title is rendered as regular markdown, not Plotly's own SVG
    # title, because Plotly titles don't wrap — a long title just gets
    # clipped at narrow (mobile) widths instead of flowing to a second
    # line the way every other text element on this page already does.
    title = chart.get("title", "")
    if title:
        st.markdown(f"**{_escape_markdown(title)}**")

    fig.update_layout(
        yaxis_title=value_label,
        template="plotly_white",
        height=340,
        margin=dict(l=40, r=20, t=20, b=40),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def handle_question(question: str) -> None:
    if st.session_state.question_count >= MAX_QUESTIONS_PER_SESSION:
        # The UI-level gate in main() should prevent reaching here at all
        # (chips/input are hidden once the limit hits), but guard anyway
        # in case a stale button from a prior render still fires.
        return
    st.session_state.question_count += 1

    st.session_state.display_messages.append({"role": "user", "text": question})
    with st.spinner("Analyzing BLS data..."):
        try:
            st.session_state.anthropic_messages, result = asyncio.run(
                run_turn(st.session_state.anthropic_messages, question)
            )
        except Exception as exc:
            print(f"[streamlit_app] turn failed: {exc!r}", file=sys.stderr)
            result = {
                "answer": (
                    "I couldn't reach the BLS data server just now. Please try "
                    "again in a moment."
                ),
                "chart": None,
                "followups": [],
            }
    st.session_state.display_messages.append({"role": "assistant", **result})
    st.rerun()


def main() -> None:
    st.set_page_config(page_title="US Labor Market Explorer", page_icon="📊", layout="wide")
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.title("US Labor Market Explorer")
    st.markdown(
        '<p class="app-subtitle">Natural language Q&amp;A over real Bureau of Labor Statistics data</p>',
        unsafe_allow_html=True,
    )

    if not ANTHROPIC_API_KEY:
        st.error("ANTHROPIC_API_KEY is not set. Add it to .env and restart the app.")
        st.stop()

    st.session_state.setdefault("display_messages", [])
    st.session_state.setdefault("anthropic_messages", [])
    st.session_state.setdefault("question_count", 0)

    # 2. Stats bar
    render_stats_bar()

    # 3. Input bar is defined at the bottom of this function (see the
    # comment there for why) but is numbered here since that's where it
    # visually renders: fixed to the bottom of the page, below everything.

    # The conversation thread, if any.
    visible_messages = st.session_state.display_messages[-(MAX_VISIBLE_EXCHANGES * 2) :]
    if len(visible_messages) < len(st.session_state.display_messages):
        st.caption(
            f"Showing your last {MAX_VISIBLE_EXCHANGES} exchanges — "
            "Claude still remembers the full conversation."
        )

    for i, msg in enumerate(visible_messages):
        if msg["role"] == "user":
            with st.chat_message("user"):
                st.write(msg["text"])
        else:
            with st.chat_message("assistant"):
                render_answer_text(msg["answer"])
                render_chart(msg.get("chart"))
                is_latest = i == len(visible_messages) - 1
                followups = msg.get("followups") or []
                if is_latest and followups:
                    cols = st.columns(len(followups))
                    for col, followup in zip(cols, followups):
                        if col.button(followup, key=f"followup_{i}_{followup}"):
                            handle_question(followup)

    # Rate limit: once the session hits MAX_QUESTIONS_PER_SESSION, stop
    # here so nothing below this line renders — no chips, no input bar —
    # while leaving the conversation above still visible. Using >= (not
    # the > 10 in the original request) so the limit is exactly 10 asked
    # questions: question_count is incremented inside handle_question
    # before this check runs on the next rerun, so a strict > would let
    # an 11th question through.
    if st.session_state.question_count >= MAX_QUESTIONS_PER_SESSION:
        st.warning("You've reached the limit for this session. Refresh to continue.")
        st.stop()

    # 4. Suggested question chips — secondary prompts below the input
    if not st.session_state.display_messages:
        st.write("Try asking:")
        cols = st.columns(len(STARTER_QUESTIONS))
        for col, starter_question in zip(cols, STARTER_QUESTIONS):
            if col.button(starter_question, key=f"starter_{starter_question}"):
                handle_question(starter_question)

    # 5. About the data
    with st.expander("About the data"):
        st.markdown(ABOUT_DATA_MARKDOWN)

    # 6. Footer
    st.markdown(FOOTER_HTML, unsafe_allow_html=True)

    # Bottom: input bar, fixed/sticky. Calling st.chat_input() unwrapped
    # (not nested inside a st.container()) is what makes Streamlit pin it
    # to the bottom of the page — its native behavior. Its position in
    # the script doesn't affect where it renders; it always floats fixed
    # at the true bottom of the viewport regardless of call order, which
    # is why it's fine to call it last, after the footer.
    question = st.chat_input("Ask anything about jobs, wages, or hiring trends...")
    if question:
        handle_question(question)


if __name__ == "__main__":
    main()
