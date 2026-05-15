#!/usr/bin/env python3
"""
Update official country fuel price data for the AFE Fuel Policy dashboard.

Repo layout expected:
  dashboard/index.html
  dashboard/data/fuel_prices.json
  dashboard/data/fuel_prices_last_updated.json
  dashboard/data/fuel_prices_refresh_report.json

Policy:
  - Madagascar (MDG): OMH only. Do NOT use GlobalPetrolPrices unless independently matched to OMH.
  - Angola (AGO): keep existing GPP allowance if already used by the dashboard.
  - Other countries: use official regulator/government sources where configured.
  - If a parser fails, keep existing data unchanged and write a warning in the refresh report.

This script is intentionally conservative. It will not silently replace official data with secondary sources.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


PRODUCT_MAP = {
    # common normalized names used by the dashboard
    "gasoline": "gasoline",
    "petrol": "gasoline",
    "essence": "gasoline",
    "supercarburant": "gasoline",
    "sc": "gasoline",

    "diesel": "diesel",
    "gasoil": "diesel",
    "gas oil": "diesel",
    "ago": "diesel",
    "go": "diesel",

    "kerosene": "kerosene",
    "petrole lampant": "kerosene",
    "pétrole lampant": "kerosene",
    "pl": "kerosene",

    "lpg": "lpg",
    "gaz": "lpg",
    "gaz butane": "lpg",
}


@dataclass
class RefreshStatus:
    country: str
    iso3: str
    source_name: str
    source_url: str
    status: str
    message: str
    observations_found: int = 0
    observations_added: int = 0


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def clean_number(value: str) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # remove spaces, currency text, and common separators
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None

    # Handle 5 100, 5.100,00, 5100.00, etc.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts[-1]) in (1, 2):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2 or (len(parts[-1]) == 3 and len(parts[0]) <= 3):
            s = s.replace(".", "")

    try:
        return float(s)
    except ValueError:
        return None


def parse_date_any(text: str) -> Optional[str]:
    """Return YYYY-MM-DD if a date can be recognized."""
    if not text:
        return None
    s = text.strip()

    month_fr = {
        "janvier": "01", "février": "02", "fevrier": "02", "mars": "03",
        "avril": "04", "mai": "05", "juin": "06", "juillet": "07",
        "août": "08", "aout": "08", "septembre": "09", "octobre": "10",
        "novembre": "11", "décembre": "12", "decembre": "12",
    }

    # yyyy-mm-dd or dd-mm-yyyy or dd/mm/yyyy
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        d, mo, y = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    # French date, e.g. 04 mai 2026
    m = re.search(r"(\d{1,2})\s+([A-Za-zéèêûùôîïçà]+)\s+(\d{4})", s, flags=re.I)
    if m:
        d, mon, y = m.groups()
        mon_key = mon.lower()
        if mon_key in month_fr:
            return f"{int(y):04d}-{month_fr[mon_key]}-{int(d):02d}"

    return None


def request_html(url: str, timeout: int = 30) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 fuel-policy-dashboard-bot/1.0 (+https://github.com/hwwbg/AFE_fuel_policy_dashboard)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def ensure_country(data: Dict[str, Any], country: str) -> Dict[str, Any]:
    if country not in data or not isinstance(data[country], dict):
        data[country] = {}
    return data[country]


def add_obs(data: Dict[str, Any], country: str, product: str, obs: Dict[str, Any]) -> bool:
    """
    Add/replace an observation.

    Expected dashboard-compatible observation fields:
      date, price_local, price_usd, currency, source, source_url
    Only date + one price field are strictly required.
    """
    c = ensure_country(data, country)
    if product not in c or not isinstance(c[product], list):
        c[product] = []

    existing = c[product]
    key = obs.get("date")
    replaced = False
    for i, old in enumerate(existing):
        if old.get("date") == key:
            if old != obs:
                existing[i] = {**old, **obs}
                return True
            return False

    existing.append(obs)
    existing.sort(key=lambda x: x.get("date", ""))
    return True


def count_obs_for_country(data: Dict[str, Any], country: str) -> int:
    c = data.get(country, {})
    if not isinstance(c, dict):
        return 0
    return sum(len(v) for v in c.values() if isinstance(v, list))


# -----------------------------------------------------------------------------
# Generic parsers
# -----------------------------------------------------------------------------

def parse_simple_html_tables(
    html: str,
    country: str,
    source_name: str,
    source_url: str,
    currency: str,
    product_headers: Dict[str, str],
) -> List[Dict[str, Any]]:
    """
    Parse ordinary HTML tables with one date column and product-price columns.

    product_headers maps possible header keywords to normalized product names.
    Example: {"sc": "gasoline", "go": "diesel", "pl": "kerosene"}.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = []

    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if len(rows) < 2:
            continue

        headers = [h.lower().strip() for h in rows[0]]
        date_idx = None
        product_idx = {}

        for i, h in enumerate(headers):
            h2 = re.sub(r"\s+", " ", h)
            if "date" in h2 or "jour" in h2:
                date_idx = i
            for keyword, product in product_headers.items():
                if keyword.lower() == h2 or keyword.lower() in h2:
                    product_idx[i] = product

        if date_idx is None or not product_idx:
            # fallback for OMH-like table: Date SC PL GO
            if len(headers) >= 4 and headers[0] in ("date", "dates"):
                date_idx = 0
                for i, h in enumerate(headers[1:], start=1):
                    if h in product_headers:
                        product_idx[i] = product_headers[h]

        if date_idx is None or not product_idx:
            continue

        for row in rows[1:]:
            if len(row) <= date_idx:
                continue
            date = parse_date_any(row[date_idx])
            if not date:
                continue

            for i, product in product_idx.items():
                if len(row) <= i:
                    continue
                price = clean_number(row[i])
                if price is None:
                    continue
                out.append({
                    "country": country,
                    "product": product,
                    "date": date,
                    "price_local": price,
                    "currency": currency,
                    "source": source_name,
                    "source_url": source_url,
                })

    # Deduplicate
    unique = {}
    for o in out:
        unique[(o["country"], o["product"], o["date"])] = o
    return list(unique.values())


# -----------------------------------------------------------------------------
# Country-specific official parsers
# -----------------------------------------------------------------------------

def fetch_mdg_omh(timeout: int) -> Tuple[List[Dict[str, Any]], str]:
    """
    Madagascar OMH official pump price page.

    OMH uses Date / SC / PL / GO:
      SC = gasoline/supercarburant
      PL = kerosene/petrole lampant
      GO = diesel/gasoil

    No GPP fallback is used here.
    """
    source_url = "https://www.omh.mg/index.php?page=prixpompe"
    html = request_html(source_url, timeout=timeout)

    obs = parse_simple_html_tables(
        html=html,
        country="Madagascar",
        source_name="Office Malgache des Hydrocarbures (OMH)",
        source_url=source_url,
        currency="MGA",
        product_headers={"sc": "gasoline", "pl": "kerosene", "go": "diesel"},
    )

    if not obs:
        return [], "OMH page loaded but no Date/SC/PL/GO table rows were parsed. The page may render data dynamically."
    return obs, "Parsed official OMH pump price table."


def generic_official_table_fetcher(
    country: str,
    iso3: str,
    source_name: str,
    source_url: str,
    currency: str,
    timeout: int,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Conservative generic parser for official HTML tables.

    It is useful for pages that publish tables directly. For PDF-only or dynamic
    sites, this will return no observations and the script will keep existing data.
    """
    html = request_html(source_url, timeout=timeout)
    obs = parse_simple_html_tables(
        html=html,
        country=country,
        source_name=source_name,
        source_url=source_url,
        currency=currency,
        product_headers={
            "gasoline": "gasoline",
            "petrol": "gasoline",
            "essence": "gasoline",
            "super": "gasoline",
            "diesel": "diesel",
            "gasoil": "diesel",
            "kerosene": "kerosene",
            "paraffin": "kerosene",
            "lpg": "lpg",
        },
    )
    if not obs:
        return [], "Official page loaded but no parseable fuel-price table was found."
    return obs, "Parsed official HTML table."


OFFICIAL_SOURCES = [
    {
        "country": "Madagascar",
        "iso3": "MDG",
        "source_name": "Office Malgache des Hydrocarbures (OMH)",
        "source_url": "https://www.omh.mg/index.php?page=prixpompe",
        "currency": "MGA",
        "parser": "mdg_omh",
    },
    {
        "country": "Malawi",
        "iso3": "MWI",
        "source_name": "Malawi Energy Regulatory Authority (MERA)",
        "source_url": "https://mera.mw/",
        "currency": "MWK",
        "parser": "generic",
    },
    {
        "country": "Mozambique",
        "iso3": "MOZ",
        "source_name": "Autoridade Reguladora de Energia (ARENE)",
        "source_url": "https://arene.org.mz/",
        "currency": "MZN",
        "parser": "generic",
    },
    {
        "country": "Kenya",
        "iso3": "KEN",
        "source_name": "Energy and Petroleum Regulatory Authority (EPRA)",
        "source_url": "https://www.epra.go.ke/services/petroleum/petroleum-prices/",
        "currency": "KES",
        "parser": "generic",
    },
    {
        "country": "South Africa",
        "iso3": "ZAF",
        "source_name": "Department of Mineral and Petroleum Resources / DMRE",
        "source_url": "https://www.energy.gov.za/files/esources/petroleum/petroleum_arch.html",
        "currency": "ZAR",
        "parser": "generic",
    },
    {
        "country": "Tanzania",
        "iso3": "TZA",
        "source_name": "Energy and Water Utilities Regulatory Authority (EWURA)",
        "source_url": "https://www.ewura.go.tz/",
        "currency": "TZS",
        "parser": "generic",
    },
    {
        "country": "Rwanda",
        "iso3": "RWA",
        "source_name": "Rwanda Utilities Regulatory Authority (RURA)",
        "source_url": "https://rura.rw/",
        "currency": "RWF",
        "parser": "generic",
    },
    {
        "country": "Zambia",
        "iso3": "ZMB",
        "source_name": "Energy Regulation Board (ERB)",
        "source_url": "https://www.erb.org.zm/",
        "currency": "ZMW",
        "parser": "generic",
    },
    {
        "country": "Namibia",
        "iso3": "NAM",
        "source_name": "Ministry of Mines and Energy, Namibia",
        "source_url": "https://www.mme.gov.na/",
        "currency": "NAD",
        "parser": "generic",
    },
    {
        "country": "Zimbabwe",
        "iso3": "ZWE",
        "source_name": "Zimbabwe Energy Regulatory Authority (ZERA)",
        "source_url": "https://www.zera.co.zw/",
        "currency": "USD",
        "parser": "generic",
    },
    {
        "country": "Mauritius",
        "iso3": "MUS",
        "source_name": "State Trading Corporation / Petroleum Pricing Committee",
        "source_url": "https://stcmu.com/",
        "currency": "MUR",
        "parser": "generic",
    },
    {
        "country": "Seychelles",
        "iso3": "SYC",
        "source_name": "SEYPEC",
        "source_url": "https://www.seypec.com/",
        "currency": "SCR",
        "parser": "generic",
    },
]


def fetch_official_source(src: Dict[str, str], timeout: int) -> Tuple[List[Dict[str, Any]], str]:
    if src["parser"] == "mdg_omh":
        return fetch_mdg_omh(timeout=timeout)
    return generic_official_table_fetcher(
        country=src["country"],
        iso3=src["iso3"],
        source_name=src["source_name"],
        source_url=src["source_url"],
        currency=src["currency"],
        timeout=timeout,
    )


# -----------------------------------------------------------------------------
# Embedded-dashboard bootstrap
# -----------------------------------------------------------------------------

def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def extract_embedded_fuel_data(index_path: Path) -> Dict[str, Any]:
    """
    Try to extract embedded FUEL_DATA from dashboard/index.html.

    Supports patterns like:
      const FUEL_DATA = {...};
      let FUEL_DATA = {...};
      const FUEL_DATA = JSON.parse("...");
    """
    if not index_path.exists():
        return {}

    text = index_path.read_text(encoding="utf-8", errors="ignore")

    # Pattern 1: const/let FUEL_DATA = JSON.parse("...")
    m = re.search(r"(?:const|let|var)\s+FUEL_DATA\s*=\s*JSON\.parse\((?P<q>['\"])(?P<body>.*?)(?P=q)\)\s*;", text, flags=re.S)
    if m:
        raw = m.group("body")
        try:
            return json.loads(json.loads(f'"{raw}"'))
        except Exception:
            try:
                return json.loads(raw)
            except Exception:
                pass

    # Pattern 2: const/let FUEL_DATA = { ... };
    marker = re.search(r"(?:const|let|var)\s+FUEL_DATA\s*=", text)
    if not marker:
        return {}

    start = text.find("{", marker.end())
    if start < 0:
        return {}

    depth = 0
    in_str = False
    esc = False
    quote = ""
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        else:
            if ch in ("'", '"'):
                in_str = True
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i+1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        return {}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default="dashboard/index.html", help="Path to dashboard/index.html")
    parser.add_argument("--output", default="dashboard/data/fuel_prices.json", help="Output JSON data path")
    parser.add_argument("--last-updated", default="dashboard/data/fuel_prices_last_updated.json", help="Last-updated metadata path")
    parser.add_argument("--report", default="dashboard/data/fuel_prices_refresh_report.json", help="Refresh report path")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--min-success", type=int, default=1, help="Minimum successful country refreshes before committing output")
    args = parser.parse_args()

    index_path = Path(args.index)
    output_path = Path(args.output)
    last_updated_path = Path(args.last_updated)
    report_path = Path(args.report)

    data = load_json(output_path, None)
    if not isinstance(data, dict):
        data = extract_embedded_fuel_data(index_path)

    if not isinstance(data, dict):
        data = {}

    statuses: List[RefreshStatus] = []
    success_count = 0

    for src in OFFICIAL_SOURCES:
        before = count_obs_for_country(data, src["country"])
        try:
            obs, msg = fetch_official_source(src, timeout=args.timeout)
            added = 0
            for o in obs:
                obs_record = {
                    "date": o["date"],
                    "price_local": o.get("price_local"),
                    "currency": o.get("currency"),
                    "source": o.get("source"),
                    "source_url": o.get("source_url"),
                }
                if o.get("price_usd") is not None:
                    obs_record["price_usd"] = o["price_usd"]
                if add_obs(data, src["country"], o["product"], obs_record):
                    added += 1

            if obs:
                success_count += 1
                statuses.append(RefreshStatus(
                    country=src["country"],
                    iso3=src["iso3"],
                    source_name=src["source_name"],
                    source_url=src["source_url"],
                    status="ok",
                    message=msg,
                    observations_found=len(obs),
                    observations_added=added,
                ))
            else:
                statuses.append(RefreshStatus(
                    country=src["country"],
                    iso3=src["iso3"],
                    source_name=src["source_name"],
                    source_url=src["source_url"],
                    status="warning",
                    message=msg + " Existing data kept unchanged.",
                    observations_found=0,
                    observations_added=0,
                ))
        except Exception as exc:
            statuses.append(RefreshStatus(
                country=src["country"],
                iso3=src["iso3"],
                source_name=src["source_name"],
                source_url=src["source_url"],
                status="error",
                message=f"{type(exc).__name__}: {exc}. Existing data kept unchanged.",
                observations_found=0,
                observations_added=0,
            ))

        time.sleep(args.sleep)

    if success_count < args.min_success:
        # Still write report, but avoid overwriting data with no successful refresh.
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({
            "updated_utc": utc_now_iso(),
            "status": "failed_min_success",
            "success_count": success_count,
            "min_success": args.min_success,
            "policy_note": "No secondary/GPP fallback was used for Madagascar. Existing price data were preserved.",
            "countries": [asdict(s) for s in statuses],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Only {success_count} official sources succeeded; required {args.min_success}. Data file not overwritten.", file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    metadata = {
        "updated_utc": utc_now_iso(),
        "source_policy": {
            "MDG": "OMH only; do not use GPP unless independently matched to OMH.",
            "AGO": "Existing GPP allowance retained only for Angola, if present in the dashboard.",
            "Other countries": "Official regulator/government source only; keep existing data if parsing fails.",
        },
        "countries_checked": len(OFFICIAL_SOURCES),
        "countries_successful": success_count,
    }
    last_updated_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    report = {
        "updated_utc": metadata["updated_utc"],
        "status": "ok",
        "success_count": success_count,
        "countries_checked": len(OFFICIAL_SOURCES),
        "policy_note": "Official-source refresh. Madagascar uses OMH only; no GPP fallback.",
        "countries": [asdict(s) for s in statuses],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Fuel prices refreshed. Successful official sources: {success_count}/{len(OFFICIAL_SOURCES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
