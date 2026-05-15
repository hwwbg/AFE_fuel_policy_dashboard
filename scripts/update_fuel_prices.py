#!/usr/bin/env python3
"""
update_fuel_prices.py — AFE Fuel Policy Dashboard scraper
Fetches official pump prices for AFE countries from regulatory sources.

Auto-scrapeable:  Madagascar (OMH), Kenya (EPRA), Tanzania (EWURA),
                  Zambia (ERB), South Africa (DMRE), Rwanda (RURA)
Stubbed (TODO):   Mauritius, Zimbabwe, Namibia, Seychelles, Malawi,
                  Mozambique, Botswana, Ethiopia, Eswatini, Lesotho, Angola
Manual (no scrape): Uganda, Congo Dem. Rep., Sao Tome and Principe
"""
from __future__ import annotations
import argparse, datetime as dt, json, re, sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import requests
from bs4 import BeautifulSoup

# ─── Country metadata ─────────────────────────────────────────────────────────

ISO3 = {
    "Madagascar":   "MDG", "Kenya":        "KEN", "Tanzania":     "TZA",
    "Zambia":       "ZMB", "South Africa": "ZAF", "Rwanda":       "RWA",
    "Mauritius":    "MUS", "Zimbabwe":     "ZWE", "Namibia":      "NAM",
    "Seychelles":   "SYC", "Malawi":       "MWI", "Mozambique":   "MOZ",
    "Botswana":     "BWA", "Ethiopia":     "ETH", "Eswatini":     "SWZ",
    "Lesotho":      "LSO", "Angola":       "AGO",
}

SOURCE_INFO = {
    "Madagascar":   ("Office Malgache des Hydrocarbures (OMH)",     "https://www.omh.mg/index.php?page=prixpompe",  "MGA"),
    "Kenya":        ("Energy & Petroleum Regulatory Authority (EPRA)", "https://www.epra.go.ke/pump-prices/",        "KES"),
    "Tanzania":     ("Energy & Water Utilities Regulatory Authority (EWURA)", "https://www.ewura.go.tz/faqs/petroleum-fuel-prices", "TZS"),
    "Zambia":       ("Energy Regulation Board (ERB)",               "https://www.erb.org.zm/fuelprices.php",        "ZMW"),
    "South Africa": ("Dept. of Mineral Resources & Energy (DMRE)",  "https://www.dmre.gov.za/energy-resources/energy-sources/pretoleum/petrol-price-archive", "ZAR"),
    "Rwanda":       ("Rwanda Utilities Regulatory Authority (RURA)", "https://www.rura.rw/index.php?id=89",          "RWF"),
    "Mauritius":    ("State Trading Corporation / PPC",             "https://www.stcmu.com/ppm/retail-prices",       "MUR"),
    "Zimbabwe":     ("Zimbabwe Energy Regulatory Authority (ZERA)", "https://www.zera.co.zw/",                      "USD"),
    "Namibia":      ("Ministry of Mines & Energy (MIME)",           "https://www.mme.gov.na/news/",                 "NAD"),
    "Seychelles":   ("SEYPEC",                                      "https://www.seypec.com/fuel-prices",           "SCR"),
    "Malawi":       ("Malawi Energy Regulatory Authority (MERA)",   "https://mera.mw/",                             "MWK"),
    "Mozambique":   ("ARENE",                                       "https://arene.org.mz/",                        "MZN"),
    "Botswana":     ("Botswana Energy Regulatory Authority (BERA)", "https://www.bera.co.bw/downloads/Petroleum",   "BWP"),
    "Ethiopia":     ("Ministry of Trade & Regional Integration (MOTRI)", "https://www.motri.gov.et/",              "ETB"),
    "Eswatini":     ("Eswatini Energy Regulatory Authority (ESERA)","https://www.esera.org.sz",                    "SZL"),
    "Lesotho":      ("Petroleum Fund of Lesotho",                   "https://petroleum.org.ls",                    "LSL"),
    "Angola":       ("GlobalPetrolPrices (proxy)",                  "https://www.globalpetrolprices.com/",          "AOA"),
}

MANUAL_COUNTRIES = {"Uganda", "Congo, Dem. Rep.", "Sao Tome and Principe"}

# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class RefreshStatus:
    country: str
    iso3: str
    source_name: str
    source_url: str
    status: str       # ok | warning | error | not_implemented
    message: str
    observations_found: int = 0
    observations_added: int = 0

# ─── Shared utilities ─────────────────────────────────────────────────────────

def utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

def clean_number(value: Any) -> Optional[float]:
    if value is None: return None
    if isinstance(value, (int, float)) and not isinstance(value, bool): return float(value)
    s = str(value).strip().replace('\u00a0', ' ')
    if not s: return None
    s = re.sub(r'[^\d,.\-]', '', s)
    if not s: return None
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.') if s.rfind(',') > s.rfind('.') else s.replace(',', '')
    elif ',' in s:
        parts = s.split(',')
        s = s.replace(',', '.') if len(parts[-1]) in (1, 2) else s.replace(',', '')
    elif '.' in s:
        parts = s.split('.')
        if len(parts) > 2 or (len(parts[-1]) == 3 and len(parts[0]) <= 3):
            s = s.replace('.', '')
    try: return float(s)
    except ValueError: return None

MONTH_FR = {
    "janvier": "01", "février": "02", "fevrier": "02", "mars": "03",
    "avril": "04", "mai": "05", "juin": "06", "juillet": "07",
    "août": "08", "aout": "08", "septembre": "09", "octobre": "10",
    "novembre": "11", "décembre": "12", "decembre": "12",
}
MONTH_EN = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

def parse_date_any(value: Any) -> Optional[str]:
    if value is None: return None
    if isinstance(value, (int, float)) and 30000 < float(value) < 60000:
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(value))).isoformat()
    s = str(value).strip()
    if not s: return None
    m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
    if m:
        y, mo, d = m.groups()
        return f'{int(y):04d}-{int(mo):02d}-{int(d):02d}'
    m = re.search(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', s)
    if m:
        d, mo, y = m.groups()
        return f'{int(y):04d}-{int(mo):02d}-{int(d):02d}'
    # "15 April 2025" / "15 avril 2025"
    m = re.search(r'(\d{1,2})\s+([A-Za-zéèêûùôîïçà]+)\s+(\d{4})', s, flags=re.I)
    if m:
        d, mon, y = m.groups()
        mo = MONTH_EN.get(mon.lower()) or MONTH_FR.get(mon.lower())
        if mo: return f'{int(y):04d}-{mo}-{int(d):02d}'
    # "April 2025" → first of month
    m = re.search(r'([A-Za-z]+)\s+(\d{4})', s, flags=re.I)
    if m:
        mon, y = m.groups()
        mo = MONTH_EN.get(mon.lower())
        if mo: return f'{int(y):04d}-{mo}-01'
    return None

def make_row(country: str, date: str, family: str, product: str,
             price_local: float, unit: str = "L") -> dict:
    """Build a canonical price row using SOURCE_INFO for currency/source metadata."""
    source_name, source_url, currency = SOURCE_INFO[country]
    iso3 = ISO3[country]
    slug = product.lower().replace(' ', '_').replace('/', '_').replace('(', '').replace(')', '')
    return {
        "series_key":       f"{family}|||{slug}",
        "observation_date": date,
        "source_key":       iso3.lower(),
        "unit":             unit,
        "fuel_family":      family,
        "fuel_product":     product,
        "price_local":      price_local,
        "currency":         currency,
        "location":         "National",
        "source_name":      source_name,
        "source_url":       source_url,
    }

def row_key(row: dict) -> tuple:
    return (
        str(row.get('observation_date', '')),
        str(row.get('fuel_family', '')),
        str(row.get('fuel_product', '')),
        str(row.get('location', 'National')),
    )

def dedupe_rows(rows: list) -> list:
    out = {}
    for r in rows:
        if r.get('observation_date') and r.get('fuel_family') and r.get('price_local') is not None:
            out[row_key(r)] = r
    return sorted(out.values(), key=lambda x: (x['observation_date'], x['fuel_family'], x['fuel_product']))

def add_rows(data: dict, country: str, rows: list) -> int:
    if not isinstance(data.get(country), list): data[country] = []
    existing = {row_key(r): i for i, r in enumerate(data[country])}
    changed = 0
    for r in rows:
        k = row_key(r)
        if k in existing:
            i = existing[k]
            if data[country][i] != r:
                data[country][i] = {**data[country][i], **r}
                changed += 1
        else:
            data[country].append(r)
            existing[k] = len(data[country]) - 1
            changed += 1
    data[country].sort(key=lambda x: (x.get('observation_date', ''), x.get('fuel_family', ''), x.get('fuel_product', '')))
    return changed

def request_html(url: str, timeout: int) -> str:
    r = requests.get(url, headers={
        "User-Agent": "Mozilla/5.0 fuel-policy-dashboard-bot/2.2 (+https://github.com/hwwbg/AFE_fuel_policy_dashboard)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }, timeout=timeout)
    r.raise_for_status()
    return r.text

def parse_table_generic(html: str, country: str, product_map: dict,
                         price_range: tuple) -> list:
    """
    Generic HTML table parser. product_map maps lowercase header substrings
    to (fuel_family, fuel_product). price_range = (min, max) in local currency/L.
    Returns deduplicated rows.
    """
    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    lo, hi = price_range
    for table in soup.find_all('table'):
        raw = [
            [c.get_text(' ', strip=True) for c in tr.find_all(['th', 'td'])]
            for tr in table.find_all('tr')
        ]
        raw = [r for r in raw if r]
        if len(raw) < 3: continue
        header = [c.lower().strip() for c in raw[0]]
        date_col = next(
            (i for i, h in enumerate(header)
             if any(x in h for x in ['date', 'effective', 'period', 'month'])), None
        )
        if date_col is None: continue
        prod_cols = {}
        for i, h in enumerate(header):
            for key, val in product_map.items():
                if key in h:
                    prod_cols[i] = val
                    break
        if not prod_cols: continue
        for row in raw[1:]:
            date = parse_date_any(row[date_col] if len(row) > date_col else None)
            if not date: continue
            for col, (family, product) in prod_cols.items():
                if len(row) <= col: continue
                price = clean_number(row[col])
                if price is not None and lo <= price <= hi:
                    rows.append(make_row(country, date, family, product, price))
    return dedupe_rows(rows)

def load_json(path: Path, default):
    if not path.exists() or path.stat().st_size == 0: return default
    try: return json.loads(path.read_text(encoding='utf-8'))
    except Exception: return default

def extract_embedded_fuel_data(index_path: Path) -> dict:
    if not index_path.exists(): return {}
    text = index_path.read_text(encoding='utf-8', errors='ignore')
    m = re.search(
        r"(?:const|let|var)\s+FUEL_DATA\s*=\s*JSON\.parse\((?P<q>['\"])(?P<body>.*?)(?P=q)\)\s*;",
        text, flags=re.S
    )
    if not m: return {}
    raw = m.group('body')
    try: return json.loads(json.loads(f'"{raw}"'))
    except Exception:
        try: return json.loads(raw)
        except Exception: return {}

# ─── Madagascar (OMH) ─────────────────────────────────────────────────────────

_MDG_ALIASES = {
    "sc": ("gasoline", "Essence / SC"), "supercarburant": ("gasoline", "Essence / SC"),
    "super carburant": ("gasoline", "Essence / SC"), "essence": ("gasoline", "Essence / SC"),
    "pl": ("kerosene", "Kerosene / PL"), "pétrole lampant": ("kerosene", "Kerosene / PL"),
    "petrole lampant": ("kerosene", "Kerosene / PL"), "kerosene": ("kerosene", "Kerosene / PL"),
    "go": ("diesel", "Diesel / GO"), "gasoil": ("diesel", "Diesel / GO"),
    "gas oil": ("diesel", "Diesel / GO"), "diesel": ("diesel", "Diesel / GO"),
}

def _mdg_parse_tables(html: str) -> list:
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    for table in soup.find_all('table'):
        raw = [[c.get_text(' ', strip=True) for c in tr.find_all(['th', 'td'])] for tr in table.find_all('tr')]
        raw = [r for r in raw if r]
        if len(raw) < 2: continue
        header_idx = date_idx = None
        prod = {}
        for ridx, row in enumerate(raw[:12]):
            norm = [re.sub(r'\s+', ' ', c.lower().strip()) for c in row]
            td = None; tp = {}
            for i, cell in enumerate(norm):
                if 'date' in cell: td = i
                for key, val in _MDG_ALIASES.items():
                    if cell == key or key in cell: tp[i] = val
            if td is not None and tp:
                header_idx = ridx; date_idx = td; prod = tp; break
        if header_idx is None: continue
        for row in raw[header_idx + 1:]:
            date = parse_date_any(row[date_idx] if len(row) > date_idx else None)
            if not date: continue
            for col, (family, label) in prod.items():
                if len(row) <= col: continue
                price = clean_number(row[col])
                if price is not None and 100 <= price <= 50000:
                    out.append(make_row("Madagascar", date, family, label, price))
    return dedupe_rows(out)

def _mdg_parse_text(text: str) -> list:
    text = re.sub(r'\s+', ' ', text)
    rows = []
    pat = re.compile(
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})'
        r'\s+([0-9][0-9\s.,]{2,12})\s+([0-9][0-9\s.,]{2,12})\s+([0-9][0-9\s.,]{2,12})'
    )
    for m in pat.finditer(text):
        date = parse_date_any(m.group(1))
        sc, pl, go = clean_number(m.group(2)), clean_number(m.group(3)), clean_number(m.group(4))
        if date and None not in (sc, pl, go) and all(100 <= v <= 50000 for v in (sc, pl, go)):
            rows += [
                make_row("Madagascar", date, "gasoline", "Essence / SC", sc),
                make_row("Madagascar", date, "kerosene", "Kerosene / PL", pl),
                make_row("Madagascar", date, "diesel",   "Diesel / GO",   go),
            ]
    return dedupe_rows(rows)

def _mdg_playwright(timeout_ms: int, debug_dir: Optional[Path]):
    from playwright.sync_api import sync_playwright
    url = SOURCE_INFO["Madagascar"][1]
    json_payloads = []; text_payloads = []; urls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        def handle(resp):
            try:
                u = resp.url; urls.append(u)
                low = u.lower(); ctype = (resp.headers.get('content-type') or '').lower()
                if not any(x in low for x in ['prix', 'pompe', 'carbur', 'fuel', 'data', 'ajax', 'api', 'json']) \
                        and 'json' not in ctype: return
                if 'json' in ctype:
                    try: json_payloads.append(resp.json()); return
                    except Exception: pass
                body = resp.text()
                if body and any(x in body.lower() for x in ['sc', 'supercarburant', 'gasoil', 'prix', 'date']):
                    text_payloads.append(body)
                    try: json_payloads.append(json.loads(body))
                    except Exception: pass
            except Exception: pass
        page.on('response', handle)
        page.goto(url, wait_until='networkidle', timeout=timeout_ms)
        page.wait_for_timeout(5000)
        html = page.content()
        browser.close()
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / 'mdg_rendered.html').write_text(html, encoding='utf-8')
        (debug_dir / 'mdg_network_urls.txt').write_text('\n'.join(urls), encoding='utf-8')
        (debug_dir / 'mdg_text_payloads.txt').write_text('\n\n---\n\n'.join(text_payloads[:20]), encoding='utf-8')
    return html, json_payloads, text_payloads, urls

def fetch_mdg(timeout: int, debug_dir: Optional[Path] = None, **_):
    url = SOURCE_INFO["Madagascar"][1]
    html = request_html(url, timeout)
    rows = _mdg_parse_tables(html)
    if not rows:
        rows = _mdg_parse_text(BeautifulSoup(html, 'html.parser').get_text(' ', strip=True))
    if not rows:
        rendered, json_payloads, text_payloads, urls = _mdg_playwright(
            max(timeout * 1000, 60000), debug_dir
        )
        rows = _mdg_parse_tables(rendered)
        if not rows:
            rows = _mdg_parse_text(BeautifulSoup(rendered, 'html.parser').get_text(' ', strip=True))
        if not rows:
            return [], (
                f'OMH loaded/rendered but no SC/PL/GO rows found. '
                f'Inspected {len(json_payloads)} JSON, {len(text_payloads)} text, {len(urls)} URLs.'
            )

    for r in rows:
        r["source_key"] = "omh.mg"

    return rows, f'Parsed OMH: {len(rows)} rows.'

# ─── Kenya (EPRA) ─────────────────────────────────────────────────────────────

def fetch_ken(timeout: int, **_):
    url = SOURCE_INFO["Kenya"][1]
    html = request_html(url, timeout)
    rows = parse_table_generic(html, "Kenya", {
        "super petrol": ("gasoline", "Super Petrol"),
        "petrol":       ("gasoline", "Super Petrol"),
        "diesel":       ("diesel",   "Diesel"),
        "kerosene":     ("kerosene", "Kerosene"),
    }, price_range=(50, 500))   # KES/L
    if rows: return rows, f'Parsed EPRA table: {len(rows)} rows.'
    return [], 'EPRA page loaded but no price table found.'

# ─── Tanzania (EWURA) ─────────────────────────────────────────────────────────

def fetch_tza(timeout: int, **_):
    url = SOURCE_INFO["Tanzania"][1]
    html = request_html(url, timeout)
    rows = parse_table_generic(html, "Tanzania", {
        "pms":          ("gasoline", "Petrol (PMS)"),
        "petrol":       ("gasoline", "Petrol (PMS)"),
        "ago":          ("diesel",   "Diesel (AGO)"),
        "diesel":       ("diesel",   "Diesel (AGO)"),
        "kerosene":     ("kerosene", "Kerosene"),
    }, price_range=(500, 10000))  # TZS/L
    if rows: return rows, f'Parsed EWURA table: {len(rows)} rows.'
    return [], 'EWURA page loaded but no price table found.'

# ─── Zambia (ERB) ─────────────────────────────────────────────────────────────

def fetch_zmb(timeout: int, **_):
    url = SOURCE_INFO["Zambia"][1]
    html = request_html(url, timeout)
    rows = parse_table_generic(html, "Zambia", {
        "petrol":       ("gasoline", "Petrol"),
        "pms":          ("gasoline", "Petrol"),
        "diesel":       ("diesel",   "Diesel"),
        "ago":          ("diesel",   "Diesel"),
        "kerosene":     ("kerosene", "Kerosene"),
    }, price_range=(1, 60))     # ZMW/L (currently ~27-30 ZMW)
    if rows: return rows, f'Parsed ERB table: {len(rows)} rows.'
    return [], 'ERB page loaded but no price table found.'

# ─── South Africa (DMRE) ──────────────────────────────────────────────────────

def fetch_zaf(timeout: int, **_):
    url = SOURCE_INFO["South Africa"][1]
    html = request_html(url, timeout)
    rows = parse_table_generic(html, "South Africa", {
        "93":               ("gasoline", "Petrol 93"),
        "95":               ("gasoline", "Petrol 95"),
        "diesel":           ("diesel",   "Diesel"),
        "illuminating":     ("kerosene", "Illuminating Paraffin"),
        "paraffin":         ("kerosene", "Illuminating Paraffin"),
    }, price_range=(5, 50))     # ZAR/L (currently ~22-24 ZAR)
    if rows: return rows, f'Parsed DMRE table: {len(rows)} rows.'
    # DMRE is heavily JS-rendered / PDF-based — common failure
    return [], 'DMRE page loaded but no price table found (likely PDF-only archive; needs Playwright or manual update).'

# ─── Rwanda (RURA) ────────────────────────────────────────────────────────────

def fetch_rwa(timeout: int, **_):
    url = SOURCE_INFO["Rwanda"][1]
    html = request_html(url, timeout)
    rows = parse_table_generic(html, "Rwanda", {
        "petrol":       ("gasoline", "Petrol"),
        "gasoline":     ("gasoline", "Petrol"),
        "diesel":       ("diesel",   "Diesel"),
        "kerosene":     ("kerosene", "Kerosene"),
    }, price_range=(100, 2000))  # RWF/L (currently ~1,200-1,400 RWF)
    if rows: return rows, f'Parsed RURA table: {len(rows)} rows.'
    return [], 'RURA page loaded but no price table found.'

# ─── Stubs — not yet implemented ─────────────────────────────────────────────

def _stub(country: str):
    _, url, _ = SOURCE_INFO[country]
    def fetch(**_):
        return [], f'{country}: scraper not yet implemented. Manual update required. Source: {url}'
    return fetch

fetch_mus = _stub("Mauritius")
fetch_zwe = _stub("Zimbabwe")
fetch_nam = _stub("Namibia")
fetch_syc = _stub("Seychelles")
fetch_mwi = _stub("Malawi")
fetch_moz = _stub("Mozambique")
fetch_bwa = _stub("Botswana")
fetch_eth = _stub("Ethiopia")
fetch_swz = _stub("Eswatini")
fetch_lso = _stub("Lesotho")
fetch_ago = _stub("Angola")

# ─── Dispatcher ───────────────────────────────────────────────────────────────

FETCH_FN: Dict[str, Callable] = {
    "Madagascar":   fetch_mdg,
    "Kenya":        fetch_ken,
    "Tanzania":     fetch_tza,
    "Zambia":       fetch_zmb,
    "South Africa": fetch_zaf,
    "Rwanda":       fetch_rwa,
    "Mauritius":    fetch_mus,
    "Zimbabwe":     fetch_zwe,
    "Namibia":      fetch_nam,
    "Seychelles":   fetch_syc,
    "Malawi":       fetch_mwi,
    "Mozambique":   fetch_moz,
    "Botswana":     fetch_bwa,
    "Ethiopia":     fetch_eth,
    "Eswatini":     fetch_swz,
    "Lesotho":      fetch_lso,
    "Angola":       fetch_ago,
}

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Update AFE fuel price data from official sources.")
    ap.add_argument('--index',        default='dashboard/index.html')
    ap.add_argument('--output',       default='dashboard/data/fuel_prices.json')
    ap.add_argument('--last-updated', default='dashboard/data/fuel_prices_last_updated.json')
    ap.add_argument('--report',       default='dashboard/data/fuel_prices_refresh_report.json')
    ap.add_argument('--timeout',      type=int, default=60)
    ap.add_argument('--debug-dir',    default='dashboard/data/scraper_debug')
    ap.add_argument('--countries',    nargs='+', default=list(FETCH_FN.keys()),
                    help='Countries to update (default: all). Manual countries are always skipped.')
    args = ap.parse_args()

    output_path      = Path(args.output)
    last_updated_path = Path(args.last_updated)
    report_path      = Path(args.report)
    debug_dir        = Path(args.debug_dir) if args.debug_dir else None

    # Load existing data — preserves manual-entry countries (Uganda etc.)
    data = load_json(output_path, None)
    if not isinstance(data, dict):
        data = extract_embedded_fuel_data(Path(args.index))
    if not isinstance(data, dict):
        data = {}

    statuses = []
    to_update = [c for c in args.countries if c in FETCH_FN]

    for country in to_update:
        source_name, source_url, _ = SOURCE_INFO[country]
        iso3 = ISO3[country]

        # Drop existing rows before re-scraping to avoid accumulating stale data
        data.pop(country, None)

        print(f"  Fetching {country} ...", end=' ', flush=True)
        try:
            rows, msg = FETCH_FN[country](timeout=args.timeout, debug_dir=debug_dir)
        except Exception as exc:
            msg = f'{type(exc).__name__}: {exc}'
            rows = []

        if rows:
            changed = add_rows(data, country, rows)
            status = 'ok'
            print(f"{len(rows)} rows (+{changed} new/updated)")
        else:
            changed = 0
            status = 'not_implemented' if 'not yet implemented' in msg else 'warning'
            print(f"0 rows — {msg[:80]}")

        statuses.append(RefreshStatus(
            country=country, iso3=iso3,
            source_name=source_name, source_url=source_url,
            status=status, message=msg,
            observations_found=len(rows), observations_added=changed,
        ))

    # ── Write outputs ──────────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
        encoding='utf-8'
    )

    updated = utc_now_iso()
    last_updated_path.write_text(
        json.dumps({
            'updated_utc': updated,
            'countries': {s.country: s.status for s in statuses},
            'manual_countries': sorted(MANUAL_COUNTRIES),
        }, indent=2, ensure_ascii=False),
        encoding='utf-8'
    )

    hard_errors = [s.country for s in statuses if s.status == 'error']
    report = {
        'updated_utc':       updated,
        'status':            'ok' if not hard_errors else 'partial',
        'hard_failures':     hard_errors,
        'manual_countries':  sorted(MANUAL_COUNTRIES),
        'countries':         {s.country: asdict(s) for s in statuses},
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')

    if hard_errors:
        print(f"\nHard failures (exceptions): {hard_errors}", file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
