# EGY Property Automation

## Files

```
egyprop_automation/
  run.py              ← main script
  launch_chrome.bat   ← opens Chrome with debugging enabled
  results.csv         ← created after each run
```

No image folder structure required — images can live anywhere on your PC.

---

## One-time setup

```bash
pip install playwright
playwright install chromium
```

---

## Every run

1. **Open Chrome** (or keep it already open) and double-click `launch_chrome.bat` to enable remote debugging
2. Log in to the CRM → navigate to your filtered list → set filters + Available checkbox
3. Open a terminal here and run:
   ```
   python run.py
   ```
   
The script will connect to your existing Chrome session, scan the current page, ask for image folders, then process all pages automatically.

---

## What happens at startup

```
Script scans the page → finds unit types (e.g. Apartment, Villa, Loft)

Same images for ALL types? (y/n): n

  Folder path for [Apartment]: C:\Users\Ahmed\Desktop\Resale Tasks\solana west\apartments
  ✓ 6 image(s) found

  Folder path for [Villa]: C:\Users\Ahmed\Desktop\Resale Tasks\solana west\villas
  ✓ 6 image(s) found

  IMAGE MAPPING SUMMARY
  Apartment  →  6 image(s)  from  C:\...\apartments
  Villa      →  6 image(s)  from  C:\...\villas

  Look good? Start? (y/n): y
```

Paths can be **anywhere** on your PC. Paste them in directly (quotes are stripped automatically).

---

## Pagination (Automatic)

The script processes all units on the current page, then **automatically** advances to the next page by:
1. Clicking the Next button
2. Waiting for the page to fully load (URL change → networkidle → freeze-overlay disappears)
3. Rescanning for NEW unit types (in "different per type" mode)
4. Processing the new page

**No manual intervention needed** — the script handles page transitions automatically. It stops when it reaches the last page and saves results to `results.csv`.

If you want to stop early, press `Ctrl+C` in the terminal.

---

## Persistent Mapping (NEW in v2)

The script supports two modes for providing image folders. Your initial choice on Page 1 controls behavior for subsequent pages:

- **Same images for ALL types (y):** You provide one folder set on Page 1 and the script will reuse that same set for every unit on every page. The script will *not* rescan subsequent pages or prompt again — pure automation after Page 1.
- **Different images per type (n):** You provide folders per type on Page 1. On subsequent pages the script *rescans* for unit types and will only prompt you for *new* types that were not previously mapped. Known types are reused silently.

Examples:

1. Same images mode (y):
  - Page 1: supply folder A → All units on all pages use folder A (no more prompts).

2. Different images mode (n):
  - Page 1: supply folders for Apartment, Villa
  - Page 2: scan finds Apartment (saved), Villa (saved), Twinhouse (new) → script asks only for Twinhouse path
  - Page 3+: any new types detected will be requested once and then remembered

This behavior reduces unnecessary prompts while allowing incremental discovery of new unit types across pages.


## Automatic Processing

The script runs fully automated once you provide image folder paths on the first page:

1. **First page**: Scans for unit types → asks for image folders → shows summary → confirms you're ready
2. **Subsequent pages**: Automatically detects new pages → rescans for new unit types → asks only for new types → processes all units
3. **Completion**: Stops at last page → saves `results.csv` with all results

**Step-by-step actions per unit (all automatic):**
- Open unit detail page
- Check publish status
- If not published: upload images → tag with 'Live Photo' → open Publish modal → set price display → select images → save
- Return to list and process next unit

---

## Settings (top of run.py)

| Setting | Default | Options |
|---------|---------|---------|
| `PRICE_MODE` | `"auto"` | `"auto"` / `"down_payment"` / `"unit_price"` |
| `DP_THRESHOLD` | `80.0` | % at which auto switches to Unit Price (≥80% = Unit Price) |
| `IMAGE_TAG` | `"Live Photo"` | Any tag name shown in Image Manager |
| `SLOW_TIMEOUT` | `300000` | 5 minutes (ms) — timeout for all waits to unpredictable CRM |

---

## results.csv columns

| Column | Meaning |
|--------|---------|
| page | Which page the unit was on |
| unit | Full unit name |
| url | Direct link to the unit |
| status | `OK` or `FAILED: reason` |
