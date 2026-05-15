#!/usr/bin/env python3
"""
Best-fix updater for Madagascar (MDG) fuel prices in the AFE fuel dashboard.

Rules:
- Madagascar must use OMH only: https://www.omh.mg/index.php?page=prixpompe
- No GlobalPetrolPrices fallback is used for Madagascar.
- Existing Madagascar rows are removed before refresh so incorrect stale MDG data are not shown.
- Output format matches dashboard FUEL_DATA: country -> list of observation rows.
- With --require-mdg, the workflow fails if OMH cannot be parsed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

MDG_URL = "https://www.omh.mg/index.php?page=prixpompe"
MDG_SOURCE = "Office Malgache des Hydrocarbures (OMH)"


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


def clean_number(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    s = str(x).strip().replace("\u00a0", " ")
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None
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


def parse_date_any(x: Any) -> Optional[str]:
    if x is None:
        return None
    if isinstance(x, (int, float)) and 30000 < float(x) < 60000:
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(x))).isoformat()
    s = str(x).strip()
    months = {
        "janvier": "01", "février": "02", "fevrier": "02", "mars": "03", "avril": "04",
        "mai": "05", "juin": "06", "juillet": "07", "août": "08", "aout": "08",
        "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12", "decembre": "12",
    }
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        d, mo, y = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m = re.search(r"(\d{1,2})\s+([A-Za-zéèêûùôîïçà]+)\s+(\d{4})", s, flags=re.I)
    if m:
        d, mon, y = m.groups()
        mon = mon.lower()
        if mon in months:
            return f"{int(y):04d}-{months[mon]}-{int(d):02d}"
    return None


def make_row(date: str, family: str, label: str, price: float) -> Dict[str, Any]:
    return {
        "series_key": f"{family}|||{label.lower().replace(' ', '_').replace('/', '_')}",
        "observation_date": date,
        "source_key": "omh.mg",
        "unit": "L",
        "fuel_family": family,
        "fuel_product": label,
        "price_local": price,
        "currency": "MGA",
        "location": "National",
        "source_name": MDG_SOURCE,
        "source_url": MDG_URL,
    }


def row_key(r: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (str(r.get("observation_date", "")), str(r.get("fuel_family", "")), str(r.get("fuel_product", "")), str(r.get("location", "National")))


def dedupe(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for r in rows:
        if r.get("observation_date") and r.get("fuel_family") and r.get("price_local") is not None:
            out[row_key(r)] = r
    return sorted(out.values(), key=lambda x: (x["observation_date"], x["fuel_family"], x["fuel_product"]))


def request_html(url: str, timeout: int) -> str:
    r = requests.get(url, headers={
        "User-Agent": "Mozilla/5.0 fuel-policy-dashboard-bot/2.0 (+https://github.com/hwwbg/AFE_fuel_policy_dashboard)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_tables(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    aliases = {
        "sc": ("gasoline", "Essence / SC"), "supercarburant": ("gasoline", "Essence / SC"),
        "essence": ("gasoline", "Essence / SC"), "pl": ("kerosene", "Kerosene / PL"),
        "pétrole lampant": ("kerosene", "Kerosene / PL"), "petrole lampant": ("kerosene", "Kerosene / PL"),
        "kerosene": ("kerosene", "Kerosene / PL"), "go": ("diesel", "Diesel / GO"),
        "gasoil": ("diesel", "Diesel / GO"), "diesel": ("diesel", "Diesel / GO"),
    }
    result: List[Dict[str, Any]] = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if len(rows) < 2:
            continue
        header_idx = date_idx = None
        product_idx: Dict[int, Tuple[str, str]] = {}
        for ridx, row in enumerate(rows[:10]):
            tmp_date = None
            tmp_prod = {}
            for i, cell in enumerate([re.sub(r"\s+", " ", c.lower().strip()) for c in row]):
                if cell == "date" or "date" in cell:
                    tmp_date = i
                for key, val in aliases.items():
                    if cell == key or key in cell:
                        tmp_prod[i] = val
            if tmp_date is not None and tmp_prod:
                header_idx, date_idx, product_idx = ridx, tmp_date, tmp_prod
                break
        if header_idx is None or date_idx is None or not product_idx:
            continue
        for row in rows[header_idx + 1:]:
            if len(row) <= date_idx:
                continue
            date = parse_date_any(row[date_idx])
            if not date:
                continue
            for col, (family, label) in product_idx.items():
                if len(row) <= col:
                    continue
                price = clean_number(row[col])
                if price is not None and 100 <= price <= 50000:
                    result.append(make_row(date, family, label, price))
    return dedupe(result)


def parse_text(text: str) -> List[Dict[str, Any]]:
    text = re.sub(r"\s+", " ", text)
    result: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"(\d{1,2}[-/]\d{1,2}[-/]\d{4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})"
        r"\s+([0-9][0-9\s.,]{2,12})"
        r"\s+([0-9][0-9\s.,]{2,12})"
        r"\s+([0-9][0-9\s.,]{2,12})"
    )
    for m in pattern.finditer(text):
        date = parse_date_any(m.group(1))
        vals = [clean_number(m.group(i)) for i in (2, 3, 4)]
        if not date or any(v is None for v in vals):
            continue
        sc, pl, go = vals
        if all(100 <= v <= 50000 for v in vals):
            result += [
                make_row(date, "gasoline", "Essence / SC", sc),
                make_row(date, "kerosene", "Kerosene / PL", pl),
                make_row(date, "diesel", "Diesel / GO", go),
            ]
    return dedupe(result)


def flatten_json(obj: Any) -> List[Any]:
    out: List[Any] = []
    if isinstance(obj, dict):
        out.append(obj)
        for v in obj.values():
            out += flatten_json(v)
    elif isinstance(obj, list):
        for v in obj:
            out += flatten_json(v)
    return out


def parse_json_payload(obj: Any) -> List[Dict[str, Any]]:
    def first(d: Dict[str, Any], names: List[str]) -> Any:
        lower = {str(k).lower().strip(): v for k, v in d.items()}
        for n in names:
            if n in lower:
                return lower[n]
        for k, v in lower.items():
            if any(n in k for n in names):
                return v
        return None
    result: List[Dict[str, Any]] = []
    for item in flatten_json(obj):
        if not isinstance(item, dict):
            continue
        date = parse_date_any(first(item, ["date", "dates", "date_prix", "dateprix", "jour"]))
        if not date:
            continue
        sc = clean_number(first(item, ["sc", "supercarburant", "essence"]))
        pl = clean_number(first(item, ["pl", "petrole_lampant", "pétrole_lampant", "petrole lampant", "kerosene"]))
        go = clean_number(first(item, ["go", "gasoil", "diesel"]))
        if sc is not None and 100 <= sc <= 50000:
            result.append(make_row(date, "gasoline", "Essence / SC", sc))
        if pl is not None and 100 <= pl <= 50000:
            result.append(make_row(date, "kerosene", "Kerosene / PL", pl))
        if go is not None and 100 <= go <= 50000:
            result.append(make_row(date, "diesel", "Diesel / GO", go))
    return dedupe(result)


def render_capture(timeout_ms: int) -> Tuple[str, List[Any], List[str]]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(f"Playwright is not available: {e}")
    json_payloads: List[Any] = []
    text_payloads: List[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        def handle_response(resp):
            try:
                url = resp.url.lower()
                ctype = (resp.headers.get("content-type") or "").lower()
                likely = any(x in url for x in ["prix", "pompe", "carbur", "fuel", "data", "ajax", "api"])
                if not likely and "json" not in ctype:
                    return
                if "json" in ctype:
                    try:
                        json_payloads.append(resp.json())
                        return
                    except Exception:
                        pass
                try:
                    body = resp.text()
                except Exception:
                    return
                low = body.lower()
                if any(x in low for x in ["sc", "supercarburant", "gasoil", "date", "prix"]):
                    text_payloads.append(body)
                    try:
                        json_payloads.append(json.loads(body))
                    except Exception:
                        pass
            except Exception:
                return
        page.on("response", handle_response)
        page.goto(MDG_URL, wait_until="networkidle", timeout=timeout_ms)
        page.wait_for_timeout(4000)
        html = page.content()
        browser.close()
    return html, json_payloads, text_payloads


def fetch_mdg(timeout: int) -> Tuple[List[Dict[str, Any]], str]:
    html = request_html(MDG_URL, timeout)
    rows = parse_tables(html)
    if rows:
        return rows, "Parsed OMH static HTML table."
    rows = parse_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    if rows:
        return rows, "Parsed OMH static text rows."
    rendered, json_payloads, text_payloads = render_capture(max(timeout * 1000, 60000))
    rows = parse_tables(rendered)
    if rows:
        return rows, "Parsed OMH rendered DOM table."
    rows = parse_text(BeautifulSoup(rendered, "html.parser").get_text(" ", strip=True))
    if rows:
        return rows, "Parsed OMH rendered text rows."
    all_json_rows: List[Dict[str, Any]] = []
    for payload in json_payloads:
        all_json_rows += parse_json_payload(payload)
    all_json_rows = dedupe(all_json_rows)
    if all_json_rows:
        return all_json_rows, f"Parsed OMH browser-captured JSON payloads ({len(json_payloads)} payloads inspected)."
    all_text_rows: List[Dict[str, Any]] = []
    for body in text_payloads:
        all_text_rows += parse_text(body)
        all_text_rows += parse_tables(body)
    all_text_rows = dedupe(all_text_rows)
    if all_text_rows:
        return all_text_rows, f"Parsed OMH browser-captured text/HTML payloads ({len(text_payloads)} payloads inspected)."
    return [], f"OMH loaded/rendered, but no official Date/SC/PL/GO rows were found ({len(json_payloads)} JSON payloads and {len(text_payloads)} text payloads inspected)."


def load_json(path: Path, default: Any) -> Any:
    if not path.exists() or path.stat().st_size == 0:
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def extract_embedded_data(index: Path) -> Dict[str, Any]:
    if not index.exists():
        return {}
    text = index.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"(?:const|let|var)\s+FUEL_DATA\s*=\s*JSON\.parse\((?P<q>['\"])(?P<body>.*?)(?P=q)\)\s*;", text, flags=re.S)
    if not m:
        return {}
    raw = m.group("body")
    try:
        return json.loads(json.loads(f'"{raw}"'))
    except Exception:
        try:
            return json.loads(raw)
        except Exception:
            return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="dashboard/index.html")
    ap.add_argument("--output", default="dashboard/data/fuel_prices.json")
    ap.add_argument("--last-updated", default="dashboard/data/fuel_prices_last_updated.json")
    ap.add_argument("--report", default="dashboard/data/fuel_prices_refresh_report.json")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--require-mdg", action="store_true")
    args = ap.parse_args()

    output = Path(args.output)
    report_path = Path(args.report)
    last_path = Path(args.last_updated)
    data = load_json(output, None)
    if not isinstance(data, dict):
        data = extract_embedded_data(Path(args.index))
    if not isinstance(data, dict):
        data = {}

    # Critical: never keep old incorrect MDG data.
    data.pop("Madagascar", None)

    status: RefreshStatus
    mdg_ok = False
    try:
        rows, msg = fetch_mdg(args.timeout)
        if rows:
            data["Madagascar"] = rows
            mdg_ok = True
            status = RefreshStatus("Madagascar", "MDG", MDG_SOURCE, MDG_URL, "ok", msg, len(rows), len(rows))
        else:
            status = RefreshStatus("Madagascar", "MDG", MDG_SOURCE, MDG_URL, "error" if args.require_mdg else "warning", msg + " Madagascar removed from output to avoid showing incorrect stale data.")
    except Exception as e:
        status = RefreshStatus("Madagascar", "MDG", MDG_SOURCE, MDG_URL, "error" if args.require_mdg else "warning", f"{type(e).__name__}: {e}. Madagascar removed from output to avoid showing incorrect stale data.")

    output.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    meta = {"updated_utc": utc_now_iso(), "mdg_successful": mdg_ok, "source_policy": {"MDG": "OMH only; no GPP fallback; old MDG rows removed before refresh."}}
    last_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    report = {
        "updated_utc": meta["updated_utc"],
        "status": "ok" if mdg_ok else ("failed_mdg_required" if args.require_mdg else "warning_mdg_not_updated"),
        "mdg_successful": mdg_ok,
        "policy_note": "Madagascar uses OMH only; no GPP fallback. Incorrect stale MDG rows are not retained.",
        "countries": [asdict(status)],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.require_mdg and not mdg_ok:
        print("MDG/OMH update failed. See dashboard/data/fuel_prices_refresh_report.json.", file=sys.stderr)
        return 2
    print(f"MDG update {'succeeded' if mdg_ok else 'did not succeed'}; report written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
