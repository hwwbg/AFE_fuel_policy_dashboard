# AFE fuel news daily refresh

This setup uses GitHub Actions to run `scripts/update_fuel_news.py` daily and refresh:

- `dashboard/data/fuel_news.json`
- `dashboard/data/fuel_news_last_updated.json`

The dashboard should load `data/fuel_news.json` because `index.html` is inside the `dashboard/` folder.

## First test

In GitHub, open:

Actions → Update AFE fuel news → Run workflow

The GDELT step can take a few minutes. The updated script prints progress by country and has a 15-minute workflow timeout.

## If the commit step fails

Go to:

Settings → Actions → General → Workflow permissions

Choose:

Read and write permissions

## Local test

From the repo root:

```bash
python scripts/update_fuel_news.py --days 14 --max-per-query 8 --max-per-country 6 --queries-per-country 4 --timeout 12 --sleep 0.25 --keep-existing
```

Then preview the dashboard from `dashboard/`:

```bash
cd dashboard
python -m http.server 8000
```

Open:

http://localhost:8000/index.html
