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
2. Keep the CRM as the **one and only tab** open — do not touch the tab during the run
3. Log in to the CRM → navigate to your filtered list → set filters + Available checkbox
4. Open either the **Rent** or **Re-Sale** workspace tab (these are the only two supported)
5. Click **▶ Start** in the GUI
6. If something breaks during the run, press **Refresh Page** to reload from the saved filtered list (see "Version Updated modal recovery" below)

---

## Every run (terminal / headless)

```
python run.py
```

Chrome still launches automatically. Follow on-screen prompts.

---

## Workspace tabs (Rent / Re-Sale)

Before scanning, the script detects which workspace tab is active:

- **Re-Sale** → full flow (price display auto-selected: Down Payment vs Unit Price)
- **Rent** → same flow, **but the price/down-payment logic is skipped** (rent units have no such decision); images are still selected exactly the same way
- **Primary / unknown** → not supported. The script blocks and shows a **Retry** button (GUI) / retry prompt (terminal). Switch to the Rent or Re-Sale tab in Chrome, then click **Retry** to re-check.

Tab detection is route-based (`/rent-unit` vs `/resale-unit`) so it survives label translation.

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

### Image mapping confirmation loop (no crash on 'n')

If you click **No** on "Look good?", the script **does not cancel**. Instead:
1. It asks: "Which project has the problem?"
2. For a **newly scanned project**: asks "Behaviour issue (same-for-all vs per-type) or path issue?"
   - Behaviour → re-run same/different question + re-collect all paths for that project
   - Path → ask for new folder path for each type
3. For a **project scanned on an earlier page** (re-scan on page 2+): skip behaviour question, go straight to path → only ask for types newly found **this page**
4. After fix: show the summary again → ask "All good?" again
5. Loop forever until you confirm **Yes**

**Key:** On page 2+ with new types, you're only re-asked for the **new types**, not the old ones you already provided.

Visibility: Only projects that actually changed on this page (new projects or gained new types) appear in the picker. Old projects with no new types are invisible.

---

## Version Updated modal recovery (automatic, ~1 second)

Frappe randomly shows a "Version Updated" modal mid-run. The script now:
1. **Detects it instantly** (~1ms) via a DOM observer that watches for the modal title or body text
2. **Within ~1 second** (at the next chunked wait), raises a recovery flag
3. **Reloads** the saved filtered list URL in the same tab (not a new tab)
4. **Rescans** for projects/types (published units drop off via `non_published=1`)
5. **Restarts** from card 1 of the reloaded list — **all run data preserved**

**Why it works:** Every Playwright wait (element visibility, network idle, upload completion) is now chunked into ~1s slices that check the modal flag. If the modal appears during a 5-minute wait, the recovery triggers within ~1s instead of blocking the full timeout.

**Manual override:** If you spot something wrong and need to refresh manually, click **Refresh Page** (next to Save Log in the GUI). Same recovery flow, instantly.

---

## Pagination (Automatic)

Script processes all units on current page, then **automatically** advances by:
1. Clicking Next button
2. Waiting for page to fully load (URL change → networkidle → freeze-overlay gone)
3. Rescanning for NEW projects/types
4. (If new types found on a known project → "All good?" confirmation with fix flow)
5. Processing new page

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

### Smart category-based image fallback (page 2+ only)

Unit types are grouped into three categories: **Small** (apartments, studios, duplexes, chalets…), **Big** (villas, townhouses, standalones, buildings…), and **Other** (land, offices, retail, clinics…).

When a new per-type unit appears on page 2+ and you don't provide a folder within 2 minutes:
- The script borrows images from another **already-uploaded type in the SAME category and project**
  - E.g., a new Duplex (Small) can reuse Apartment (Small) images from this project, if Apartment was successfully uploaded earlier
  - E.g., a new Townhouse (Big) can reuse Villa (Big) images from the same project
- **Other/uncategorized types never borrow** — you must always provide their folder

This is a fallback only — if you provide the folder, it's used immediately.

---

## Per-unit flow (fully automatic)

1. Open unit detail page
2. Check publish status dot
   - **Red** → proceed with upload & publish
   - **Green / faded** → already published, skip
   - **Disabled** → cannot publish (duplicate), skip
3. Open Image Manager → upload images (order randomized) → tag as `Live Photo`
4. Open Publish Unit modal → auto-select price display (Down Payment vs Unit Price) **— skipped on Rent**
   - **Check:** if the suitable price display checkbox is already checked → skip to image selection (no redundant clicking)
   - **Otherwise:** set the checkbox to the auto-determined choice (Down Payment % ≥ threshold or DP=0 → Unit Price)
5. Select images → check Published → Save
6. Go back to list → next unit

**All waits during this flow are chunked** — if the Version Updated modal appears during image upload, publish modal load, or price display waits, recovery triggers within ~1s.

---

## Settings (top of run.py)

| Setting | Default | Options |
|---------|---------|---------|
| `PRICE_MODE` | `"auto"` | `"auto"` / `"down_payment"` / `"unit_price"` — "auto" compares Down Payment % vs threshold |
| `DP_THRESHOLD` | `80.0` | % at which auto switches to Unit Price (≥80% or DP=0 → Unit Price) |
| `IMAGE_TAG` | `"Live Photo"` | Any tag name shown in Image Manager |
| `SLOW_TIMEOUT` | `300000` | 5 min (ms) — max timeout for any single CRM wait (chunked into ~1s slices for modal recovery) |

---

## PLEASE NOTE (shown on startup)

The script prints helpful context on launch:

1. **Same images for all types** — explains the y/n choice and what happens when scanning page 2+
2. **CRM image-read errors** — explains the retry loop for faulty images (first upload per type only)
3. **Units grouped by project, then type** — explains the "All good?" confirmation + fix flow
4. **Category-based image borrowing** — explains the Small/Big/Other fallback and the 2-min timer

Read these once to understand the flow. They stay the same across runs.

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
