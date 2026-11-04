"""
Mumbai / MMR Redevelopment Tender News Finder
Searches Google News (RSS) for redevelopment tender news, pulls the full article
text and shows headline, source, link and article text.

Run:  streamlit run app.py
"""

import io
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import feedparser
import pandas as pd
import streamlit as st
import trafilatura
from googlenewsdecoder import gnewsdecoder

# ----------------------------------------------------------------------------
# KEYWORDS - edit these lists to change what is searched
# ----------------------------------------------------------------------------
TOPIC_KEYWORDS = [
    '"construction and development model"',
    '"construction and development Agency"',
    '"cluster redevelopment"',
    '"slum redevelopment"',

]

# ACTION_KEYWORDS = ["tender", "tendered", "proposal", "award", "awarded"]
ACTION_KEYWORDS = []

LOCATION_KEYWORDS = [
    "Mumbai",
    "Thane",
    '"Navi Mumbai"',
    '"Third Mumbai"',
    "MMR",
]

EXCLUDE_KEYWORDS = ["Dharavi", "Kamathipura"]

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"


# ----------------------------------------------------------------------------
# Query building
# ----------------------------------------------------------------------------
def or_group(words):
    # An empty list adds nothing to the query (avoids a bare "()")
    return "(" + " OR ".join(words) + ")" if words else ""


def build_queries(period):
    """
    One query per topic keyword. Google News returns at most ~100 results per
    query, so splitting by topic gives far more coverage than one giant query.
    Each query still contains the action words, locations and exclusions.
    """
    common = (
        f"{or_group(ACTION_KEYWORDS)} {or_group(LOCATION_KEYWORDS)} "
        + " ".join(f"-{w}" for w in EXCLUDE_KEYWORDS)
    )
    if period:
        common += f" when:{period}"
    return [" ".join(f"{topic} {common}".split()) for topic in TOPIC_KEYWORDS]


# ----------------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------------
def search_google_news(query):
    url = GOOGLE_NEWS_RSS.format(q=urllib.parse.quote(query))
    feed = feedparser.parse(url)
    results = []
    for e in feed.entries:
        source = e.get("source", {}).get("title", "")
        title = e.get("title", "")
        # Google appends " - Source Name" to the headline; strip it
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")]
        results.append(
            {
                "Headline": title.strip(),
                "Source": source,
                "Published": e.get("published", ""),
                "google_link": e.get("link", ""),
            }
        )
    return results


def decode_google_link(link):
    """Google News RSS links are redirects; convert to the real publisher URL."""
    try:
        r = gnewsdecoder(link, interval=0.3)
        if (r.get("status") or r.get("success")) and r.get("decoded_url"):
            return r["decoded_url"]
    except Exception:
        pass
    return link


def fetch_article(item):
    real_url = decode_google_link(item["google_link"])
    text = ""
    try:
        html = trafilatura.fetch_url(real_url)
        if html:
            text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except Exception:
        pass
    return {**item, "Link": real_url, "Article Text": text.strip()}


# ----------------------------------------------------------------------------
# Local filtering (applied on headline + full text)
# ----------------------------------------------------------------------------
def contains_any(text, words):
    text = text.lower()
    return any(re.search(r"\b" + re.escape(w.strip('"').lower()) + r"\b", text) for w in words)


def passes_filters(row, strict):
    blob = f"{row['Headline']} {row['Article Text']}"
    if contains_any(blob, EXCLUDE_KEYWORDS):
        return False
    if strict:
        # Must mention tender AND proposal AND award (any form)
        return (
            contains_any(blob, ["tender", "tendered", "tenders"])
            and contains_any(blob, ["proposal", "proposals"])
            and contains_any(blob, ["award", "awarded"])
        )
    return True


@st.cache_data(ttl=1800, show_spinner=False)
def run_search(period, max_articles):
    queries = build_queries(period)

    # 1. Run all Google News queries in parallel
    with ThreadPoolExecutor(max_workers=len(queries)) as ex:
        batches = list(ex.map(search_google_news, queries))

    # 2. De-duplicate by headline
    seen, items = set(), []
    for batch in batches:
        for it in batch:
            key = it["Headline"].lower()
            if key and key not in seen:
                seen.add(key)
                items.append(it)

    # newest first
    def parse_date(s):
        try:
            return datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %Z")
        except Exception:
            return datetime.min

    items.sort(key=lambda x: parse_date(x["Published"]), reverse=True)
    items = items[:max_articles]

    # 3. Fetch full article text in parallel
    with ThreadPoolExecutor(max_workers=8) as ex:
        articles = list(ex.map(fetch_article, items))

    return queries, articles


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.set_page_config(page_title="MMR Redevelopment Tender News", page_icon="🏗️", layout="wide")
st.title("🏗️ Mumbai / MMR Redevelopment Tender News")
st.caption("SRA · MHADA · MMRDA · Cluster / Slum Redevelopment — tenders, proposals & awards")

with st.sidebar:
    st.header("Options")
    period_label = st.selectbox(
        "Time period",
        ["Last 24 hours", "Last 7 days", "Last 30 days", "Last 1 year", "Any time"],
        index=2,
    )
    period = {"Last 24 hours": "1d", "Last 7 days": "7d", "Last 30 days": "30d",
              "Last 1 year": "1y", "Any time": ""}[period_label]
    max_articles = st.slider("Max articles to fetch", 10, 200, 60, step=10)
    strict = st.checkbox(
        "Strict match (article must mention tender AND proposal AND award)", value=False
    )
    hide_empty = st.checkbox("Hide articles where text could not be extracted", value=False)

if st.button("🔍 Search", type="primary", use_container_width=True):
    with st.spinner("Searching Google News and reading articles... (this can take a minute)"):
        queries, articles = run_search(period, max_articles)

    df = pd.DataFrame(articles)
    if df.empty:
        st.warning("No articles found. Try a longer time period.")
        st.stop()

    df = df[df.apply(passes_filters, axis=1, strict=strict)]
    if hide_empty:
        df = df[df["Article Text"].str.len() > 0]

    st.success(f"Found {len(df)} articles")

    with st.expander("Queries sent to Google News"):
        for q in queries:
            st.code(q)

    out = df[["Headline", "Source", "Published", "Link", "Article Text"]]

    c1, c2 = st.columns(2)
    c1.download_button("⬇️ Download CSV", out.to_csv(index=False).encode("utf-8"),
                       "redevelopment_news.csv", "text/csv", use_container_width=True)
    xbuf = io.BytesIO()
    out.to_excel(xbuf, index=False, engine="openpyxl")
    c2.download_button("⬇️ Download Excel", xbuf.getvalue(), "redevelopment_news.xlsx",
                       use_container_width=True)

    tab_cards, tab_table = st.tabs(["📰 Articles", "📊 Table"])

    with tab_cards:
        for _, r in out.iterrows():
            st.markdown(f"### [{r['Headline']}]({r['Link']})")
            st.markdown(f"**{r['Source']}** · {r['Published']}")
            with st.expander("Read article text"):
                st.write(r["Article Text"] or "_Article text could not be extracted — open the link._")
            st.divider()

    with tab_table:
        st.dataframe(out, use_container_width=True, hide_index=True,
                     column_config={"Link": st.column_config.LinkColumn("Link")})
else:
    st.info("Click **Search** to fetch the latest news.")
