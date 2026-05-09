# AFE fuel-price news daily refresh using GitHub Actions + GDELT

This folder contains a minimal GitHub-based refresh setup for the fuel-price news tab.

## Files

```text
.github/workflows/update-fuel-news.yml
scripts/update_fuel_news.py
dashboard/data/afe_fuel_news_media_source_registry.json
dashboard/data/fuel_news.json                    # created by the workflow
dashboard/data/fuel_news_last_updated.json       # created by the workflow
```

## How it works

1. GitHub Actions runs every day at 07:30 UTC, or manually through **Actions > Update AFE fuel news > Run workflow**.
2. The Python script reads `dashboard/data/afe_fuel_news_media_source_registry.json`.
3. For each AFE country, it sends recent fuel-price queries to the free GDELT DOC 2.0 API.
4. The script deduplicates article URLs and writes `dashboard/data/fuel_news.json`.
5. The workflow commits the updated JSON back into the repository.
6. The dashboard loads `dashboard/data/fuel_news.json` when opened.

## GitHub setup

1. Create a GitHub repository.
2. Put your dashboard HTML in `dashboard/index.html` or `dashboard/fuel_policy_afe_dashboard.html`.
3. Copy these files/folders into the repo:

```text
.github/workflows/update-fuel-news.yml
scripts/update_fuel_news.py
dashboard/data/afe_fuel_news_media_source_registry.json
```

4. Enable GitHub Actions.
5. Ensure workflow write permission is allowed:
   - Repository **Settings**
   - **Actions**
   - **General**
   - **Workflow permissions**
   - choose **Read and write permissions**

The workflow also sets `permissions: contents: write` in the YAML file.

## Local test

From the repository root:

```bash
python scripts/update_fuel_news.py --days 7 --max-per-query 5 --max-per-country 5
```

Then check:

```text
dashboard/data/fuel_news.json
dashboard/data/fuel_news_last_updated.json
```

## Dashboard loading code

In the dashboard news tab JavaScript, load the JSON like this:

```javascript
let fuelNewsData = [];

async function loadFuelNews() {
  try {
    const response = await fetch("dashboard/data/fuel_news.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    fuelNewsData = await response.json();
    renderNewsTab();
  } catch (err) {
    console.error("Could not load fuel_news.json", err);
    fuelNewsData = [];
    renderNewsTab();
  }
}

document.addEventListener("DOMContentLoaded", loadFuelNews);
```

If the HTML file is inside the `dashboard/` folder, use this path instead:

```javascript
fetch("data/fuel_news.json", { cache: "no-store" })
```

## Important notes

- GDELT is broad and free, but it is not a verified data source. The JSON marks all automatic results as `verified: false`.
- Treat the feed as a discovery layer. Use official regulator/government releases or high-quality news sources for final validation.
- Keep API/search logic outside the browser dashboard. The dashboard should load the already-refreshed JSON file.
