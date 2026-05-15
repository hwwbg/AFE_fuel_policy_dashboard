
#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, json, re, sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import requests
from bs4 import BeautifulSoup
MDG_OMH_URL = "https://www.omh.mg/index.php?page=prixpompe"
MDG_SOURCE_NAME = "Office Malgache des Hydrocarbures (OMH)"
@dataclass
class RefreshStatus:
    country: str; iso3: str; source_name: str; source_url: str; status: str; message: str; observations_found: int=0; observations_added: int=0

def utc_now_iso(): return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
def clean_number(value: Any) -> Optional[float]:
    if value is None: return None
    if isinstance(value,(int,float)) and not isinstance(value,bool): return float(value)
    s=str(value).strip().replace('\u00a0',' ')
    if not s: return None
    s=re.sub(r'[^\d,.\-]','',s)
    if not s: return None
    if ',' in s and '.' in s:
        s=s.replace('.','').replace(',','.') if s.rfind(',')>s.rfind('.') else s.replace(',','')
    elif ',' in s:
        parts=s.split(','); s=s.replace(',','.') if len(parts[-1]) in (1,2) else s.replace(',','')
    elif '.' in s:
        parts=s.split('.')
        if len(parts)>2 or (len(parts[-1])==3 and len(parts[0])<=3): s=s.replace('.','')
    try: return float(s)
    except ValueError: return None

def parse_date_any(value: Any) -> Optional[str]:
    if value is None: return None
    if isinstance(value,(int,float)) and 30000<float(value)<60000:
        return (dt.date(1899,12,30)+dt.timedelta(days=int(value))).isoformat()
    s=str(value).strip()
    if not s: return None
    month_fr={"janvier":"01","février":"02","fevrier":"02","mars":"03","avril":"04","mai":"05","juin":"06","juillet":"07","août":"08","aout":"08","septembre":"09","octobre":"10","novembre":"11","décembre":"12","decembre":"12"}
    m=re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})',s)
    if m:
        y,mo,d=m.groups(); return f'{int(y):04d}-{int(mo):02d}-{int(d):02d}'
    m=re.search(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})',s)
    if m:
        d,mo,y=m.groups(); return f'{int(y):04d}-{int(mo):02d}-{int(d):02d}'
    m=re.search(r'(\d{1,2})\s+([A-Za-zéèêûùôîïçà]+)\s+(\d{4})',s,flags=re.I)
    if m:
        d,mon,y=m.groups(); mon=mon.lower()
        if mon in month_fr: return f'{int(y):04d}-{month_fr[mon]}-{int(d):02d}'
    return None

def make_mdg_row(date,family,label,price_local):
    return {"series_key":f"{family}|||{label.lower().replace(' ','_').replace('/','_')}","observation_date":date,"source_key":"omh.mg","unit":"L","fuel_family":family,"fuel_product":label,"price_local":price_local,"currency":"MGA","location":"National","source_name":MDG_SOURCE_NAME,"source_url":MDG_OMH_URL}
def row_key(row): return (str(row.get('observation_date','')),str(row.get('fuel_family','')),str(row.get('fuel_product','')),str(row.get('location','National')))
def dedupe_rows(rows):
    out={}
    for r in rows:
        if r.get('observation_date') and r.get('fuel_family') and r.get('price_local') is not None: out[row_key(r)]=r
    return sorted(out.values(), key=lambda x:(x['observation_date'],x['fuel_family'],x['fuel_product']))
def add_rows(data,country,rows):
    if country not in data or not isinstance(data[country],list): data[country]=[]
    existing={row_key(r):i for i,r in enumerate(data[country])}; changed=0
    for r in rows:
        k=row_key(r)
        if k in existing:
            i=existing[k]
            if data[country][i] != r: data[country][i]={**data[country][i],**r}; changed+=1
        else:
            data[country].append(r); existing[k]=len(data[country])-1; changed+=1
    data[country].sort(key=lambda x:(x.get('observation_date',''),x.get('fuel_family',''),x.get('fuel_product','')))
    return changed

def request_html(url, timeout):
    r=requests.get(url,headers={"User-Agent":"Mozilla/5.0 fuel-policy-dashboard-bot/2.1 (+https://github.com/hwwbg/AFE_fuel_policy_dashboard)","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},timeout=timeout)
    r.raise_for_status(); return r.text

def parse_mdg_tables_from_html(html):
    soup=BeautifulSoup(html,'html.parser'); out=[]
    aliases={"sc":("gasoline","Essence / SC"),"supercarburant":("gasoline","Essence / SC"),"super carburant":("gasoline","Essence / SC"),"essence":("gasoline","Essence / SC"),"pl":("kerosene","Kerosene / PL"),"pétrole lampant":("kerosene","Kerosene / PL"),"petrole lampant":("kerosene","Kerosene / PL"),"kerosene":("kerosene","Kerosene / PL"),"go":("diesel","Diesel / GO"),"gasoil":("diesel","Diesel / GO"),"gas oil":("diesel","Diesel / GO"),"diesel":("diesel","Diesel / GO")}
    for table in soup.find_all('table'):
        raw=[]
        for tr in table.find_all('tr'):
            cells=[c.get_text(' ',strip=True) for c in tr.find_all(['th','td'])]
            if cells: raw.append(cells)
        if len(raw)<2: continue
        header_idx=date_idx=None; prod={}
        for ridx,row in enumerate(raw[:12]):
            norm=[re.sub(r'\s+',' ',c.lower().strip()) for c in row]; td=None; tp={}
            for i,cell in enumerate(norm):
                if cell=='date' or 'date' in cell: td=i
                for key,val in aliases.items():
                    if cell==key or key in cell: tp[i]=val
            if td is not None and tp: header_idx=ridx; date_idx=td; prod=tp; break
        if header_idx is None or date_idx is None: continue
        for row in raw[header_idx+1:]:
            if len(row)<=date_idx: continue
            date=parse_date_any(row[date_idx])
            if not date: continue
            for col,(family,label) in prod.items():
                if len(row)<=col: continue
                price=clean_number(row[col])
                if price is not None and 100<=price<=50000: out.append(make_mdg_row(date,family,label,price))
    return dedupe_rows(out)

def parse_mdg_text_rows(text):
    text=re.sub(r'\s+',' ',text); rows=[]
    pat=re.compile(r'(\d{1,2}[-/]\d{1,2}[-/]\d{4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+([0-9][0-9\s.,]{2,12})\s+([0-9][0-9\s.,]{2,12})\s+([0-9][0-9\s.,]{2,12})')
    for m in pat.finditer(text):
        date=parse_date_any(m.group(1)); sc=clean_number(m.group(2)); pl=clean_number(m.group(3)); go=clean_number(m.group(4))
        if date and None not in (sc,pl,go) and all(100<=v<=50000 for v in (sc,pl,go)):
            rows += [make_mdg_row(date,'gasoline','Essence / SC',sc),make_mdg_row(date,'kerosene','Kerosene / PL',pl),make_mdg_row(date,'diesel','Diesel / GO',go)]
    return dedupe_rows(rows)

def flatten_json(obj):
    out=[]
    if isinstance(obj,dict):
        out.append(obj)
        for v in obj.values(): out += flatten_json(v)
    elif isinstance(obj,list):
        for v in obj: out += flatten_json(v)
    return out

def parse_mdg_json_payload(payload):
    rows=[]
    def first(d,aliases):
        lower={str(k).lower().strip():v for k,v in d.items()}
        for a in aliases:
            if a in lower: return lower[a]
        for k,v in lower.items():
            if any(a in k for a in aliases): return v
        return None
    for item in flatten_json(payload):
        if not isinstance(item,dict): continue
        date=parse_date_any(first(item,['date','dates','date_prix','dateprix','jour']))
        if not date: continue
        sc=clean_number(first(item,['sc','supercarburant','super carburant','essence']))
        pl=clean_number(first(item,['pl','petrole_lampant','pétrole_lampant','petrole lampant','kerosene']))
        go=clean_number(first(item,['go','gasoil','gas oil','diesel']))
        if sc is not None and 100<=sc<=50000: rows.append(make_mdg_row(date,'gasoline','Essence / SC',sc))
        if pl is not None and 100<=pl<=50000: rows.append(make_mdg_row(date,'kerosene','Kerosene / PL',pl))
        if go is not None and 100<=go<=50000: rows.append(make_mdg_row(date,'diesel','Diesel / GO',go))
    return dedupe_rows(rows)

def render_and_capture_omh(timeout_ms, debug_dir=None):
    from playwright.sync_api import sync_playwright
    json_payloads=[]; text_payloads=[]; urls=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True); page=browser.new_page()
        def handle_response(resp):
            try:
                url=resp.url; urls.append(url); low=url.lower(); ctype=(resp.headers.get('content-type') or '').lower()
                likely=any(x in low for x in ['prix','pompe','carbur','fuel','data','ajax','api','json'])
                if not likely and 'json' not in ctype: return
                if 'json' in ctype:
                    try: json_payloads.append(resp.json()); return
                    except Exception: pass
                try: body=resp.text()
                except Exception: return
                if body and any(x in body.lower() for x in ['sc','supercarburant','gasoil','prix','date']):
                    text_payloads.append(body)
                    try: json_payloads.append(json.loads(body))
                    except Exception: pass
            except Exception: return
        page.on('response',handle_response)
        page.goto(MDG_OMH_URL, wait_until='networkidle', timeout=timeout_ms)
        page.wait_for_timeout(5000)
        html=page.content(); browser.close()
    if debug_dir:
        debug_dir.mkdir(parents=True,exist_ok=True)
        (debug_dir/'omh_rendered.html').write_text(html,encoding='utf-8')
        (debug_dir/'omh_network_urls.txt').write_text('\n'.join(urls),encoding='utf-8')
        (debug_dir/'omh_text_payloads.txt').write_text('\n\n---PAYLOAD---\n\n'.join(text_payloads[:20]),encoding='utf-8')
    return html,json_payloads,text_payloads,urls

def fetch_mdg_omh(timeout, debug_dir=None):
    html=request_html(MDG_OMH_URL,timeout)
    rows=parse_mdg_tables_from_html(html)
    if rows: return rows,'Parsed OMH static HTML table.'
    rows=parse_mdg_text_rows(BeautifulSoup(html,'html.parser').get_text(' ',strip=True))
    if rows: return rows,'Parsed OMH static text rows.'
    rendered,json_payloads,text_payloads,urls=render_and_capture_omh(max(timeout*1000,60000),debug_dir)
    rows=parse_mdg_tables_from_html(rendered)
    if rows: return rows,'Parsed OMH rendered DOM table.'
    rows=parse_mdg_text_rows(BeautifulSoup(rendered,'html.parser').get_text(' ',strip=True))
    if rows: return rows,'Parsed OMH rendered text rows.'
    rows=[]
    for payload in json_payloads: rows += parse_mdg_json_payload(payload)
    rows=dedupe_rows(rows)
    if rows: return rows,f'Parsed OMH browser-captured JSON payloads ({len(json_payloads)} payloads inspected).'
    rows=[]
    for body in text_payloads:
        rows += parse_mdg_text_rows(body); rows += parse_mdg_tables_from_html(body)
    rows=dedupe_rows(rows)
    if rows: return rows,f'Parsed OMH browser-captured text/HTML payloads ({len(text_payloads)} payloads inspected).'
    return [], f'OMH was loaded/rendered, but no official Date/SC/PL/GO rows were found. Inspected {len(json_payloads)} JSON payload(s), {len(text_payloads)} text payload(s), and {len(urls)} network URL(s).'

def load_json(path,default):
    if not path.exists() or path.stat().st_size==0: return default
    try: return json.loads(path.read_text(encoding='utf-8'))
    except Exception: return default

def extract_embedded_fuel_data(index_path):
    if not index_path.exists(): return {}
    text=index_path.read_text(encoding='utf-8',errors='ignore')
    m=re.search(r"(?:const|let|var)\s+FUEL_DATA\s*=\s*JSON\.parse\((?P<q>['\"])(?P<body>.*?)(?P=q)\)\s*;",text,flags=re.S)
    if not m: return {}
    raw=m.group('body')
    try: return json.loads(json.loads(f'"{raw}"'))
    except Exception:
        try: return json.loads(raw)
        except Exception: return {}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--index',default='dashboard/index.html'); ap.add_argument('--output',default='dashboard/data/fuel_prices.json'); ap.add_argument('--last-updated',default='dashboard/data/fuel_prices_last_updated.json'); ap.add_argument('--report',default='dashboard/data/fuel_prices_refresh_report.json'); ap.add_argument('--timeout',type=int,default=60); ap.add_argument('--require-mdg',action='store_true'); ap.add_argument('--debug-dir',default='dashboard/data/omh_debug')
    args=ap.parse_args(); index_path=Path(args.index); output_path=Path(args.output); last_updated_path=Path(args.last_updated); report_path=Path(args.report); debug_dir=Path(args.debug_dir) if args.debug_dir else None
    data=load_json(output_path,None)
    if not isinstance(data,dict): data=extract_embedded_fuel_data(index_path)
    if not isinstance(data,dict): data={}
    data.pop('Madagascar',None)
    statuses=[]; mdg_ok=False
    try:
        mdg_rows,msg=fetch_mdg_omh(args.timeout,debug_dir)
        if mdg_rows:
            changed=add_rows(data,'Madagascar',mdg_rows); mdg_ok=True
            statuses.append(RefreshStatus('Madagascar','MDG',MDG_SOURCE_NAME,MDG_OMH_URL,'ok',msg,len(mdg_rows),changed))
        else: statuses.append(RefreshStatus('Madagascar','MDG',MDG_SOURCE_NAME,MDG_OMH_URL,'error' if args.require_mdg else 'warning',msg+' Madagascar was removed from output to avoid stale incorrect data.',0,0))
    except Exception as exc:
        statuses.append(RefreshStatus('Madagascar','MDG',MDG_SOURCE_NAME,MDG_OMH_URL,'error' if args.require_mdg else 'warning',f'{type(exc).__name__}: {exc}. Madagascar was removed from output to avoid stale incorrect data.',0,0))
    output_path.parent.mkdir(parents=True,exist_ok=True); report_path.parent.mkdir(parents=True,exist_ok=True)
    output_path.write_text(json.dumps(data,indent=2,ensure_ascii=False,sort_keys=True),encoding='utf-8')
    updated=utc_now_iso()
    last_updated_path.write_text(json.dumps({'updated_utc':updated,'source_policy':{'MDG':'OMH only; no GPP fallback. Existing MDG rows are removed before refresh because they are not trusted.'},'mdg_successful':mdg_ok},indent=2,ensure_ascii=False),encoding='utf-8')
    report={'updated_utc':updated,'status':'ok' if mdg_ok else ('failed_mdg_required' if args.require_mdg else 'warning_mdg_not_updated'),'mdg_successful':mdg_ok,'policy_note':'Madagascar uses OMH only; no GPP fallback. Stale/incorrect MDG rows are not retained.','countries':[asdict(s) for s in statuses]}
    report_path.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    if args.require_mdg and not mdg_ok:
        print('MDG/OMH scrape failed. See dashboard/data/fuel_prices_refresh_report.json and dashboard/data/omh_debug/.',file=sys.stderr); return 2
    print(f"MDG/OMH scrape {'succeeded' if mdg_ok else 'did not succeed'}."); return 0
if __name__=='__main__': raise SystemExit(main())
