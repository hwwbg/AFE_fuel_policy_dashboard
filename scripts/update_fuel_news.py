#!/usr/bin/env python3
"""
Refresh AFE fuel-price news using the free GDELT DOC 2.0 API.

Inputs:
  dashboard/data/afe_fuel_news_media_source_registry.json

Outputs:
  dashboard/data/fuel_news.json
  dashboard/data/fuel_news_last_updated.json

Recommended GitHub Actions usage:
  python scripts/update_fuel_news.py --days 14 --max-per-query 8 --max-per-country 6 --queries-per-country 4 --timeout 12 --sleep 0.25 --keep-existing
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
    "diesel": r"\b(diesel|gasoil|gasóleo|gazóleo|gazole|gas oil)\b",
    "kerosene": r"\b(kerosene|paraffin|kérosène|petrole lampant|petróleo iluminante)\b",
    "LPG": r"\b(lpg|cooking gas|gas bottle|gaz domestique|gás de cozinha|gaz de cuisine)\b",
}

CHANGE_PATTERNS = [
    ("price increase", r"\b(hike|hikes|rise|rises|raise|raises|raised|increase|increases|increased|surge|soar|hausse|augment|aument|subida)\b"),
    ("price decrease", r"\b(cut|cuts|drop|drops|decrease|decreases|reduction|reduced|lower|slashed|baisse|réduction|reduz|queda)\b"),
    ("subsidy / tax measure", r"\b(subsidy|subsidies|subsidised|subsidized|tax|levy|VAT|excise|duty|stabilization|stabilisation|subven|imposto)\b"),
    ("shortage / supply disruption", r"\b(shortage|scarcity|queue|queues|ration|supply disruption|stockout|crisis|pénurie|rupture|escassez)\b"),
    ("price adjustment", r"\b(adjustment|adjusted|review|reviewed|pump price|fuel price|tariff|price cap|prix carburant)\b"),
]

FUEL_CONTEXT_RE = re.compile(
    r"\b(fuel|petrol|gasoline|diesel|kerosene|paraffin|LPG|pump price|fuel price|subsidy|combust[ií]vel|carburant|gasóleo|gazóleo|essence|gazole)\b",
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


def build_query(country: str, term: str) -> str:
    term = term.strip()
    if country.lower() not in term.lower():
        term = f"{country} {term}"
    if not FUEL_CONTEXT_RE.search(term):
        term = f"{term} fuel price"
    return term


def call_gdelt(query: str, days: int, max_records: int, timeout: int) -> dict[str, Any]:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max_records),
        "sort": "DateDesc",
        "timespan": f"{days}d",
    }
    url = f"{GDELT_ENDPOINT}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "afe-fuel-news-dashboard/1.1"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"articles": []}


def parse_gdelt_date(value: str | None) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return value[:10]


def source_name_from_domain(domain: str | None) -> str:
    if not domain:
        return "GDELT-indexed source"
    return domain.replace("www.", "")


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


def collect_terms(country_record: dict[str, Any], default_keywords: list[str], queries_per_country: int) -> list[str]:
    country = country_record.get("country", "").strip()
    raw_terms: list[str] = []

    # Give priority to the first terms from each source, then generic country terms.
    for src in country_record.get("sources", []):
        raw_terms.extend(src.get("search_terms", [])[:2])
    raw_terms.extend([f"{country} {kw}" for kw in default_keywords[:6]])

    queries: list[str] = []
    seen = set()
    for term in raw_terms:
        q = build_query(country, term)
        qkey = q.lower()
        if qkey not in seen:
            seen.add(qkey)
            queries.append(q)
        if len(queries) >= queries_per_country:
            break
    return queries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--max-per-query", type=int, default=8)
    parser.add_argument("--max-per-country", type=int, default=6)
    parser.add_argument("--queries-per-country", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--keep-existing", action="store_true")
    args = parser.parse_args()

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    countries = registry.get("countries", [])
    default_keywords = registry.get("default_keywords", [])
    retrieved_at = datetime.now(timezone.utc).date().isoformat()

    new_items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    print(f"Starting GDELT refresh for {len(countries)} countries", flush=True)
    print(f"Settings: days={args.days}, queries_per_country={args.queries_per_country}, timeout={args.timeout}s", flush=True)

    for idx, c in enumerate(countries, start=1):
        country = c.get("country", "").strip()
        iso3 = c.get("iso3", "").strip()
        if not country:
            continue

        queries = collect_terms(c, default_keywords, args.queries_per_country)
        country_articles: list[dict[str, Any]] = []

        print(f"[{idx}/{len(countries)}] {country}: {len(queries)} queries", flush=True)
        for q in queries:
            try:
                payload = call_gdelt(q, days=args.days, max_records=args.max_per_query, timeout=args.timeout)
                article_count = 0
                for raw in payload.get("articles", []) or []:
                    art = normalize_article(raw, country, iso3, q, retrieved_at)
                    if art:
                        country_articles.append(art.__dict__)
                        article_count += 1
                print(f"    query OK: {article_count} relevant items | {q[:90]}", flush=True)
            except (HTTPError, URLError, TimeoutError, Exception) as exc:
                errors.append({"country": country, "query": q, "error": str(exc)[:300]})
                print(f"    query ERROR: {str(exc)[:120]} | {q[:90]}", flush=True)
            time.sleep(args.sleep)

        country_articles = deduplicate(country_articles)
        country_articles = sorted(country_articles, key=lambda x: x.get("date", ""), reverse=True)[: args.max_per_country]
        new_items.extend(country_articles)
        print(f"[{idx}/{len(countries)}] {country}: kept {len(country_articles)} items", flush=True)

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
        "queries_per_country": args.queries_per_country,
        "items_written": len(all_items),
        "new_items_found_this_run": len(new_items),
        "errors": errors[:50],
        "note": "Items are automatically retrieved and marked verified=false. Review important items before using them as evidence."
    }
    args.status.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(all_items)} items to {args.output}", flush=True)
    if errors:
        print(f"Completed with {len(errors)} query errors; see {args.status}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
