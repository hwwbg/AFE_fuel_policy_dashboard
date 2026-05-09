#!/usr/bin/env python3
"""
Refresh AFE fuel-price news using the free GDELT DOC 2.0 API.

v3 changes:
  - Retry/backoff for HTTP 429 rate limits.
  - Stricter relevance filtering: the article title/description must contain fuel terms.
  - The target country must appear in the article text/url OR the source domain must be a known source for that country.
  - Existing items are re-filtered before being kept, so earlier noisy GDELT results are cleaned automatically.
  - Writes dashboard/data/fuel_news_rejected.json for QA/debugging.
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
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "dashboard" / "data" / "afe_fuel_news_media_source_registry.json"
DEFAULT_OUTPUT = ROOT / "dashboard" / "data" / "fuel_news.json"
DEFAULT_STATUS = ROOT / "dashboard" / "data" / "fuel_news_last_updated.json"
DEFAULT_REJECTED = ROOT / "dashboard" / "data" / "fuel_news_rejected.json"

PRODUCT_PATTERNS = {
    "gasoline": r"\b(gasoline|petrol|essence|gasolina)\b",
    "diesel": r"\b(diesel|gasoil|gasóleo|gazóleo|gazole|gas oil)\b",
    "kerosene": r"\b(kerosene|paraffin|kérosène|petrole lampant|petróleo iluminante)\b",
    "LPG": r"\b(lpg|cooking gas|gas bottle|gaz domestique|gás de cozinha|gaz de cuisine)\b",
}

CHANGE_PATTERNS = [
    ("price increase", r"\b(hike|hikes|rise|rises|raise|raises|raised|increase|increases|increased|surge|soar|soars|hausse|augment|aument|subida)\b"),
    ("price decrease", r"\b(cut|cuts|drop|drops|decrease|decreases|reduction|reduced|lower|slashed|baisse|réduction|reduz|queda)\b"),
    ("subsidy / tax measure", r"\b(subsidy|subsidies|subsidised|subsidized|tax|levy|VAT|excise|duty|stabilization|stabilisation|subven|imposto)\b"),
    ("shortage / supply disruption", r"\b(shortage|scarcity|queue|queues|ration|supply disruption|stockout|crisis|pénurie|rupture|escassez)\b"),
    ("price adjustment", r"\b(adjustment|adjusted|review|reviewed|pump price|fuel price|tariff|price cap|prix carburant)\b"),
]

FUEL_CONTEXT_RE = re.compile(
    r"\b(fuel|petrol|gasoline|diesel|kerosene|paraffin|lpg|pump price|fuel price|subsidy|combust[ií]vel|combustíveis|carburant|gasóleo|gazóleo|essence|gazole)\b",
    flags=re.IGNORECASE,
)

# Domains that often match GDELT fuel words but tend to be broad financial/political aggregator noise.
LOW_VALUE_DOMAINS = {
    "investegate.co.uk", "finance.yahoo.com", "yahoo.com", "moneycontrol.com", "bostonglobe.com",
    "nakedcapitalism.com", "countercurrents.org", "links.org.au", "oilprice.com", "manilatimes.net",
    "indianexpress.com", "orissapost.com", "infomoney.com.br", "udn.com", "bote.ch",
}

COUNTRY_ALIASES = {
    "Angola": ["angola"],
    "Botswana": ["botswana"],
    "Burundi": ["burundi"],
    "Comoros": ["comoros", "comores"],
    "Democratic Republic of Congo": ["democratic republic of congo", "dr congo", "drc", "rdc", "congo-kinshasa", "kinshasa"],
    "Eritrea": ["eritrea"],
    "Eswatini": ["eswatini", "swaziland"],
    "Ethiopia": ["ethiopia", "ethiopian", "addis ababa"],
    "Kenya": ["kenya", "kenyan"],
    "Lesotho": ["lesotho"],
    "Madagascar": ["madagascar", "malagasy"],
    "Malawi": ["malawi", "malawian"],
    "Mauritius": ["mauritius", "maurice"],
    "Mozambique": ["mozambique", "mozambican", "moçambique"],
    "Namibia": ["namibia", "namibian"],
    "Rwanda": ["rwanda", "rwandan"],
    "Sao Tome and Principe": ["sao tome", "são tomé", "principe", "príncipe"],
    "Seychelles": ["seychelles"],
    "Somalia": ["somalia", "somali"],
    "South Africa": ["south africa", "south african"],
    "South Sudan": ["south sudan"],
    "Sudan": ["sudan", "sudanese"],
    "Tanzania": ["tanzania", "tanzanian"],
    "Uganda": ["uganda", "ugandan"],
    "Zambia": ["zambia", "zambian"],
    "Zimbabwe": ["zimbabwe", "zimbabwean"],
}

ALL_COUNTRY_ALIAS_MAP = {c: set(v) for c, v in COUNTRY_ALIASES.items()}

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


def normalize_domain(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    if value.startswith("http"):
        value = urlparse(value).netloc.lower()
    return value.replace("www.", "")


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


def call_gdelt(query: str, days: int, max_records: int, timeout: int, retries: int, backoff: float) -> dict[str, Any]:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max_records),
        "sort": "DateDesc",
        "timespan": f"{days}d",
    }
    url = f"{GDELT_ENDPOINT}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "afe-fuel-news-dashboard/1.2"})
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"articles": []}
        except HTTPError as exc:
            last_exc = exc
            if exc.code == 429 and attempt < retries:
                time.sleep(backoff * (attempt + 1) * 2)
                continue
            raise
        except (URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    if last_exc:
        raise last_exc
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


def registry_domains(country_record: dict[str, Any]) -> set[str]:
    domains: set[str] = set()
    for src in country_record.get("sources", []):
        d = normalize_domain(src.get("url"))
        if d:
            domains.add(d)
    return domains


def contains_any(text: str, aliases: set[str]) -> bool:
    low = text.lower()
    return any(alias in low for alias in aliases)


def mentioned_other_country(text: str, target_country: str) -> str | None:
    low = text.lower()
    for country, aliases in ALL_COUNTRY_ALIAS_MAP.items():
        if country == target_country:
            continue
        if any(alias in low for alias in aliases):
            return country
    return None


def quality_check(item: dict[str, Any], country_record: dict[str, Any] | None = None) -> tuple[bool, str]:
    country = item.get("country", "")
    title = item.get("headline", "") or ""
    summary = item.get("summary", "") or ""
    article_url = item.get("article_url", "") or ""
    domain = normalize_domain(item.get("source_domain") or item.get("source_url"))
    title_summary = f"{title} {summary}"
    text_with_url = f"{title_summary} {article_url} {domain}"

    if domain in LOW_VALUE_DOMAINS:
        return False, f"low-value broad domain: {domain}"

    # Important: do NOT use the GDELT query as evidence of relevance.
    if not FUEL_CONTEXT_RE.search(title_summary):
        return False, "no fuel-price keyword in title/summary"

    aliases = set(COUNTRY_ALIASES.get(country, [country.lower()]))
    known_domains = registry_domains(country_record) if country_record else set()
    country_in_text = contains_any(text_with_url, aliases)
    domain_is_known = bool(domain and any(domain == d or domain.endswith("." + d) for d in known_domains))

    if not country_in_text and not domain_is_known:
        return False, "target country not found in title/summary/url and source is not a known country source"

    other = mentioned_other_country(title_summary, country)
    if other and not country_in_text:
        return False, f"mentions another country instead of target: {other}"

    return True, "accepted"


def normalize_article(a: dict[str, Any], country_record: dict[str, Any], query: str, retrieved_at: str) -> tuple[Article | None, str]:
    country = country_record.get("country", "").strip()
    iso3 = country_record.get("iso3", "").strip()
    title = (a.get("title") or "").strip()
    url = (a.get("url") or "").strip()
    if not title or not url:
        return None, "missing title or url"

    date = parse_gdelt_date(a.get("seendate") or a.get("date")) or retrieved_at
    domain = normalize_domain(a.get("domain") or urlparse(url).netloc)
    source = source_name_from_domain(domain)
    desc = (a.get("description") or "").strip()
    text_for_classification = f"{title} {desc}"
    products = detect_products(text_for_classification)
    change_type = detect_change_type(text_for_classification)

    temp = {
        "country": country,
        "headline": title,
        "summary": desc or title,
        "article_url": url,
        "source_domain": domain,
        "source_url": f"https://{domain}" if domain else "",
    }
    ok, reason = quality_check(temp, country_record)
    if not ok:
        return None, reason

    return Article(
        id=article_id(country, date, title, url),
        date=date,
        country=country,
        iso3=iso3,
        headline=title,
        summary=desc or title,
        products=products,
        change_type=change_type,
        magnitude="Not extracted automatically",
        source_name=source,
        source_url=f"https://{domain}" if domain else "",
        article_url=url,
        source_domain=domain,
        policy_relevance="Automatically retrieved from GDELT and passed stricter relevance filters. Still review before treating as verified evidence.",
        verified=False,
        retrieved_at=retrieved_at,
        gdelt_query=query,
    ), "accepted"


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

    # Use fewer, more specific terms. Prefer local/source terms first.
    for src in sorted(country_record.get("sources", []), key=lambda x: x.get("priority", 99)):
        raw_terms.extend(src.get("search_terms", [])[:1])
    raw_terms.extend([f"{country} {kw}" for kw in default_keywords[:4]])

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
    parser.add_argument("--rejected", type=Path, default=DEFAULT_REJECTED)
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--max-per-query", type=int, default=6)
    parser.add_argument("--max-per-country", type=int, default=4)
    parser.add_argument("--queries-per-country", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--sleep", type=float, default=1.2)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--backoff", type=float, default=2.0)
    parser.add_argument("--keep-existing", action="store_true")
    args = parser.parse_args()

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    countries = registry.get("countries", [])
    country_map = {c.get("country", ""): c for c in countries}
    default_keywords = registry.get("default_keywords", [])
    retrieved_at = datetime.now(timezone.utc).date().isoformat()

    new_items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []

    print(f"Starting GDELT refresh for {len(countries)} countries", flush=True)
    print(f"Settings: days={args.days}, queries_per_country={args.queries_per_country}, sleep={args.sleep}s, retries={args.retries}", flush=True)

    for idx, c in enumerate(countries, start=1):
        country = c.get("country", "").strip()
        if not country:
            continue

        queries = collect_terms(c, default_keywords, args.queries_per_country)
        country_articles: list[dict[str, Any]] = []

        print(f"[{idx}/{len(countries)}] {country}: {len(queries)} queries", flush=True)
        for q in queries:
            try:
                payload = call_gdelt(q, days=args.days, max_records=args.max_per_query, timeout=args.timeout, retries=args.retries, backoff=args.backoff)
                accepted_count = 0
                rejected_count = 0
                for raw in payload.get("articles", []) or []:
                    art, reason = normalize_article(raw, c, q, retrieved_at)
                    if art:
                        country_articles.append(art.__dict__)
                        accepted_count += 1
                    else:
                        rejected_count += 1
                        rejected.append({"country": country, "query": q, "reason": reason, "title": (raw.get("title") or "")[:200], "url": (raw.get("url") or "")[:300]})
                print(f"    query OK: {accepted_count} kept, {rejected_count} rejected | {q[:90]}", flush=True)
            except (HTTPError, URLError, TimeoutError, Exception) as exc:
                errors.append({"country": country, "query": q, "error": str(exc)[:300]})
                print(f"    query ERROR: {str(exc)[:120]} | {q[:90]}", flush=True)
            time.sleep(args.sleep)

        country_articles = deduplicate(country_articles)
        country_articles = sorted(country_articles, key=lambda x: x.get("date", ""), reverse=True)[: args.max_per_country]
        new_items.extend(country_articles)
        print(f"[{idx}/{len(countries)}] {country}: kept {len(country_articles)} items", flush=True)

    all_items = new_items
    existing_checked = 0
    existing_dropped = 0
    if args.keep_existing:
        filtered_existing: list[dict[str, Any]] = []
        for item in load_existing(args.output):
            existing_checked += 1
            c = country_map.get(item.get("country", ""))
            ok, reason = quality_check(item, c)
            if ok:
                filtered_existing.append(item)
            else:
                existing_dropped += 1
                rejected.append({"country": item.get("country", ""), "query": item.get("gdelt_query", "existing"), "reason": f"existing item dropped: {reason}", "title": (item.get("headline") or "")[:200], "url": (item.get("article_url") or "")[:300]})
        all_items = filtered_existing + new_items

    all_items = deduplicate(all_items)
    all_items = sorted(all_items, key=lambda x: x.get("date", ""), reverse=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(all_items, indent=2, ensure_ascii=False), encoding="utf-8")
    args.rejected.write_text(json.dumps(rejected[:500], indent=2, ensure_ascii=False), encoding="utf-8")

    status = {
        "last_refreshed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "GDELT DOC 2.0 API",
        "registry": str(args.registry),
        "days_searched": args.days,
        "queries_per_country": args.queries_per_country,
        "items_written": len(all_items),
        "new_items_found_this_run": len(new_items),
        "existing_items_checked": existing_checked,
        "existing_items_dropped_by_quality_filter": existing_dropped,
        "query_errors_count": len(errors),
        "errors": errors[:50],
        "rejected_sample_file": str(args.rejected),
        "note": "Items are automatically retrieved from GDELT and marked verified=false. v3 applies stricter relevance filters, but important items should still be reviewed before use as evidence."
    }
    args.status.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(all_items)} items to {args.output}", flush=True)
    print(f"Rejected/QA sample saved to {args.rejected}", flush=True)
    if errors:
        print(f"Completed with {len(errors)} query errors; see {args.status}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
