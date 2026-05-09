#!/usr/bin/env python3
"""
Refresh AFE fuel-price news using the free GDELT DOC 2.0 API.

Inputs:
  dashboard/data/afe_fuel_news_media_source_registry.json

Outputs:
  dashboard/data/fuel_news.json
  dashboard/data/fuel_news_last_updated.json

Usage:
  python scripts/update_fuel_news.py --days 14 --max-per-query 10 --max-per-country 8
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "dashboard" / "data" / "afe_fuel_news_media_source_registry.json"
DEFAULT_OUTPUT = ROOT / "dashboard" / "data" / "fuel_news.json"
DEFAULT_STATUS = ROOT / "dashboard" / "data" / "fuel_news_last_updated.json"

PRODUCT_PATTERNS = {
    "gasoline": r"\b(gasoline|petrol|essence|gasolina)\b",
    "diesel": r"\b(diesel|gasoil|gasóleo|gazole|gas oil)\b",
    "kerosene": r"\b(kerosene|paraffin|kérosène|petrole lampant)\b",
    "LPG": r"\b(lpg|cooking gas|gas bottle|gaz domestique|gás de cozinha)\b",
}

CHANGE_PATTERNS = [
    ("price increase", r"\b(hike|hikes|rise|rises|raise|raises|raised|increase|increases|increased|up|surge|soar|prix.*hausse|augment|aument|subida)\b"),
    ("price decrease", r"\b(cut|cuts|drop|drops|decrease|decreases|reduction|reduced|down|lower|slashed|baisse|réduction|reduz|queda)\b"),
    ("subsidy / tax measure", r"\b(subsidy|subsidies|subsidised|subsidized|tax|levy|VAT|excise|duty|stabilization|stabilisation|subven|imposto)\b"),
    ("shortage / supply disruption", r"\b(shortage|scarcity|queue|queues|ration|supply disruption|stockout|crisis|pénurie|rupture|escassez)\b"),
    ("price adjustment", r"\b(adjustment|adjusted|review|reviewed|pump price|fuel price|tariff|price cap|prix carburant)\b"),
]

FUEL_CONTEXT_RE = re.compile(
    r"\b(fuel|petrol|gasoline|diesel|kerosene|paraffin|LPG|pump price|fuel price|subsidy|combust[ií]vel|carburant|gasóleo|essence|gazole)\b",
    flags=re.IGNORECASE,
)

@dataclass
class Article:
    id: str
    date: str
    country: str
    iso3: str
    headline: str
    summary: str
    products: list[str]
    change_type: str
    magnitude: str
    source_name: str
    source_url: str
    article_url: str
    source_domain: str
    policy_relevance: str
    verified: bool
    retrieved_at: str
    gdelt_query: str


def slugify(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")
    return text[:max_len].strip("-") or "article"


def article_id(country: str, date: str, title: str, url: str) -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{slugify(country, 20)}-{date}-{slugify(title, 45)}-{h}"


def detect_products(text: str) -> list[str]:
    found = []
    for product, pattern in PRODUCT_PATTERNS.items():
        if re.search(pattern, text, flags=re.IGNORECASE):
            found.append(product)
    return found or ["fuel"]


def detect_change_type(text: str) -> str:
    for label, pattern in CHANGE_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return label
    return "fuel-price news"


def build_query(country: str, term: str, source_url: str | None = None) -> str:
    """Build a focused GDELT query.

    GDELT supports Boolean-style full-text queries. Domain restrictions are useful
    but can reduce recall, so this script first searches broad country+term queries.
    """
    term = term.strip()
    if country.lower() not in term.lower():
        term = f'{country} {term}'
    # Add a broad fuel context guard unless the term already clearly contains one.
    if not FUEL_CONTEXT_RE.search(term):
        term = f'{term} fuel price'
    return term


def call_gdelt(query: str, days: int, max_records: int, timeout: int = 30) -> dict[str, Any]:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max_records),
        "sort": "DateDesc",
        "timespan": f"{days}d",
    }
    url = f"{GDELT_ENDPOINT}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "afe-fuel-news-dashboard/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"articles": []}


def parse_gdelt_date(value: str | None) -> str:
    if not value:
        return ""
    # Common GDELT format: 20260507123000 or 20260507T123000Z-like variants.
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return value[:10]


def source_name_from_domain(domain: str | None) -> str:
    if not domain:
        return "GDELT-indexed source"
    domain = domain.replace("www.", "")
    return domain


def normalize_article(a: dict[str, Any], country: str, iso3: str, query: str, retrieved_at: str) -> Article | None:
    title = (a.get("title") or "").strip()
    url = (a.get("url") or "").strip()
    if not title or not url:
        return None

    text = f"{title} {a.get('description') or ''} {query}"
    if not FUEL_CONTEXT_RE.search(text):
        return None

    date = parse_gdelt_date(a.get("seendate") or a.get("date")) or retrieved_at
    domain = (a.get("domain") or "").strip()
    source = source_name_from_domain(domain)
    products = detect_products(text)
    change_type = detect_change_type(text)

    return Article(
        id=article_id(country, date, title, url),
        date=date,
        country=country,
        iso3=iso3,
        headline=title,
        summary=title,
        products=products,
        change_type=change_type,
        magnitude="Not extracted automatically",
        source_name=source,
        source_url=f"https://{domain}" if domain else "",
        article_url=url,
        source_domain=domain,
        policy_relevance="Flagged by automated GDELT search as potentially relevant to fuel prices, subsidies, shortages, or pump-price adjustments. Review before treating as verified evidence.",
        verified=False,
        retrieved_at=retrieved_at,
        gdelt_query=query,
    )


def load_existing(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for item in items:
        url = (item.get("article_url") or item.get("source_url") or "").strip().lower()
        title = re.sub(r"\W+", " ", (item.get("headline") or "").lower()).strip()
        key = url or f"{item.get('country','').lower()}|{item.get('date','')}|{title[:90]}"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--days", type=int, default=14, help="GDELT timespan in days. DOC 2.0 ArticleList is best within recent windows.")
    parser.add_argument("--max-per-query", type=int, default=10)
    parser.add_argument("--max-per-country", type=int, default=8)
    parser.add_argument("--sleep", type=float, default=0.8, help="Delay between GDELT calls.")
    parser.add_argument("--keep-existing", action="store_true", help="Keep existing fuel_news.json items and append new results.")
    args = parser.parse_args()

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    countries = registry.get("countries", [])
    default_keywords = registry.get("default_keywords", [])
    retrieved_at = datetime.now(timezone.utc).date().isoformat()

    new_items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for c in countries:
        country = c.get("country", "").strip()
        iso3 = c.get("iso3", "").strip()
        if not country:
            continue

        terms: list[str] = []
        for src in c.get("sources", []):
            terms.extend(src.get("search_terms", [])[:3])
        # Add a few generic terms per country to increase recall.
        terms.extend([f"{country} {kw}" for kw in default_keywords[:6]])

        # Deduplicate queries while preserving order.
        queries = []
        seen_terms = set()
        for term in terms:
            q = build_query(country, term)
            qkey = q.lower()
            if qkey not in seen_terms:
                seen_terms.add(qkey)
                queries.append(q)

        country_articles: list[dict[str, Any]] = []
        for q in queries[:8]:  # avoid too many API calls per country
            try:
                payload = call_gdelt(q, days=args.days, max_records=args.max_per_query)
                for raw in payload.get("articles", []) or []:
                    art = normalize_article(raw, country, iso3, q, retrieved_at)
                    if art:
                        country_articles.append(art.__dict__)
            except (HTTPError, URLError, TimeoutError, Exception) as exc:
                errors.append({"country": country, "query": q, "error": str(exc)[:300]})
            time.sleep(args.sleep)

        country_articles = deduplicate(country_articles)
        country_articles = sorted(country_articles, key=lambda x: x.get("date", ""), reverse=True)[: args.max_per_country]
        new_items.extend(country_articles)
        print(f"{country}: {len(country_articles)} items")

    all_items = new_items
    if args.keep_existing:
        all_items = load_existing(args.output) + new_items

    all_items = deduplicate(all_items)
    all_items = sorted(all_items, key=lambda x: x.get("date", ""), reverse=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(all_items, indent=2, ensure_ascii=False), encoding="utf-8")

    status = {
        "last_refreshed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "GDELT DOC 2.0 API",
        "registry": str(args.registry),
        "days_searched": args.days,
        "items_written": len(all_items),
        "errors": errors[:50],
        "note": "Items are automatically retrieved and marked verified=false. Review important items before using them as evidence."
    }
    args.status.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(all_items)} items to {args.output}")
    if errors:
        print(f"Completed with {len(errors)} query errors; see {args.status}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
