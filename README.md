# EGY Property Automation — Debeed

## Files

```
run.py             ← automation logic
gui.py             ← Tkinter GUI (entry point — run this)
_exe_setup.py      ← Chrome auto-launch + PyInstaller path helpers (do not edit)
build.bat          ← builds dist\Debeed.exe (do not edit)
Debeed.spec        ← PyInstaller spec (do not edit)
launch_chrome.bat  ← kept for reference, no longer needed
results.csv        ← created after each run
logs\              ← per-run log files (auto-created)
```

No image folder structure required — images can live anywhere on your PC.

---

## One-time setup

```bash
pip install playwright
playwright install chromium
```

---

## Every run (GUI)

```
python gui.py
```

1. Window opens — Chrome launches **automatically** (no `.bat` needed)
2. Log in to the CRM → navigate to your filtered list → set filters + Available checkbox
3. Click **▶ Start** in the GUI

---

## Every run (terminal / headless)

```
python run.py
```

Chrome still launches automatically. Follow on-screen prompts.

---

## What happens at startup

```
Script scans the page → finds projects and unit types

Project: Solana West  —  2 type(s): Apartment, Villa

  For project 'Solana West': Same images for ALL types? (y/n): n

  Folder path for [Solana West -> Apartment]: C:\Users\Ahmed\Desktop\apartments
  ✓ 6 image(s) found

  Folder path for [Solana West -> Villa]: C:\Users\Ahmed\Desktop\villas
  ✓ 6 image(s) found

  IMAGE MAPPING SUMMARY
  Solana West  (per type)
    Apartment  →  6 image(s)  from  C:\...\apartments
    Villa      →  6 image(s)  from  C:\...\villas

  Look good? Start? (y/n): y
```

Paths can be **anywhere** on your PC. Paste them in directly (quotes are stripped automatically).

---

## Pagination (Automatic)

Script processes all units on current page, then **automatically** advances by:
1. Clicking Next button
2. Waiting for page to fully load (URL change → networkidle → freeze-overlay gone)
3. Rescanning for NEW projects/types
4. Processing new page

Stops at last page, saves `results.csv`. To stop early press `Ctrl+C` in terminal (or close GUI window).

---

## Image mapping modes

**Same images for ALL types (y):**
Provide one folder per project on Page 1 → reused for every unit on every page. Zero prompts after.

**Different images per type (n):**
Provide folders per type on Page 1. On later pages only NEW types (not yet mapped) trigger a prompt. Known types reuse silently.

Examples:

| Mode | Page 1 | Page 2 | Page 3 |
|------|--------|--------|--------|
| Same (y) | Supply folder A | No prompt | No prompt |
| Per type (n) | Apartment ✓, Villa ✓ | Twinhouse ❌ → ask once | Any newer types ❌ → ask once |

---

## Per-unit flow (fully automatic)

1. Open unit detail page
2. Check publish status dot
   - **Red** → proceed with upload & publish
   - **Green / faded** → already published, skip
   - **Disabled** → cannot publish (duplicate), skip
3. Open Image Manager → upload images (order randomized) → tag as `Live Photo`
4. Open Publish Unit modal → auto-select price display (Down Payment vs Unit Price) → select images → check Published → Save
5. Go back to list → next unit

---

## Settings (top of run.py)

| Setting | Default | Options |
|---------|---------|---------|
| `PRICE_MODE` | `"auto"` | `"auto"` / `"down_payment"` / `"unit_price"` |
| `DP_THRESHOLD` | `80.0` | % at which auto switches to Unit Price (≥80% or DP=0 → Unit Price) |
| `IMAGE_TAG` | `"Live Photo"` | Any tag name shown in Image Manager |
| `SLOW_TIMEOUT` | `300000` | 5 min (ms) — timeout for all CRM waits |

---

## Building the .exe

Double-click `build.bat`. Wait 3–7 minutes.

Output: `dist\Debeed.exe`

Ship **only** `dist\Debeed.exe` to users. They need Google Chrome installed; they do **not** need Python.

---

## results.csv columns

| Column | Meaning |
|--------|---------|
| page | Which page the unit was on |
| unit | Full unit name |
| url | Direct link to the unit |
| status | `OK` or `FAILED: reason` |
