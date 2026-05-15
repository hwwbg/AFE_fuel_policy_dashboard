# Fuel Policy Overview — Africa Eastern and Southern Region

## Overview

This repository contains the **Fuel Policy Overview — Africa Eastern and Southern Region** dashboard. The dashboard is designed to support monitoring and review of fuel pricing regimes, subsidy status, retail pump prices, and fuel-price-related news across countries in the World Bank’s Africa Eastern and Southern Region.

The dashboard brings together several types of information in one lightweight web interface:

- international commodity price trends;
- country-level fuel pricing regimes and subsidy-status labels;
- domestic pump price series for gasoline, diesel, kerosene, and LPG where available;
- cross-country retail fuel price comparisons;
- fuel-price-related news and policy updates.

The dashboard is intended as an analytical and monitoring tool. It is not an official policy classification database. Users should refer to the original source links and refresh reports when using the data for formal analysis or country engagement.

## Repository Structure

```text
AFE_fuel_policy_dashboard/
│
├── dashboard/
│   ├── index.html
│   └── data/
│       ├── fuel_news.json
│       ├── fuel_news_last_updated.json
│       ├── fuel_prices.json
│       ├── fuel_prices_last_updated.json
│       └── fuel_prices_refresh_report.json
│
├── scripts/
│   ├── update_fuel_news.py
│   └── update_fuel_prices.py
│
├── .github/
│   └── workflows/
│       ├── update_fuel_news.yml
│       └── update_fuel_prices.yml
│
└── README.md
```

Some files may be generated automatically by GitHub Actions and may not exist until the workflow has run at least once.

## Dashboard Tabs

### 1. Commodity Prices

This tab shows international fuel and commodity price trends. It provides a broad market context for domestic fuel-price movements and policy responses.

### 2. Country Subsidies

This tab summarizes country-level fuel pricing regimes and subsidy-status labels. The dashboard separates two concepts:

- **Pricing regime**: how domestic fuel prices are set, such as market-based pricing or government-administered/controlled pricing.
- **Subsidy status**: whether there is documented evidence of official subsidy, price freeze, stabilization, tax relief, or no official subsidy.

This distinction is important because a country can have regulated prices without an explicit fiscal subsidy, or tax relief without a direct fuel subsidy.

### 3. Country Fuel Prices

This tab presents domestic retail fuel price series by country and product. Where possible, prices are sourced from official regulators or government agencies.

The dashboard currently supports fuel products such as:

- gasoline / petrol;
- diesel / gas oil;
- kerosene;
- LPG.

Retail price data are loaded from:

```text
dashboard/data/fuel_prices.json
```

If that file is unavailable, the dashboard falls back to the embedded dataset in `dashboard/index.html`.

### 4. Cross-Country Comparison

This tab compares domestic fuel prices across countries and products. It uses the same underlying country fuel price dataset as Tab 3.

### 5. Fuel Price News

This tab lists recent fuel-price-related news and policy updates. It is updated through an automated GitHub Actions workflow and reads from:

```text
dashboard/data/fuel_news.json
```

The news workflow is intended to help users identify recent fuel price changes, subsidy announcements, tax changes, and related policy developments. It should be treated as a monitoring aid, not a replacement for official regulatory notices.

## Data Sources

The dashboard prioritizes **official country sources** wherever possible, including regulators, ministries, and official fuel-price notices.

Examples of official or preferred sources include:

| Country | Preferred source |
|---|---|
| Angola | GlobalPetrolPrices, used only as a supplemental source for Angola under the current dashboard rule |
| Kenya | Energy and Petroleum Regulatory Authority (EPRA) |
| Madagascar | Office Malgache des Hydrocarbures (OMH) |
| Malawi | Malawi Energy Regulatory Authority (MERA) |
| Mozambique | Autoridade Reguladora de Energia (ARENE) |
| Rwanda | Rwanda Utilities Regulatory Authority (RURA) |
| South Africa | Department of Mineral and Petroleum Resources / DMRE |
| Tanzania | Energy and Water Utilities Regulatory Authority (EWURA) |
| Zambia | Energy Regulation Board (ERB) |
| Zimbabwe | Zimbabwe Energy Regulatory Authority (ZERA) |

### Source Policy

The dashboard follows these source-use rules:

1. **Official regulator or government sources are preferred.**
2. **GlobalPetrolPrices is not used as a general source across countries.**
3. **For Madagascar, OMH is the official source. GlobalPetrolPrices should not be used for Madagascar unless the values can be independently matched to OMH.**
4. **For Angola, GlobalPetrolPrices may be used under the current dashboard rule.**
5. If an official source cannot be parsed automatically, the existing data are preserved and the issue is recorded in the refresh report.

## Automated Updates

The dashboard uses GitHub Actions to update selected data files.

### Fuel Price News Update

The fuel news workflow updates:

```text
dashboard/data/fuel_news.json
dashboard/data/fuel_news_last_updated.json
```

The workflow searches for relevant fuel-price-related news and policy updates, deduplicates results, and writes a dashboard-ready JSON file.

### Country Fuel Price Update

The Tab 3/4 fuel price workflow updates:

```text
dashboard/data/fuel_prices.json
dashboard/data/fuel_prices_last_updated.json
dashboard/data/fuel_prices_refresh_report.json
```

The workflow is defined in:

```text
.github/workflows/update_fuel_prices.yml
```

and runs:

```text
scripts/update_fuel_prices.py
```

The workflow runs every 24 hours and can also be triggered manually from the GitHub Actions tab.

The updater checks official sources for countries where an official source has been configured. If a source can be parsed, new observations are added or existing observations are updated. If a source cannot be parsed, the workflow keeps the existing data unchanged and records the issue in:

```text
dashboard/data/fuel_prices_refresh_report.json
```

This conservative behavior avoids silently replacing official-source data with secondary data.

## Running the Fuel Price Update Locally

From the repository root, install dependencies:

```bash
python -m pip install --upgrade pip
pip install requests beautifulsoup4 lxml
```

Then run:

```bash
python scripts/update_fuel_prices.py   --index dashboard/index.html   --output dashboard/data/fuel_prices.json   --last-updated dashboard/data/fuel_prices_last_updated.json   --report dashboard/data/fuel_prices_refresh_report.json   --timeout 30   --sleep 1.0   --min-success 1
```

After running, review:

```text
dashboard/data/fuel_prices_refresh_report.json
```

to confirm which countries were successfully parsed and which require manual review or parser improvement.

## Updating the Dashboard

The dashboard is a static HTML application. The main file is:

```text
dashboard/index.html
```

When editing the dashboard, be careful not to break the following dynamic data-loading behavior:

```javascript
fetch('data/fuel_prices.json', { cache: 'no-store' })
```

Because `index.html` is served from the `dashboard/` folder, the correct relative path is:

```text
data/fuel_prices.json
```

not:

```text
dashboard/data/fuel_prices.json
```

## Deployment

The dashboard is designed to be deployed through GitHub Pages or another static web hosting service.

If deployed through GitHub Pages, confirm that the Pages source points to the correct branch and folder. The dashboard entry point is:

```text
dashboard/index.html
```

If the site is served from the `dashboard/` folder, all data files should be accessed using relative paths under:

```text
data/
```

## Quality Control Notes

Before using the dashboard outputs for formal analysis, check:

1. whether the country’s data source is official or secondary;
2. the latest observation date in Tab 3;
3. the fuel price refresh report;
4. whether the country has a complete time series or only sparse observations;
5. whether the price is in local currency, USD per liter, or both;
6. whether the source reports national prices or city-/region-specific prices.

The dashboard is most useful as a monitoring and review tool. For formal country analysis, verify the relevant observation against the original source.

## Known Limitations

Some official sources publish prices in formats that are difficult to scrape automatically, such as:

- dynamic web pages;
- PDF notices;
- scanned notices;
- pages with changing layouts;
- fragmented announcement pages rather than structured historical tables.

For these sources, the automated workflow may not capture new data until a country-specific parser is added or improved. The refresh report is designed to make these failures visible.

## Maintenance Checklist

When adding or reviewing a country source:

1. Confirm that the source is official or clearly documented.
2. Check whether the source provides a historical table, a current-price page, or PDF notices.
3. Add or update the source configuration in `scripts/update_fuel_prices.py`.
4. Run the script locally.
5. Check `fuel_prices_refresh_report.json`.
6. Confirm that Tab 3 and Tab 4 render correctly.
7. Commit the updated script and generated data if appropriate.

## Suggested Citation

When referring to this dashboard in internal notes or presentations, use:

> Fuel Policy Overview — Africa Eastern and Southern Region, internal dashboard compiled from official fuel price regulators, policy databases, and verified fuel-price news sources.

## Disclaimer

This dashboard is a working analytical product. It compiles information from multiple sources and may contain gaps or lags depending on source availability and automated parsing performance. Users should verify country-specific values against the original source before using them for official reporting or policy analysis.
