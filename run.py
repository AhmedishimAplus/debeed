"""
EGY Property — Resale Unit Automation
======================================
Flow:
  1. Connect to your open Chrome session
  2. Script scans current page → finds all unit types
  3. Asks you for image folder paths (same for all, or per type)
  4. Processes every unit on the page
  5. Prompts you to advance to next page manually → repeat until done

REMOVING CONFIRMATIONS (go full-auto later):
  Search "# ← REMOVE FOR AUTO" and delete those lines.
  Keep STEP 5 (Images tab) until that part of the DOM is mapped.

SETUP (one-time):
  pip install playwright
  playwright install chromium
  Launch Chrome via launch_chrome.bat, log in, navigate to filtered list.
"""

import re, csv, random
from pathlib import Path
from playwright.sync_api import sync_playwright, Page

# ═══════════════════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════════════════
BASE_URL       = "https://team.egyproperty-eg.com"
PRICE_MODE     = "auto"     # "auto" | "down_payment" | "unit_price"
DP_THRESHOLD   = 80.0       # auto: if DP > this % of price → use Unit Price (0 or >80% = Unit Price)
IMAGE_TAG      = "Live Photo"
UPLOAD_WAIT_MS = 3500
SLOW_TIMEOUT   = 300_000    # 5 minutes - used for unpredictable slow CRM responses

# ═══════════════════════════════════════════════════════════════════
#  CONFIRMATION  ← delete all confirm() calls to go auto
# ═══════════════════════════════════════════════════════════════════
def confirm(step: str):
    # Auto mode: skip manual confirmations for full automation
    return

# ═══════════════════════════════════════════════════════════════════
#  ERROR HANDLING STATE (tracks upload errors per type/globally)
# ═══════════════════════════════════════════════════════════════════
class UploadErrorState:
    def __init__(self, same_images_mode: bool):
        self.same_images_mode = same_images_mode
        self.global_acknowledged = False  # For same_images_mode
        self.acknowledged_types = {}      # For different images per type: {type: True/False}
        self.first_upload_per_type = {}   # Track if first upload for each type

    def is_first_upload(self, unit_type: str) -> bool:
        """Check if this is the first upload for this type."""
        if self.same_images_mode:
            # For same images mode, only check first time globally
            return len(self.first_upload_per_type) == 0
        else:
            # For different images per type, check first time per type
            return unit_type not in self.first_upload_per_type

    def mark_uploaded(self, unit_type: str):
        """Mark that we've done first upload for this type."""
        if self.same_images_mode:
            self.first_upload_per_type["global"] = True
        else:
            self.first_upload_per_type[unit_type] = True

    def should_ask_user(self, unit_type: str) -> bool:
        """Determine if user should be asked about the error for this type."""
        if self.same_images_mode:
            return not self.global_acknowledged
        else:
            return unit_type not in self.acknowledged_types

    def mark_acknowledged(self, unit_type: str):
        """Mark that user acknowledged error for this type."""
        if self.same_images_mode:
            self.global_acknowledged = True
        else:
            self.acknowledged_types[unit_type] = True

# ═══════════════════════════════════════════════════════════════════
#  UNIT TYPE EXTRACTION
#  "Hyde Park-Hyde park New Cairo-Greens-Apartment" → "Apartment"
#  "ORA-Solana West-C4-Twin House"                 → "Twin House"
# ═══════════════════════════════════════════════════════════════════
def extract_type(unit_name: str) -> str:
    parts = unit_name.split("-")
    return parts[-1].strip() if parts else unit_name.strip()

def extract_project_and_type(unit_name: str) -> tuple:
    """Extract project and type from a dash-delimited unit name.
    Preferred parsing: project = parts[-3], type = parts[-1].
    Falls back to best-effort when parts are short.
    """
    parts = [p.strip() for p in unit_name.split("-") if p.strip()]
    if len(parts) >= 3:
        project = parts[-3].strip()
        utype = parts[-1].strip()
        return project, utype
    # Fallback: try to infer project from middle part if available
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    # Single token fallback
    return (parts[0].strip() if parts else "Unknown Project"), (parts[-1].strip() if parts else "Unknown Type")

def resolve_unit_type(unit_text: str, mapping: dict) -> str:
    extracted = extract_type(unit_text)
    if extracted in mapping:
        return extracted

    def normalize(value: str) -> str:
        return re.sub(r"[\s\-_]+", "", value).lower()

    normalized_text = normalize(unit_text)
    normalized_extracted = normalize(extracted)

    for key in mapping:
        if normalize(key) == normalized_extracted:
            return key

    matches = [key for key in mapping if normalize(key) in normalized_text]
    if matches:
        return max(matches, key=len)

    raise KeyError(
        f"Could not resolve unit type from '{unit_text}'. Available: {list(mapping.keys())}"
    )

def extract_unit_names_from_text(page: Page) -> list:
    """Extract visible unit names from the list page text.
    This is a fallback for pages where unit cards are not anchor tags.
    """
    body_text = page.locator("body").inner_text()
    lines = body_text.split("\n")

    names = []
    skip_tokens = [
        "refresh", "last update", "price", "payment", "no. of", "bua",
        "installments", "press enter", "debug", "available", "sold-out",
        "select all", "total:", "clear duplicates", "published by", "cash"
    ]

    for raw in lines:
        line = raw.strip()
        if not line or len(line) < 10 or line.count("-") < 2:
            continue
        if any(tok in line.lower() for tok in skip_tokens):
            continue
        names.append(line)

    return names

def extract_unit_cards_from_text(page: Page) -> list:
    """Extract ordered card records from list-page text.
    Each record keeps the unit name plus lightweight fingerprint fields
    (last update and price hint) to disambiguate duplicate names.
    """
    body_text = page.locator("body").inner_text()
    lines = [ln.strip() for ln in body_text.split("\n") if ln.strip()]

    skip_tokens = [
        "refresh", "press enter", "debug", "select all", "clear duplicates",
        "published by", "filters", "collapse", "total:"
    ]

    def looks_like_name(line: str) -> bool:
        if len(line) < 10 or line.count("-") < 2:
            return False
        low = line.lower()
        if any(tok in low for tok in skip_tokens):
            return False
        if "last update" in low or "bua" in low or "bedroom" in low:
            return False
        return True

    cards = []
    current = None

    for line in lines:
        if looks_like_name(line):
            if current:
                cards.append(current)
            current = {"name": line, "last_update": "", "price_hint": ""}
            continue

        if not current:
            continue

        if "last update date" in line.lower():
            current["last_update"] = line
            continue

        if not current["price_hint"] and line.upper().startswith("EGP"):
            current["price_hint"] = line

    if current:
        cards.append(current)

    return cards

# ═══════════════════════════════════════════════════════════════════
#  SCAN PAGE → UNIQUE UNIT TYPES
# ═══════════════════════════════════════════════════════════════════
def scan_unit_types(page: Page) -> list:
    page.wait_for_load_state("networkidle", timeout=SLOW_TIMEOUT)
    page.wait_for_selector("div.card.cursor-pointer.rounded-0", timeout=SLOW_TIMEOUT)

    print(f"   DEBUG: Scanning for unit cards...")

    unit_names = extract_unit_names_from_text(page)

    types = []
    seen = set()

    for line in unit_names:
        t = extract_type(line)
        if t and t not in seen and len(t) > 2 and not any(c.isdigit() for c in t[:3]):
            seen.add(t)
            types.append(t)
            print(f"   DEBUG: Found type from line: '{line[:70]}...' → Type: '{t}'")

    print(f"   DEBUG: Extracted types: {types}")
    return types


def scan_projects_types(page: Page) -> dict:
    """Scan the page and return a mapping of project -> list of unit types."""
    page.wait_for_load_state("networkidle", timeout=SLOW_TIMEOUT)
    page.wait_for_selector("div.card.cursor-pointer.rounded-0", timeout=SLOW_TIMEOUT)
    # Read directly from the card grid to avoid multiline noise (dates, BUA, etc.)
    cards = page.locator("div.card.cursor-pointer.rounded-0")
    total = cards.count()
    projects = {}

    def looks_valid_type(t: str) -> bool:
        return t and len(t) > 2 and not any(c.isdigit() for c in t[:3])

    for i in range(total):
        try:
            card_text = cards.nth(i).inner_text().strip()
            title = card_text.split("\n")[0].strip()
            if not title:
                continue
            proj, utype = extract_project_and_type(title)
        except Exception:
            continue
        if not looks_valid_type(utype):
            continue
        pdata = projects.setdefault(proj, [])
        if utype not in pdata:
            pdata.append(utype)

    print(f"   DEBUG: Projects found: {list(projects.keys())}")
    return projects


def collect_image_mapping_per_project(project_types: dict) -> dict:
    """Interactively collect image folders per project. Returns nested mapping.
    Structure:
      { 'Project Name': { '_same_for_all': True/False, '_images': [...], 'Type': [...] } }
    """
    mapping = {}
    print(f"\n{'─'*50}")
    print(f"  Found {len(project_types)} project(s):  {',  '.join(project_types.keys())}")
    print(f"{'─'*50}")

    for proj, types in project_types.items():
        print(f"\nProject: {proj}  —  {len(types)} type(s): {', '.join(types)}")
        while True:
            choice = input(f"\n  For project '{proj}': Same images for ALL types? (y/n): ").strip().lower()
            if choice in ('y', 'n'): break
            print(' Enter y or n.')

        pdata = {'_same_for_all': False}
        if choice == 'y':
            while True:
                try:
                    files = validate_folder(clean_path(input(f"\n  Folder path for project '{proj}' (all types): ")))
                    print(f"  ✓ {len(files)} image(s) found")
                    pdata['_same_for_all'] = True
                    pdata['_images'] = files
                    break
                except FileNotFoundError as e:
                    print(f"  ✗ {e} — try again")
        else:
            for t in types:
                while True:
                    try:
                        files = validate_folder(clean_path(input(f"\n  Folder path for [{proj} -> {t}]: ")))
                        print(f"  ✓ {len(files)} image(s) found")
                        pdata[t] = files
                        break
                    except FileNotFoundError as e:
                        print(f"  ✗ {e} — try again")

        mapping[proj] = pdata

    return mapping

# ═══════════════════════════════════════════════════════════════════
#  COLLECT IMAGE PATHS FROM USER
# ═══════════════════════════════════════════════════════════════════
def clean_path(raw: str) -> str:
    return raw.strip().strip('"').strip("'")

def validate_folder(folder: str) -> list:
    p = Path(folder)
    if not p.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder}")
    exts  = {".jpg", ".jpeg", ".png", ".webp"}
    files = sorted(str(f) for f in p.iterdir() if f.suffix.lower() in exts)
    if not files:
        raise FileNotFoundError(f"No images in: {folder}")
    return files

def collect_image_mapping(types: list) -> dict:
    print(f"\n{'─'*50}")
    print(f"  Found {len(types)} type(s):  {',  '.join(types)}")
    print(f"{'─'*50}")

    while True:
        choice = input("\n  Same images for ALL types? (y/n): ").strip().lower()
        if choice in ("y", "n"):
            break
        print("  Enter y or n.")

    # For backward compatibility, this function will collect a flat mapping
    # of types → files. Higher-level per-project grouping is handled in
    # `collect_image_mapping_per_project` which calls this helper per project.
    mapping = {}

    if choice == "y":
        while True:
            try:
                files = validate_folder(clean_path(input("\n  Folder path (all types): ")))
                print(f"  ✓ {len(files)} image(s) found")
                for t in types:
                    mapping[t] = files
                break
            except FileNotFoundError as e:
                print(f"  ✗ {e} — try again")
    else:
        for t in types:
            while True:
                try:
                    files = validate_folder(clean_path(input(f"\n  Folder path for  [{t}]: ")))
                    print(f"  ✓ {len(files)} image(s) found")
                    mapping[t] = files
                    break
                except FileNotFoundError as e:
                    print(f"  ✗ {e} — try again")

    return mapping

def confirm_mapping(mapping: dict) -> bool:
    # Mapping may be per-project (nested) or flat (type->files). Detect shape.
    print(f"\n{'═'*50}")
    print("  IMAGE MAPPING SUMMARY")
    print(f"{'─'*50}")
    first_val = next(iter(mapping.values())) if mapping else None
    # Nested per-project mapping
    if isinstance(first_val, dict):
        for proj, pdata in mapping.items():
            same = pdata.get("_same_for_all", False)
            if same:
                files = pdata.get("_images", [])
                folder = str(Path(files[0]).parent) if files else "—"
                print(f"  {proj}  (same for all types)")
                print(f"    ALL TYPES  →  {len(files)} image(s)")
                print(f"    {folder}")
            else:
                print(f"  {proj}  (per type)")
                for t, files in pdata.items():
                    if t.startswith("_"): continue
                    folder = str(Path(files[0]).parent) if files else "—"
                    print(f"    {t:<15} →  {len(files)} image(s)  from  {folder}")
    else:
        for t, files in mapping.items():
            folder = str(Path(files[0]).parent) if files else "—"
            print(f"  {t:<20} →  {len(files)} image(s)")
            print(f"  {'':20}    {folder}")
    print(f"{'═'*50}")
    while True:
        ans = input("\n  Look good? Start? (y/n): ").strip().lower()
        if ans == "y": return True
        if ans == "n": return False
        print("  Enter y or n.")

def update_image_mapping(existing_mapping: dict, new_types: list) -> dict:
    """
    Check for NEW unit types on current page.
    If found, ask user for paths only for new types.
    Reuse existing paths for known types.
    """
    # Support both flat mapping and per-project nested mapping
    first_val = next(iter(existing_mapping.values())) if existing_mapping else None
    if isinstance(first_val, dict):
        # existing_mapping is per-project
        # Expect new_types to be list of (project, type) tuples in this mode
        new_types_needed = []
        for proj, t in new_types:
            pdata = existing_mapping.get(proj, {})
            if pdata.get("_same_for_all"):
                continue
            if t not in pdata:
                new_types_needed.append((proj, t))
    else:
        new_types_needed = [t for t in new_types if t not in existing_mapping]
    
    if not new_types_needed:
        # All types already known, no new asking needed
        print(f"\n  All {len(new_types)} type(s) already saved:  {',  '.join(new_types)}")
        return existing_mapping
    
    # Found new types, ask user for paths
    print(f"\n{'─'*50}")
    if isinstance(first_val, dict):
        # new_types is list of (proj, type) tuples
        found_list = ', '.join(f"{p}->{t}" for p, t in new_types)
        needed_list = ', '.join(f"{p}->{t}" for p, t in new_types_needed)
        print(f"  Found entries: {found_list}")
        print(f"  New entry(s) needed: {needed_list}")
    else:
        print(f"  Found {len(new_types)} type(s):  {',  '.join(new_types)}")
        print(f"  New type(s):  {',  '.join(new_types_needed)}")
        print(f"  Saved type(s):  {',  '.join([t for t in new_types if t in existing_mapping])}")
    print(f"{'─'*50}")
    
    if isinstance(first_val, dict):
        for proj, t in new_types_needed:
            pdata = existing_mapping.setdefault(proj, {"_same_for_all": False})
            while True:
                try:
                    files = validate_folder(clean_path(input(f"\n  Folder path for [{proj} -> {t}]: ")))
                    print(f"  ✓ {len(files)} image(s) found")
                    pdata[t] = files
                    break
                except FileNotFoundError as e:
                    print(f"  ✗ {e} — try again")
    else:
        for t in new_types_needed:
            while True:
                try:
                    files = validate_folder(clean_path(input(f"\n  Folder path for [{t}]: ")))
                    print(f"  ✓ {len(files)} image(s) found")
                    existing_mapping[t] = files
                    break
                except FileNotFoundError as e:
                    print(f"  ✗ {e} — try again")
    
    # Show summary for new types
    print(f"\n{'═'*50}")
    print("  NEW MAPPINGS ADDED")
    print(f"{'─'*50}")
    first_val = next(iter(existing_mapping.values())) if existing_mapping else None
    if isinstance(first_val, dict):
        for proj, t in new_types_needed:
            pdata = existing_mapping.get(proj, {})
            files = pdata.get(t) if pdata.get('_same_for_all') is not True else pdata.get('_images', [])
            folder = str(Path(files[0]).parent) if files else "—"
            print(f"  {proj} -> {t:<20} →  {len(files)} image(s)")
            print(f"  {'':20}    {folder}")
    else:
        for t in new_types_needed:
            files = existing_mapping[t]
            folder = str(Path(files[0]).parent) if files else "—"
            print(f"  {t:<20} →  {len(files)} image(s)")
            print(f"  {'':20}    {folder}")
    print(f"{'═'*50}")
    
    while True:
        ans = input("\n  All good? Continue? (y/n): ").strip().lower()
        if ans == "y": return existing_mapping
        if ans == "n":
            print("\n  Cancelled.")
            return None
        print("  Enter y or n.")

# ═══════════════════════════════════════════════════════════════════
#  CHECK PUBLISH STATUS
# ═══════════════════════════════════════════════════════════════════
def check_publish_status(page: Page) -> str:
    """
    Check the color of the dot indicator next to 'Publish Unit (Clients)' button.
    Returns: 'red' (not published), 'green' (already published), 'faded' (already published),
    or 'disabled' (button is disabled, unit cannot be published).
    """
    try:
        # Use .first to avoid strict mode violation when multiple tabs are open
        btn = page.locator('button:has-text("Publish Unit (Clients)")').first
        btn.wait_for(state="visible", timeout=SLOW_TIMEOUT)
        
        # Check if button is disabled (e.g., duplicate unit already published)
        if not btn.is_enabled():
            disabled_msg = btn.get_attribute("title") or "disabled"
            print(f"   ⚠ Publish button is disabled: {disabled_msg}")
            return "disabled"
        
        dot = btn.locator('span.dot').first
        dot.wait_for(state="visible", timeout=SLOW_TIMEOUT)
        
        bg_color = dot.evaluate("el => window.getComputedStyle(el).backgroundColor")
        
        if "220, 53, 69" in bg_color:  # rgb(220, 53, 69) — #dc3545 (red/danger)
            return "red"
        elif "40, 167, 69" in bg_color:  # rgb(40, 167, 69) — #28a745 (green/success)
            return "green"
        else:
            return "faded"  # Any other color (gray, etc.) = already published
    except Exception as e:
        print(f"   ⚠ Could not check publish status: {e} — assuming RED (not published)")
        return "red"

# ═══════════════════════════════════════════════════════════════════
#  GO BACK TO LIST (extracted helper)
# ═══════════════════════════════════════════════════════════════════
def go_back_to_list(page: Page):
    """
    Navigate back to the list page and close any open detail tabs.
    Used both after publishing and when skipping already-published units.
    """
    print("   ↩ Going back to list page...")
    # Wait for any freeze overlay to disappear first (may take time on slow systems)
    try:
        page.wait_for_selector(".freeze-message-container", state="hidden", timeout=SLOW_TIMEOUT)
    except Exception:
        pass  # Overlay might not exist, continue anyway
    page.locator("button, a", has_text=re.compile(r"\bBack\b", re.IGNORECASE)).first.click(timeout=SLOW_TIMEOUT)
    page.wait_for_load_state("networkidle", timeout=SLOW_TIMEOUT)
    page.wait_for_selector("div.card.cursor-pointer.rounded-0", timeout=SLOW_TIMEOUT)

    # Close any open detail tabs so the next card always opens fresh.
    # The close button selector is button.close-button (confirmed from DOM inspection).
    # If we don't close tabs, the CRM reuses the open tab instead of rendering fresh,
    # so Image Manager never appears for subsequent units.
    try:
        close_btns = page.locator("button.close-button")
        count = close_btns.count()
        for j in range(count):
            try:
                close_btns.nth(j).click(timeout=SLOW_TIMEOUT)
                page.wait_for_selector("button.close-button", state="hidden", timeout=SLOW_TIMEOUT)
            except Exception:
                # If one tab fails to close, skip it and continue with others
                continue
        if count > 0:
            print(f"   ✓ Closed {count} detail tab(s)")
    except Exception:
        # Tab close is not critical, just log and continue
        pass

    page.wait_for_selector("div.card.cursor-pointer.rounded-0", timeout=SLOW_TIMEOUT)
    print("   ✓ Back to list")

# ═══════════════════════════════════════════════════════════════════
#  PRICE LOGIC
# ═══════════════════════════════════════════════════════════════════
def decide_price(page: Page) -> str:
    if PRICE_MODE == "down_payment":
        print("   ↳ PRICE_MODE override: down_payment")
        return "down_payment"
    if PRICE_MODE == "unit_price":
        print("   ↳ PRICE_MODE override: unit_price")
        return "unit_price"

    try:
        MODAL = ".modal-mask"
        page.wait_for_selector(MODAL, state="visible", timeout=SLOW_TIMEOUT)

        # Values are inside readonly <input> elements — innerText misses them.
        # We must read .value via JavaScript directly from the DOM inputs.
        # Wait until Unit Price input has a non-empty value (confirms Vue rendered).
        try:
            page.wait_for_function(
                """() => {
                    const blocks = document.querySelectorAll('.modal-mask .field-block');
                    for (const block of blocks) {
                        const label = block.querySelector('.field-label');
                        const input = block.querySelector('input.readonly-input');
                        if (label && input && label.innerText.trim() === 'Unit Price' && input.value.trim() !== '') {
                            return true;
                        }
                    }
                    return false;
                }""",
                timeout=SLOW_TIMEOUT
            )
            print("   ↳ Modal field values confirmed loaded")
        except Exception:
            print("   ↳ ⚠ Timed out waiting for field values — proceeding anyway")

        # Extract all field label→value pairs from the modal using JS
        # because values live in readonly inputs, not in innerText
        field_map = page.evaluate("""() => {
            const result = {};
            const blocks = document.querySelectorAll('.modal-mask .field-block');
            for (const block of blocks) {
                const label = block.querySelector('.field-label');
                const input = block.querySelector('input.readonly-input');
                if (label && input) {
                    result[label.innerText.trim()] = input.value.trim();
                }
            }
            return result;
        }""")

        print(f"   ↳ Fields extracted: { {k: v for k, v in field_map.items() if k in ('Unit Price', 'Down Payment', 'Selling Price (EGP)')} }")

        # Get Down Payment value
        dp_raw = field_map.get("Down Payment", "")
        # Get Unit Price value (try both possible label names)
        up_raw = field_map.get("Unit Price", "") or field_map.get("Selling Price (EGP)", "")

        if not dp_raw or not up_raw:
            print(f"   ↳ ⚠ Could not extract both values (DP='{dp_raw}', UP='{up_raw}'). Defaulting to down_payment.")
            return "down_payment"

        try:
            dp = float(re.sub(r"[^\d.]", "", dp_raw))
            up = float(re.sub(r"[^\d.]", "", up_raw))
        except ValueError as e:
            print(f"   ↳ ⚠ Parse error: {e}. Defaulting to down_payment.")
            return "down_payment"

        ratio = (dp / up * 100) if up > 0 else 0
        print(f"   ↳ DP={dp:,.0f}  |  UP={up:,.0f}  |  DP is {ratio:.1f}% of UP")

        # Default is down_payment UNLESS:
        #   - Down Payment is 0, OR
        #   - Down Payment >= DP_THRESHOLD% of Unit Price
        if dp == 0 or ratio >= DP_THRESHOLD:
            print(f"   ↳ Decision: unit_price  (DP=0 or DP≥{DP_THRESHOLD}% of UP)")
            return "unit_price"
        else:
            print(f"   ↳ Decision: down_payment  (DP is {ratio:.1f}% of UP, under {DP_THRESHOLD}%)")
            return "down_payment"

    except Exception as e:
        print(f"   ↳ ⚠ decide_price() exception: {e}. Defaulting to down_payment.")
        return "down_payment"

# ═══════════════════════════════════════════════════════════════════
#  STEP 1 — UPLOAD + TAG IMAGES
# ═══════════════════════════════════════════════════════════════════
def step_upload_images(page: Page, paths: list, project: str, unit_type: str, state: UploadErrorState, mapping: dict) -> int:
    # Multi-project signature: include `project` so mapping updates write into
    # mapping[project][unit_type] (or mapping[project]['_images'] for same_for_all).
    print("\n   ── STEP 1: Image Manager ──")

    random.shuffle(paths)
    print(f"   ↳ Upload order randomized")

    MODAL  = ".modal-mask"
    UPLOAD = ".modal.fade.show"

    # ── Open Image Manager ─────────────────────────────────────────
    print("   ↳ Clicking Image Manager button...")
    page.locator("button", has_text="Image Manager").first.click(timeout=SLOW_TIMEOUT)
    page.locator(MODAL).wait_for(state="visible", timeout=SLOW_TIMEOUT)
    try:
        page.wait_for_selector(f"{MODAL} button.btn-primary", timeout=SLOW_TIMEOUT)
        print("   ✓ Image Manager loaded")
    except Exception:
        page.wait_for_selector(f"{MODAL} button.btn-primary", timeout=SLOW_TIMEOUT)
        print("   ✓ Image Manager opened (fallback wait)")

    # ── Open Upload sub-modal ──────────────────────────────────────
    page.locator(f"{MODAL} button.btn-primary", has_text="Upload").click()
    page.locator(UPLOAD).wait_for(state="visible", timeout=SLOW_TIMEOUT)

    # ── Set files ──────────────────────────────────────────────────
    with page.expect_file_chooser(timeout=SLOW_TIMEOUT) as fc_info:
        page.locator(f"{UPLOAD} .btn-file-upload").first.click()
    fc_info.value.set_files(paths)
    print(f"   ✓ {len(paths)} file(s) queued via file chooser")

    # ── Confirm upload ─────────────────────────────────────────────
    page.locator(f"{UPLOAD} .btn-modal-primary").click()

    # ── Wait for all rows to show a final status icon ──────────────
    # The spinner SVG has display:none when done. We wait until every
    # .file-preview row has either #icon-solid-success or #icon-solid-error.
    print("   ↳ Waiting for uploads to complete…")
    try:
        page.wait_for_function(
            """() => {
                const rows = document.querySelectorAll('.file-preview-container .file-preview');
                // If no rows found, might be different upload state — just return true to proceed
                if (rows.length === 0) {
                    // Check if upload is even happening — look for file info anywhere
                    const hasAnyUploadContent = document.querySelector('.file-preview-container, [class*="upload"], [class*="file"]');
                    return hasAnyUploadContent ? false : true;  // If no upload UI at all, we're done
                }
                // Wait for all rows to have success or error icon
                for (const row of rows) {
                    const success = row.querySelector('use[href="#icon-solid-success"]');
                    const error   = row.querySelector('use[href="#icon-solid-error"]');
                    if (!success && !error) return false;  // still pending
                }
                return true;
            }""",
                timeout=SLOW_TIMEOUT
            )
        print("   ✓ All uploads settled")
        confirm("STEP 1 — Images uploaded. Verify they appeared.")
    except Exception as e:
        print(f"   ⚠ Timeout waiting for upload results ({e}) — continuing anyway")
        page.wait_for_selector('.file-preview-container .file-preview', timeout=SLOW_TIMEOUT)

    # ── Check for errors (ONLY on first upload for this type) ──────
    is_first = state.is_first_upload(unit_type)
    actual_count = len(paths)

    if is_first:
        # Scan every .file-preview row for #icon-solid-error
        failed_filenames = page.evaluate("""() => {
            const failed = [];
            const rows = document.querySelectorAll('.file-preview-container .file-preview');
            for (const row of rows) {
                if (row.querySelector('use[href="#icon-solid-error"]')) {
                    const nameEl = row.querySelector('.file-preview > div:nth-child(2) > div:first-child');
                    failed.push(nameEl ? nameEl.innerText.trim() : 'unknown');
                }
            }
            return failed;
        }""")

        if failed_filenames:
            n_failed = len(failed_filenames)
            n_success = len(paths) - n_failed
            failed_paths = [p for p in paths if Path(p).name in failed_filenames]

            print(f"\n   ⚠ {n_failed} image(s) failed to upload:")
            for fp in failed_paths:
                print(f"      → {fp}")

            if state.should_ask_user(unit_type):
                ans = input(f"\n   ❓ Continue with {n_success} image(s) instead of {len(paths)}? (y/n): ").strip().lower()

                if ans == "y":
                    state.mark_acknowledged(unit_type)
                    successful_paths = [p for p in paths if p not in failed_paths]
                    pdata = mapping.get(project, {})
                    if pdata.get('_same_for_all'):
                        pdata['_images'] = successful_paths
                    else:
                        pdata[unit_type] = successful_paths
                    mapping[project] = pdata
                    actual_count = len(successful_paths)
                    print(f"   ✓ Pool updated to {actual_count} image(s), won't ask again for '{unit_type}'")
                    # Close upload modal and continue normally
                    if page.locator(UPLOAD).is_visible():
                        try:
                            page.locator(f"{UPLOAD} .btn-modal-close").click()
                            page.locator(UPLOAD).wait_for(state="hidden", timeout=SLOW_TIMEOUT)
                            print("   ✓ Upload modal closed")
                        except Exception as e:
                            print(f"   ⚠ Modal close timeout: {e} — continuing")
                            page.wait_for_selector(UPLOAD, state="hidden", timeout=SLOW_TIMEOUT)
                    else:
                        print("   ℹ Upload modal already closed")
                else:
                    # User is NOT OK — show faulty paths, wait, ask for new folder
                    try:
                        page.locator(f"{UPLOAD} .btn-modal-close").click()
                        page.locator(UPLOAD).wait_for(state="hidden", timeout=SLOW_TIMEOUT)
                    except Exception:
                        try:
                            page.press("Escape")
                            page.wait_for_selector(UPLOAD, state="hidden", timeout=SLOW_TIMEOUT)
                        except Exception:
                            pass
                    # Close Image Manager too so user can go back to list
                    try:
                        page.locator(f"{MODAL} button", has_text="Cancel").first.click(timeout=SLOW_TIMEOUT)
                        page.locator(MODAL).wait_for(state="hidden", timeout=SLOW_TIMEOUT)
                    except Exception:
                        pass

                    while True:
                        print(f"\n   ❌ Faulty image(s) that failed:")
                        for fp in failed_paths:
                            print(f"      → {fp}")
                        print(f"\n   Please go back to the list page and delete the bad images from the folder.")
                        input(f"   Press Enter when ready to provide a new folder path… ")

                        new_raw = input(f"   New folder path for [{unit_type}]: ").strip().strip('"').strip("'")
                        try:
                            new_files = validate_folder(new_raw)
                            print(f"   ✓ {len(new_files)} image(s) found in new folder")
                            pdata = mapping.get(project, {})
                            if pdata.get('_same_for_all'):
                                pdata['_images'] = new_files
                            else:
                                pdata[unit_type] = new_files
                            mapping[project] = pdata
                            # Retry upload from scratch with new images
                            print(f"   ↳ Retrying upload with new images…")
                            return step_upload_images(page, new_files, project, unit_type, state, mapping)
                        except FileNotFoundError as e:
                            print(f"   ✗ {e} — try again")
            else:
                # Already acknowledged for this type — silently omit, update pool
                successful_paths = [p for p in paths if p not in failed_paths]
                pdata = mapping.get(project, {})
                if pdata.get('_same_for_all'):
                    pdata['_images'] = successful_paths
                else:
                    pdata[unit_type] = successful_paths
                mapping[project] = pdata
                actual_count = len(successful_paths)
                print(f"   ℹ Already acknowledged for '{unit_type}' — omitting {n_failed} failed image(s), using {actual_count}")
                if page.locator(UPLOAD).is_visible():
                    try:
                        page.locator(f"{UPLOAD} .btn-modal-close").click()
                        page.locator(UPLOAD).wait_for(state="hidden", timeout=SLOW_TIMEOUT)
                        print("   ✓ Upload modal closed")
                    except Exception as e:
                        print(f"   ⚠ Modal close timeout: {e} — continuing")
                        page.wait_for_selector(UPLOAD, state="hidden", timeout=SLOW_TIMEOUT)
                else:
                    print("   ℹ Upload modal already closed")
        else:
            print("   ✓ All images uploaded successfully")
            if page.locator(UPLOAD).is_visible():
                try:
                    page.locator(f"{UPLOAD} .btn-modal-close").click()
                    page.locator(UPLOAD).wait_for(state="hidden", timeout=SLOW_TIMEOUT)
                    print("   ✓ Upload modal closed")
                except Exception as e:
                    print(f"   ⚠ Modal close timeout: {e} — continuing")
                    page.wait_for_selector(UPLOAD, state="hidden", timeout=SLOW_TIMEOUT)
            else:
                print("   ℹ Upload modal already closed")

        state.mark_uploaded(unit_type)

    else:
        # Not first upload for this type — close immediately, no check
        print("   ℹ Not first upload for this type — closing without error check")
        # Check if upload modal is still open before trying to close
        if page.locator(UPLOAD).is_visible():
            try:
                page.locator(f"{UPLOAD} .btn-modal-close").click()
                page.locator(UPLOAD).wait_for(state="hidden", timeout=SLOW_TIMEOUT)
                print("   ✓ Upload modal closed")
            except Exception as e:
                print(f"   ⚠ Could not close modal: {e} — proceeding anyway")
                page.wait_for_selector(UPLOAD, state="hidden", timeout=SLOW_TIMEOUT)
        else:
            print("   ℹ Upload modal already auto-closed, proceeding to tagging…")

    # ── Tag newly uploaded images ──────────────────────────────────
    print(f"   ── Tagging as '{IMAGE_TAG}'…")
    img_cols = page.locator(f"{MODAL} .col-6.col-md-3.col-lg-3.py-2")
    col_count = img_cols.count()
    tagged = 0

    for i in range(col_count):
        col = img_cols.nth(i)
        wrapper = col.locator(".image-wrapper")
        wrapper_class = wrapper.get_attribute("class") or ""
        if "faded" in wrapper_class:
            continue
        tag_btn = col.locator("button", has_text=IMAGE_TAG)
        if tag_btn.count() == 0:
            continue
        btn_class = tag_btn.get_attribute("class") or ""
        if "btn-primary" in btn_class:
            continue
        tag_btn.click()
        page.wait_for_selector(f"{MODAL} .image-wrapper.faded", timeout=SLOW_TIMEOUT)
        tagged += 1

    print(f"   ✓ Tagged {tagged} image(s)")
    confirm("STEP 2 — Tags applied. Verify correct images tagged.")

    # ── Cancel instead of Save (images auto-save, Cancel bypasses permission error) ───
    cancel_btn = page.locator(f"{MODAL} button", has_text="Cancel")
    cancel_btn.wait_for(state="visible", timeout=SLOW_TIMEOUT)
    cancel_btn.click()
    page.locator(MODAL).wait_for(state="hidden", timeout=SLOW_TIMEOUT)
    print("   ✓ Cancelled (images saved normally)")
    confirm("STEP 3 — Image Manager saved. Closed cleanly?")

    return actual_count

# ═══════════════════════════════════════════════════════════════════
#  STEP 2 — PUBLISH UNIT (CLIENTS)
# ═══════════════════════════════════════════════════════════════════
def step_publish(page: Page, n_images: int):
    """
    n_images: number of images just uploaded — select this many newest images.
    """
    print("\n   ── STEP 2: Publish Unit ──")

    MODAL = ".modal-mask"

    # ── Open modal ─────────────────────────────────────────────────
    print("   ↳ Opening Publish Unit (Clients) modal...")
    page.locator("button", has_text="Publish Unit (Clients)").first.click(timeout=SLOW_TIMEOUT)
    page.locator(MODAL).wait_for(state="visible", timeout=SLOW_TIMEOUT)
    page.wait_for_selector("text=Price Display", timeout=SLOW_TIMEOUT)
    print("   ✓ Modal open")

    # ── Price display (Fields tab is default) ──────────────────────
    UNIT_PRICE_CB   = f"{MODAL} label.price-display__option:first-child input[type='checkbox']"
    DOWN_PAYMENT_CB = f"{MODAL} label.price-display__option:last-child input[type='checkbox']"

    choice = decide_price(page)
    print(f"   ↳ Choice made: {choice}")

    up_checked = page.locator(UNIT_PRICE_CB).is_checked()
    dp_checked = page.locator(DOWN_PAYMENT_CB).is_checked()
    print(f"   ↳ Current state: Unit Price={up_checked}  |  Down Payment={dp_checked}")

    if choice == "down_payment":
        print(f"   ↳ Setting to: Down Payment")
        if page.locator(UNIT_PRICE_CB).is_checked():
            page.locator(UNIT_PRICE_CB).click()
            page.wait_for_function(
                f"""() => {{
                    const el = document.querySelector({UNIT_PRICE_CB!r});
                    return el ? !el.checked : false;
                }}""",
                timeout=SLOW_TIMEOUT,
            )
        if not page.locator(DOWN_PAYMENT_CB).is_checked():
            page.locator(DOWN_PAYMENT_CB).click()
    else:
        print(f"   ↳ Setting to: Unit Price")
        if page.locator(DOWN_PAYMENT_CB).is_checked():
            page.locator(DOWN_PAYMENT_CB).click()
            page.wait_for_function(
                f"""() => {{
                    const el = document.querySelector({DOWN_PAYMENT_CB!r});
                    return el ? !el.checked : false;
                }}""",
                timeout=SLOW_TIMEOUT,
            )
        if not page.locator(UNIT_PRICE_CB).is_checked():
            page.locator(UNIT_PRICE_CB).click()

    up_final = page.locator(UNIT_PRICE_CB).is_checked()
    dp_final = page.locator(DOWN_PAYMENT_CB).is_checked()
    print(f"   ↳ Final state: Unit Price={up_final}  |  Down Payment={dp_final}")
    confirm("STEP 4 — Price display correct?")

    # ── Switch to Images tab ───────────────────────────────────────
    print("   ── Switching to Images tab…")
    page.locator(f"{MODAL} button", has_text="Images").click()
    page.wait_for_selector(f"{MODAL} .col-6.col-md-3.col-lg-3.py-2", timeout=SLOW_TIMEOUT)
    confirm("STEP 5 — Images tab open. Select your images.")

    # ── Select the N newest images ─────────────────────────────────
    img_cols = page.locator(f"{MODAL} .col-6.col-md-3.col-lg-3.py-2")
    col_count = img_cols.count()
    to_select = min(n_images, col_count)
    selected = 0

    for i in range(to_select):
        col = img_cols.nth(i)
        wrapper = col.locator(".image-wrapper")
        wrapper_class = wrapper.get_attribute("class") or ""
        if "faded" in wrapper_class:
            wrapper.click()
            page.wait_for_selector(f"{MODAL} .col-6.col-md-3.col-lg-3.py-2", timeout=SLOW_TIMEOUT)
            selected += 1
        else:
            selected += 1

    print(f"   ✓ {selected}/{to_select} image(s) selected for publish")

    # ── Check Published checkbox ───────────────────────────────────
    page.locator(f"{MODAL} button", has_text="Fields").click()
    page.wait_for_selector("text=Price Display", timeout=SLOW_TIMEOUT)

    try:
        published_cb = page.locator(f"{MODAL}").get_by_text("Published", exact=False).last.locator("ancestor::label input[type='checkbox']")
        if not published_cb.is_checked():
            published_cb.check()
            page.wait_for_selector(f"{MODAL} input[type='checkbox']", timeout=SLOW_TIMEOUT)
            print("   ✓ Published checkbox enabled")
    except Exception:
        try:
            published_cb = page.locator(f"{MODAL} input[type='checkbox']").last
            if not published_cb.is_checked():
                published_cb.check()
                page.wait_for_selector(f"{MODAL} input[type='checkbox']", timeout=SLOW_TIMEOUT)
                print("   ✓ Published checkbox enabled")
        except Exception as e:
            print(f"   ⚠ Could not find Published checkbox: {e}")

    # ── Save ───────────────────────────────────────────────────────
    page.locator(f"{MODAL} button", has_text="Save").click()
    print("   ✓ Save clicked")

    # Wait for modal to CLOSE — confirms save completed.
    # System is very slow, so wait up to 45s for modal to close.
    try:
        page.locator(MODAL).wait_for(state="hidden", timeout=SLOW_TIMEOUT)
        print("   ✓ Modal closed — save confirmed")
    except Exception:
        print("   ⚠ Modal did not close in expected time — continuing anyway")
        page.wait_for_selector(MODAL, state="hidden", timeout=SLOW_TIMEOUT)

    # ── Wait for freeze overlay to disappear (system freeze message) ──
    # The CRM shows a freeze-message-container during processing that blocks clicks.
    # Wait for it to disappear before trying to navigate back.
    try:
        page.wait_for_selector(".freeze-message-container", state="hidden", timeout=SLOW_TIMEOUT)
        print("   ✓ Freeze overlay cleared")
    except Exception:
        print("   ⚠ Freeze overlay still visible, proceeding anyway")

    confirm("STEP 6 — Publish saved. Listing looks correct?")

    # ── Go back to list ────────────────────────────────────────────
    go_back_to_list(page)

# ═══════════════════════════════════════════════════════════════════
#  PROCESS ONE UNIT
# ═══════════════════════════════════════════════════════════════════
def process_unit(page: Page, url: str, name: str, mapping: dict, states: dict):
    # Resolve project and type from name
    parsed_project, parsed_type = extract_project_and_type(name)

    def normalize(value: str) -> str:
        return re.sub(r"[\s\-_]+", "", value).lower()

    # Resolve project key in mapping
    project = None
    if parsed_project in mapping:
        project = parsed_project
    else:
        nproj = normalize(parsed_project)
        for key in mapping:
            if normalize(key) == nproj or nproj in normalize(key) or normalize(key) in nproj:
                project = key
                break
    if not project:
        raise KeyError(f"Could not resolve project for '{name}'. Known projects: {list(mapping.keys())}")

    # Resolve unit type within project mapping
    pdata = mapping.get(project, {})
    if pdata.get("_same_for_all"):
        utype = parsed_type
        images = pdata.get("_images", [])
    else:
        # Try direct match, then normalized matches
        if parsed_type in pdata:
            utype = parsed_type
            images = pdata[utype]
        else:
            ntype = normalize(parsed_type)
            match = None
            for key in pdata:
                if key.startswith("_"): continue
                if normalize(key) == ntype or ntype in normalize(key) or normalize(key) in ntype:
                    match = key
                    break
            if match:
                utype = match
                images = pdata[utype]
            else:
                raise KeyError(f"Could not resolve unit type '{parsed_type}' for project '{project}'. Available: { [k for k in pdata.keys() if not k.startswith('_')] }")

    print(f"   Project: {project}  |  Type: {utype}  |  {len(images)} image(s)")
    if url:
        page.goto(url)
        page.wait_for_load_state("networkidle", timeout=SLOW_TIMEOUT)

    # ── Check if unit is already published ──────────────────────────
    publish_status = check_publish_status(page)

    if publish_status == "red":
        # Not published — proceed normally with upload and publish
        print("   ↳ Status: Not published (RED) — proceeding normally with upload & publish")
        state = states.get(project) if isinstance(states, dict) else states
        actual_count = step_upload_images(page, images, project, utype, state, mapping)
        step_publish(page, n_images=actual_count)
    elif publish_status == "disabled":
        # Button is disabled (e.g., duplicate unit) — skip to next unit
        print(f"   ↳ Status: Cannot publish (DISABLED) — skipping to next unit")
        go_back_to_list(page)
    else:
        # Already published (green or faded) — skip to next unit
        print(f"   ↳ Status: Already published ({publish_status.upper()}) — skipping to next unit")
        go_back_to_list(page)

# ═══════════════════════════════════════════════════════════════════
#  PROCESS CURRENT PAGE  (only what's loaded right now)
# ═══════════════════════════════════════════════════════════════════
def process_current_page(page: Page, mapping: dict, page_num: int, results: list, states: dict):
    list_url = page.url

    links = page.locator("a[href*='/resale-unit/'], a[href*='realestate-workspace/resale-unit/']").all()
    hrefs = list(dict.fromkeys(
        a.get_attribute("href") for a in links if a.get_attribute("href")
    ))

    hrefs = [h for h in hrefs if h and "realestate-workspace/resale-unit?" not in h and not h.endswith("/resale-unit")]

    if hrefs:
        print(f"\n  📄 Page {page_num}  —  {len(hrefs)} unit(s)")
        print(f"{'─'*50}")

        unit_data = []
        for href in hrefs:
            link_el = page.locator(f"a[href='{href}']").first
            name = link_el.inner_text().strip()
            if not name:
                try:
                    name = link_el.locator("h2, h3, [class*='title']").first.inner_text().strip()
                except Exception:
                    name = href.split("/")[-1]
            url = href if href.startswith("http") else BASE_URL + href
            unit_data.append((name, url))

        for i, (name, url) in enumerate(unit_data, 1):
            print(f"\n  [{i}/{len(unit_data)}]  {name}")
            try:
                process_unit(page, url, name, mapping, states)
                results.append({"page": page_num, "unit": name, "url": url, "status": "OK"})
                print("  ✅ Done")
            except Exception as e:
                print(f"  ✗ ERROR: {e}")
                results.append({"page": page_num, "unit": name, "url": url, "status": f"FAILED: {e}"})
                input("  ⚠ Fix manually if needed, then press Enter to continue… ")

        return

    # Fallback path: loop DOM cards directly by position.
    # We do NOT use a text-extracted name list because the page body contains
    # each unit name twice (tab header + card), creating phantom duplicates
    # that shift all indexes and cause every unit after the first to click
    # the wrong card. By reading the name FROM the card we are about to click,
    # the name and the click target are always the same element — no drift,
    # no mismatch, duplicate names are handled correctly.
    card_selector = "div.card.cursor-pointer.rounded-0"
    cards = page.locator(card_selector)
    total_cards = cards.count()

    if total_cards == 0:
        print("  ⚠ No unit cards found in DOM. Are you on the list page?")
        return

    print(f"\n  📄 Page {page_num}  —  {total_cards} unit(s) [DOM cards]")
    print(f"{'─'*50}")

    for i in range(total_cards):
        card_el = cards.nth(i)

        # Read the name directly from the card element
        try:
            card_text = card_el.inner_text().strip()
            name = card_text.split("\n")[0].strip()
        except Exception:
            card_text = ""
            name = f"Unit {i + 1}"

        print(f"\n  [{i + 1}/{total_cards}]  {name}")
        try:
            # PRE-CLICK DIAGNOSTICS
            print(f"   [DEBUG] Total cards available: {cards.count()}")
            print(f"   [DEBUG] Current URL: {page.url}")
            print(f"   [DEBUG] Targeting card #{i}: {name}")
            
            # Verify the card we're about to click is valid
            card_rect = card_el.bounding_box()
            if card_rect:
                print(f"   [DEBUG] Card position - X:{card_rect['x']:.0f} Y:{card_rect['y']:.0f} W:{card_rect['width']:.0f} H:{card_rect['height']:.0f}")
            
            # Make sure we are on the list page before clicking
            if "RESALE-" in page.url:
                page.wait_for_load_state("networkidle")

            card_el.scroll_into_view_if_needed()
            print(f"   [DEBUG] Click executing...")
            card_el.click(timeout=SLOW_TIMEOUT)
            print(f"   [DEBUG] Click executed - waiting for navigation...")
            page.wait_for_load_state("networkidle", timeout=SLOW_TIMEOUT)

            # POST-CLICK DIAGNOSTICS
            print(f"   [DEBUG] After click - New URL: {page.url}")
            print(f"   [DEBUG] Page title: {page.title()}")
            
            # Check page content before looking for button
            try:
                detail_indicators = page.locator("h1, h2, [class*='title'], [class*='heading']").first.inner_text() if page.locator("h1, h2, [class*='title'], [class*='heading']").count() > 0 else "N/A"
                print(f"   [DEBUG] Page heading detected: {detail_indicators}")
            except Exception:
                print(f"   [DEBUG] Page heading detection failed")
            
            # Check for overlay blocking
            try:
                overlay = page.locator("[role='dialog'], .modal, .overlay, [class*='backdrop']").first
                if overlay.is_visible(timeout=SLOW_TIMEOUT):
                    print(f"   [DEBUG] ⚠ Overlay detected after click!")
            except Exception:
                pass

            # Wait for Image Manager to fully render.
            # networkidle fires too early — Vue still needs time to mount buttons.
            # wait_for_selector polls until visible instead of checking once.
            page.wait_for_selector("button:has-text('Image Manager')", state="visible", timeout=SLOW_TIMEOUT)

            print(f"   ✓ Opened detail page")
            process_unit(page, None, name, mapping, states)
            results.append({"page": page_num, "unit": name, "url": page.url, "status": "OK"})
            print("  ✅ Done")
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results.append({"page": page_num, "unit": name, "url": page.url, "status": f"FAILED: {e}"})
            input("  ⚠ Fix manually if needed, then press Enter to continue… ")

# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════
def main():
    print("\n" + "═"*50)
    print("  EGY Property Automation")
    print("═"*50)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception:
            print("\n❌  Cannot connect to Chrome.")
            print("    Launch it first with launch_chrome.bat\n")
            raise

        ctx  = browser.contexts[0]
        page = ctx.pages[0]
        print(f"\n✅  Connected  |  {page.url}")
        print("\n  → Navigate to the filtered unit list in Chrome")
        print("  → Set your filters and Available checkbox")
        input("  → Press Enter when ready… \n")

        print("  Scanning page for projects and unit types…")
        project_types = scan_projects_types(page)
        if not project_types:
            print("  ✗ No unit projects/types found. Are you on the list page?")
            return

        mapping = collect_image_mapping_per_project(project_types)
        if not confirm_mapping(mapping):
            print("\n  Cancelled.")
            return

        # Per-project upload error state tracker
        states = { proj: UploadErrorState(same_images_mode=mapping[proj].get('_same_for_all', False)) for proj in mapping }
        print(f"   Projects configured: {', '.join(mapping.keys())}")

        results  = []
        page_num = 1

        while True:
            # ═══════════════════════════════════════════════════════════════
            #  PAGE SCANNING LOGIC (Different behavior based on mode)
            # ═══════════════════════════════════════════════════════════════
            if page_num > 1:
                print(f"\n  Scanning page {page_num} for projects/types…")
                current_projects = scan_projects_types(page)
                if current_projects:
                    # For each project on the page, adapt mapping as needed
                    for proj, types in current_projects.items():
                        if proj not in mapping:
                            print(f"\n  New project found: {proj} — requesting image folders")
                            new_map = collect_image_mapping_per_project({proj: types})
                            mapping.update(new_map)
                            states[proj] = UploadErrorState(same_images_mode=mapping[proj].get('_same_for_all', False))
                        else:
                            if mapping[proj].get('_same_for_all'):
                                print(f"\n  Skipping per-type checks for project '{proj}' — _same_for_all=True")
                                # nothing to do for this project
                                continue
                            new_types = [t for t in types if t not in mapping[proj]]
                            if not new_types:
                                print(f"\n  No new types for project '{proj}' — nothing to prompt")
                            if new_types:
                                updated = update_image_mapping(mapping, [(proj, t) for t in new_types])
                                if updated is None:
                                    print("\n  Cancelled.")
                                    break
                                mapping = updated

            process_current_page(page, mapping, page_num, results, states)

            pg_ok   = sum(1 for r in results if r["page"] == page_num and r["status"] == "OK")
            pg_fail = sum(1 for r in results if r["page"] == page_num and r["status"] != "OK")
            print(f"\n{'─'*50}")
            print(f"  Page {page_num} complete  —  {pg_ok} OK  |  {pg_fail} failed")

            try:
                indicator = page.locator(
                    "[class*='pagination'], [class*='page-info'], text=/ \\d"
                ).first.inner_text().strip()
                print(f"  Browser shows: {indicator}")
            except Exception:
                pass

            print(f"{'─'*50}")
            print("  Enter  →  I moved to next page, continue")
            print("  done   →  Finish and save results")
            ans = input("\n  > ").strip().lower()
            if ans == "done":
                break

            page_num += 1
            page.wait_for_load_state("networkidle")

        with open("results.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["page", "unit", "url", "status"])
            w.writeheader()
            w.writerows(results)

        total_ok   = sum(1 for r in results if r["status"] == "OK")
        total_fail = len(results) - total_ok
        print(f"\n{'═'*50}")
        print(f"  ✅  All done!  {total_ok} OK  |  {total_fail} failed")
        print(f"  📄  results.csv saved")
        print(f"{'═'*50}\n")

if __name__ == "__main__":
    main()