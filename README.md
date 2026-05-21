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

1. **Close all Chrome windows**
2. Double-click `launch_chrome.bat`
3. Log in to the CRM → navigate to your filtered list → set filters + Available checkbox
4. Open a terminal here and run:
   ```
   python run.py
   ```

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

## Pagination

The script only processes the units visible on the current page.
When a page is done it asks:

```
  Page 1 complete — 42 OK | 0 failed
  Browser shows: 1 / 3

  Enter  →  I moved to next page, continue
  done   →  Finish and save results

  >
```

You click next page in Chrome yourself, then press Enter.
Type `done` when all pages are finished.

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


## Confirmation steps (test mode)

| Step | What to verify |
|------|---------------|
| 1 | New images appeared in Image Manager |
| 2 | Correct tag (Live Photo) applied |
| 3 | Image Manager closed cleanly |
| 4 | Correct price checkbox ticked |
| 5 | Your images selected in Images tab *(manual for now)* |
| 6 | Listing looks correct before moving on |

**To go fully automatic:** open `run.py`, search `# ← REMOVE FOR AUTO`, delete those lines.
Keep STEP 5 until the Images tab DOM is mapped.

---

## Settings (top of run.py)

| Setting | Default | Options |
|---------|---------|---------|
| `PRICE_MODE` | `"auto"` | `"auto"` / `"down_payment"` / `"unit_price"` |
| `DP_THRESHOLD` | `5.0` | % below which auto switches to Unit Price |
| `IMAGE_TAG` | `"Live Photo"` | Any tag name shown in Image Manager |
| `UPLOAD_WAIT_MS` | `3500` | Increase if uploads are slow |

---

## results.csv columns

| Column | Meaning |
|--------|---------|
| page | Which page the unit was on |
| unit | Full unit name |
| url | Direct link to the unit |
| status | `OK` or `FAILED: reason` |
