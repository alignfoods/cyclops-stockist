# Cyclops Stockist Finder

A stockist map for cyclops.nz that shows which Cyclops varieties each store carries, and keeps itself up to date.

How it works:

1. **New World and PAK'nSAVE** are checked automatically every night. A GitHub Action (`.github/workflows/nightly.yml`) runs `job/fetch_stockists.py`, which asks each store's public website which Cyclops products it ranges.
2. **Woolworths, FreshChoice, SuperValue and the independents** come from a Google Sheet that you keep. Woolworths blocks automated checks and its terms forbid scraping, so these stores are managed by hand.
3. The job merges both into `data/stockists.json` and commits it to the repo.
4. The WordPress plugin reads that JSON and draws the map wherever you place `[cyclops_stockists]`.

## What's in this folder

| Path | What it is |
|---|---|
| `job/fetch_stockists.py` | The nightly data job (Python 3, no extra packages). |
| `job/varieties.json` | The Cyclops varieties and their New World/PAK'nSAVE product IDs. Edit labels here; keep keys the same. |
| `.github/workflows/nightly.yml` | Runs the job nightly at about 3:15am NZ time and commits the data. |
| `data/` | Output: `stockists.json` (used by the map), `state.json`, `run_summary.md`. |
| `sheet/seed_sheet.csv` | Starter Google Sheet: 220 Woolworths and independent stores from the current map, with addresses and map pins. Variety columns are blank for you to fill in. |
| `sheet/newworld_name_mapping.csv` | How the old map's New World names line up with New World's own store list. |
| `dist/cyclops-stockist-finder.zip` | The WordPress plugin, ready to upload. |
| `plugin/` | Plugin source. |
| `preview/` | A local preview page and screenshot. |

## Setup (about 30 minutes)

### 1. Create the Google Sheet

1. In Google Sheets, choose **File > Import > Upload** and select `sheet/seed_sheet.csv`. Choose **Replace spreadsheet**.
2. For each store, put **Y** under every variety it stocks. Leave the cell blank if it doesn't. A store with no ticks still shows on the map, labelled "Ask in store for the varieties available".
3. Rules:
   - Don't rename the column headers. Variety headers must match a label in `job/varieties.json`.
   - The `Notes` column is ignored, so use it for anything.
   - To add a store, fill in Store name, Banner, Address, Lat and Lng. To get Lat and Lng, right-click the store in Google Maps; the first item in the menu is the coordinates. Click it to copy.
   - To remove a store, delete its row.
   - Check the rows that have a note. One is Countdown E-Store Grenada North, which is online only, so you'll probably want to delete it.
4. Choose **File > Share > Publish to web**. Pick the sheet tab, choose **Comma-separated values (.csv)**, then click **Publish**. Copy the link; it ends in `output=csv`.

### 2. Create the GitHub repository

1. Sign in at github.com (a free account is fine) and click **New repository**. Name it `cyclops-stockists` and make it **Public**. A public repo lets the website read the JSON with no password. The repo only holds public store data.
2. Upload everything in this folder **except** `dist/`, `preview/` and `plugin/`. Include the `.github` folder; it's hidden on some computers. The quickest way is GitHub Desktop or `git push`. If you use the web uploader, check that `.github/workflows/nightly.yml` arrived.

### 3. Add the sheet link as a secret

Until this secret exists, the job uses `sheet/seed_sheet.csv` from the repo for the Woolworths and independent stores, so you can do this step later. New World and PAK'nSAVE update either way.

1. In the repo, open **Settings > Secrets and variables > Actions > New repository secret**.
2. Set Name to `SHEET_CSV_URL` and Value to the published CSV link from step 1.
3. Open **Settings > Actions > General > Workflow permissions** and choose **Read and write permissions**, so the job can save the data.

### 4. Run it once

1. Open **Actions > Update Cyclops stockists > Run workflow**.
2. The run takes about 13 minutes, because it waits 2–3 seconds between stores to be polite to the supermarket sites.
3. Open the finished run to see a summary: store counts, any new Cyclops products found, and any problems.
4. Your JSON link will be:
   `https://raw.githubusercontent.com/YOUR-GITHUB-NAME/cyclops-stockists/main/data/stockists.json`

### 5. Install the WordPress plugin

1. In WordPress, go to **Plugins > Add New > Upload Plugin**, choose `dist/cyclops-stockist-finder.zip`, then **Install Now** and **Activate**.
2. Go to **Settings > Cyclops Stockists**, paste the JSON link from step 4, and click **Save Changes**. Then click **Refresh data now**. The Status line should say how many stores loaded.

### 6. Add the map to the stockists page

1. Edit the stockists page.
2. Add a **Shortcode** block containing `[cyclops_stockists]`. For a taller map, use `[cyclops_stockists height="700"]`.
3. Preview the page, then Update.

### 7. Retire Hero Maps

1. Once the new map looks right, remove the Hero Maps block or shortcode from the page.
2. Deactivate and delete the Hero Maps plugin.
3. Cancel any Hero Maps subscription.
4. Hero Maps uses a Google Maps API key that is visible in the page source. Once Hero Maps is gone, delete or restrict that key in Google Cloud Console.

## Day to day

- **New World and PAK'nSAVE** update themselves. If a store stops ranging Cyclops, it drops off after three nights in a row with no Cyclops products, so a one-night glitch doesn't remove it.
- **Woolworths and the independents**: edit the sheet. Changes show on the site after the next nightly run, plus up to 60 minutes of website cache. To update sooner, run the workflow by hand, then click **Refresh data now** in WordPress.
- **New products**: when the job finds a Cyclops product it doesn't know, it lists it under "New product IDs" in the run summary, and the product still shows on the map under a temporary name. To give it a proper name, add it to `job/varieties.json` with a label, group and ID, and add a matching column to the sheet.
- **If a source fails** (for example, a supermarket site is down), the job keeps the previous good data for that source and notes it in the run summary.
- **GitHub pauses scheduled workflows** after 60 days with no repo activity. The nightly data commit normally prevents this. If you see a "workflow disabled" email, re-enable it under Actions.

## Other things noticed on the current site

- The current map has two "Hawke's Bay" categories, and "Marlborough" is misspelt "Malborough". Neither matters once the new map replaces it.
- New World Victoria Park is no longer in New World's store list (it has probably closed), so it won't appear.
- The current map has one pin with no title. It is New World Motueka, which now comes through automatically.

## Notes

- The job uses the same public endpoints the New World and PAK'nSAVE websites use for guest shoppers: one request per store per night, spaced 2–3 seconds apart, under a descriptive user agent. If Foodstuffs ever blocks or changes these endpoints, the job keeps the last good data and reports the failure in the run summary.
- The map uses OpenStreetMap tiles, which are fine for a normal-traffic site; the attribution is shown on the map. If traffic grows a lot, change the tile URL in the plugin settings to a commercial provider.
