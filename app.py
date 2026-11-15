"""Streamlit reader for the validated Hindsight article-history layer.

Run with: streamlit run app.py
Requires MariaDB tables produced by Steps 1–5. Question answering and date snapping are not part of this prototype yet.
"""
from __future__ import annotations

import csv
import json
import os
import ssl
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pymysql
import streamlit as st

ROOT = Path(__file__).resolve().parent
HERO_TSV = ROOT / "docs" / "hero.tsv"
DEMO_JSON = ROOT / "docs" / "demo_snapshots.json"

st.set_page_config(page_title="Hindsight", page_icon="🕰️", layout="wide")


def _database_settings() -> dict[str, Any]:
    try:
        secrets = dict(st.secrets.get("database", {}))
    except Exception:
        secrets = {}
    return {
        "host": os.environ.get("DB_HOST", secrets.get("host", "127.0.0.1")),
        "port": int(os.environ.get("DB_PORT", secrets.get("port", 3306))),
        "user": os.environ.get("DB_USER", secrets.get("user", "hindsight")),
        "password": os.environ.get("DB_PASSWORD", secrets.get("password", "hindsight")),
        "database": os.environ.get("DB_NAME", secrets.get("name", "wikihistory")),
        "unix_socket": os.environ.get("DB_SOCKET", secrets.get("socket")),
    }


def connect_db() -> pymysql.connections.Connection:
    settings = _database_settings()
    if not settings["unix_socket"] and settings["host"].strip("[]").lower() not in {"localhost", "127.0.0.1", "::1"}:
        settings["ssl"] = ssl.create_default_context()
    connection = pymysql.connect(**settings, autocommit=True, charset="utf8mb4", connect_timeout=10)
    connection.cursor().execute("SET time_zone = '+00:00'")
    return connection


@st.cache_data(show_spinner=False)
def load_catalog() -> list[dict[str, Any]]:
    with HERO_TSV.open(encoding="utf-8", newline="") as file:
        return [
            {**row, "page_id": int(row["page_id"])}
            for row in csv.DictReader(file, delimiter="\t")
        ]


@st.cache_data(show_spinner=False)
def load_demo_snapshots() -> list[dict[str, Any]]:
    return json.loads(DEMO_JSON.read_text(encoding="utf-8"))["snapshots"]


@st.cache_data(ttl=300, show_spinner=False)
def load_date_bounds() -> tuple[date, date]:
    connection = connect_db()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT MIN(row_start), MAX(row_start) FROM wikihistory.page_state FOR SYSTEM_TIME ALL")
        earliest, latest = cursor.fetchone()
        if earliest is None or latest is None:
            raise RuntimeError("The page_state history is empty. Load the Step 1 corpus first.")
        return earliest.date(), latest.date()
    finally:
        connection.close()


@st.cache_data(ttl=30, max_entries=256, show_spinner=False)
def load_snapshot(page_id: int, as_of: str) -> dict[str, Any] | None:
    """Rebuild one page at midnight UTC using the page_state, layout and claim histories."""
    timestamp = f"{as_of} 00:00:00.000000"
    connection = connect_db()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """SELECT title, rev_id, anon, is_revert, undone, row_start
               FROM wikihistory.page_state FOR SYSTEM_TIME AS OF TIMESTAMP %s
               WHERE page_id = %s""",
            (timestamp, page_id),
        )
        state = cursor.fetchone()
        if state is None:
            return None

        cursor.execute(
            """SELECT claim_order FROM wikihistory.layout FOR SYSTEM_TIME AS OF TIMESTAMP %s
               WHERE page_id = %s""",
            (timestamp, page_id),
        )
        layout_row = cursor.fetchone()
        if layout_row is None:
            order: list[int] = []
        else:
            raw_order = layout_row[0]
            order = json.loads(raw_order) if isinstance(raw_order, (str, bytes, bytearray)) else list(raw_order)

        cursor.execute(
            """SELECT claim_id, section, exact_hash, n_refs
               FROM wikihistory.claim FOR SYSTEM_TIME AS OF TIMESTAMP %s
               WHERE page_id = %s""",
            (timestamp, page_id),
        )
        claim_rows = cursor.fetchall()
        texts: dict[int, str] = {}
        if claim_rows:
            hashes = [row[2] for row in claim_rows]
            placeholders = ", ".join("%s" for _ in hashes)
            cursor.execute(
                f"SELECT exact_hash, text FROM wikihistory.text_variant WHERE exact_hash IN ({placeholders})",
                hashes,
            )
            texts = {exact_hash: text for exact_hash, text in cursor.fetchall()}

        claims = {
            claim_id: {
                "claim_id": claim_id,
                "section": section or "",
                "n_refs": n_refs,
                "text": texts.get(exact_hash, "[Exact text variant is missing.]"),
            }
            for claim_id, section, exact_hash, n_refs in claim_rows
        }
        ordered_claims = [claims[claim_id] for claim_id in order if claim_id in claims]
        return {
            "title": state[0],
            "rev_id": state[1],
            "anonymous": bool(state[2]),
            "is_revert": bool(state[3]),
            "undone": bool(state[4]),
            "row_start": state[5],
            "claims": ordered_claims,
            "missing_from_claims": sorted(set(order) - claims.keys()),
            "missing_from_layout": sorted(claims.keys() - set(order)),
        }
    finally:
        connection.close()


def render_snapshot(snapshot: dict[str, Any] | None, page_id: int, as_of: date) -> None:
    if snapshot is None:
        st.info("This article did not yet have a saved revision on that date.")
        return

    title = snapshot["title"]
    rev_id = snapshot["rev_id"]
    st.title(title)
    st.caption(f"Page {page_id} · snapshot date {as_of.isoformat()} UTC · dataset ends 1 October 2026")

    timestamp = snapshot["row_start"]
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp)
    first, second, third = st.columns(3)
    first.metric("Revision", f"{rev_id:,}")
    second.metric("Claims", f"{len(snapshot['claims']):,}")
    third.metric("References", f"{sum(int(c['n_refs']) for c in snapshot['claims']):,}")
    st.caption(f"Revision timestamp: {timestamp:%Y-%m-%d %H:%M:%S} UTC · editor class: "
               f"{'anonymous' if snapshot['anonymous'] else 'named'}")

    if snapshot["is_revert"]:
        st.info("This revision was marked as a revert.")
    if snapshot["undone"]:
        st.warning("This revision was later marked as undone. Vandalism date snapping is not implemented in this viewer yet.")
    if snapshot["missing_from_claims"] or snapshot["missing_from_layout"]:
        st.warning("The stored layout and claim set differ for this snapshot. Some text may be omitted.")

    st.link_button("Open this revision on Finnish Wikipedia", f"https://fi.wikipedia.org/?oldid={rev_id}")
    st.divider()
    if not snapshot["claims"]:
        st.info("No claim text was stored for this revision.")
        return

    current_section: str | None = None
    for claim in snapshot["claims"]:
        section = claim["section"] or "Introduction"
        if section != current_section:
            st.subheader(section)
            current_section = section
        with st.container(border=True):
            st.caption(f"Claim {claim['claim_id']} · {claim['n_refs']} reference(s)")
            st.text(claim["text"])


st.title("Hindsight")
st.caption("Explore how Finnish Wikipedia articles changed over time.")
st.info("This prototype reconstructs historical article snapshots. Question answering, comparison, and vandalism date snapping are still in development.")

try:
    catalog = load_catalog()
    earliest, latest = load_date_bounds()
except (pymysql.MySQLError, RuntimeError, OSError) as error:
    st.warning("Demo mode: the full history database is not connected. This sample includes four snapshots of Talvisota.")
    try:
        demo_snapshots = load_demo_snapshots()
    except (OSError, json.JSONDecodeError, KeyError) as demo_error:
        st.error("Hindsight could not load the database or the packaged demo sample.")
        with st.expander("Technical details"):
            st.code(f"Database: {error}\nDemo data: {demo_error}")
        st.stop()

    with st.sidebar:
        st.header("Demo snapshots")
        demo_by_date = {item["as_of"]: item for item in demo_snapshots}
        demo_date = st.selectbox(
            "Talvisota as of",
            list(demo_by_date),
            index=len(demo_by_date) - 1,
        )
        demo = demo_by_date[demo_date]
    render_snapshot(demo["snapshot"], demo["page_id"], date.fromisoformat(demo["as_of"]))
    st.markdown(
        "Sample text: [Finnish Wikipedia history dataset M](https://github.com/MariaDB/mariadb-wikipedia-datasets); "
        "Wikipedia-derived content is CC BY-SA 4.0."
    )
    with st.expander("Technical details"):
        st.code(f"Database: {error}")
    st.stop()

with st.sidebar:
    st.header("Choose a snapshot")
    search = st.text_input("Filter articles", placeholder="Search article titles")
    matching = [row for row in catalog if search.casefold() in row["title"].casefold()]
    if not matching:
        st.warning("No article titles match that filter.")
        st.stop()

    default_index = next((i for i, row in enumerate(matching) if row["title"] == "Talvisota"), 0)
    with st.form("snapshot-form"):
        selected = st.selectbox("Article", matching, index=default_index, format_func=lambda row: row["title"])
        selected_date = st.date_input("As of (UTC)", value=latest, min_value=earliest, max_value=latest)
        submitted = st.form_submit_button("Show snapshot", type="primary", use_container_width=True)
    st.caption(f"Hero set: {len(catalog)} articles · history range {earliest} to {latest} UTC")

if submitted:
    st.session_state["requested_snapshot"] = (selected["page_id"], selected_date.isoformat())

if "requested_snapshot" not in st.session_state:
    st.info("Choose an article and date in the sidebar, then select **Show snapshot**.")
else:
    page_id, as_of = st.session_state["requested_snapshot"]
    try:
        with st.spinner("Rebuilding the article snapshot…"):
            snapshot = load_snapshot(page_id, as_of)
    except pymysql.MySQLError as error:
        st.error("The article snapshot could not be loaded. Check the database connection and try again.")
        with st.expander("Technical details"):
            st.code(str(error))
    else:
        render_snapshot(snapshot, page_id, date.fromisoformat(as_of))
