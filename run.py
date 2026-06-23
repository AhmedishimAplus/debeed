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

from _exe_setup import setup, launch_chrome
setup()
import re, csv, random, sys, builtins, threading, time
from datetime import datetime
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

# Active workspace tab, set at runtime in main(): "Re-Sale" | "Rent" | "Primary".
# Re-Sale is the default and original behaviour. Rent reuses the entire flow but
# skips the price/down-payment logic in step_publish (no price decision needed).
CURRENT_VIEW   = "Re-Sale"

# Failure thresholds for image upload (see step_upload_images):
#   ≤ 25% failed  → ask user, auto-continue after timeout
#   > 25% failed  → skip whole unit, log failed paths
#   100% failed   → skip whole unit (no paths needed)
UPLOAD_FAIL_SKIP_PCT  = 25.0
UPLOAD_PROMPT_TIMEOUT = 120   # seconds to wait for user before auto-continuing


# ═══════════════════════════════════════════════════════════════════
#  FILTERED LIST URL  (saved after Continue; normalized so the view shows
#  non-published units, 100 per page)
# ═══════════════════════════════════════════════════════════════════
_filtered_list_url = None


def _normalize_list_url(url: str):
    """Force non_published_units=1 and page_length=100 in the list URL, leaving
    everything else untouched. Returns (new_url, messages) — one human-readable
    message per value that had to be fixed. Both fixes are baked into the single
    returned URL."""
    if not url:
        return url, []
    messages = []
    new = url

    # non_published_units → 1 (handles URL-encoded %22 and literal " quoting).
    m = re.search(r'non_published_units(?:%22|")\s*:\s*(\d+)', new)
    if m and m.group(1) != "1":
        if m.group(1) == "0":
            messages.append("‘Not Published (to clients)’ checkbox isn’t ticked — ticking it now.")
        else:
            messages.append(f"non_published_units is {m.group(1)} — setting it to 1.")
        new = re.sub(r'(non_published_units(?:%22|")\s*:\s*)\d+', r'\g<1>1', new)

    # page_length → 100.
    m = re.search(r'page_length=(\d+)', new)
    if m and m.group(1) != "100":
        messages.append(f"Page length is {m.group(1)} — changing it to 100.")
        new = re.sub(r'(page_length=)\d+', r'\g<1>100', new)

    return new, messages


# ═══════════════════════════════════════════════════════════════════
#  SKIP SIGNAL + TIMED INPUT
# ═══════════════════════════════════════════════════════════════════
class UnitSkipped(Exception):
    """Raised inside processing to abort the current unit, log it as failed,
    and move straight to the next unit without a manual 'fix it' prompt."""
    pass


class VersionRefresh(Exception):
    """Raised when the Frappe 'Version Updated' modal is detected (MutationObserver,
    networkidle wrapper, upload-JS throw, or the GUI 'Refresh Page' button). Caught
    in main() → reload _filtered_list_url, rescan, reprocess from card 1. Run data
    (mapping, states, results, _successful_uploads) is preserved."""
    pass


# Set True the instant the Version Updated modal appears: the in-browser
# MutationObserver calls the exposed Python fn (__onVersionModal), or the GUI
# 'Refresh Page' button sets it. Read by _check_version_refresh / the networkidle
# wrapper to raise VersionRefresh. Cleared after each reload. Cross-thread: GUI
# main thread sets it, automation thread reads it (atomic bool assign).
_version_refresh_pending = False

# Which layer noticed the modal — for the log, so the unpredictable real modal
# tells us how it was caught: "observer" / "networkidle" / "upload" / "checkpoint"
# / "manual button". Set alongside _version_refresh_pending.
_version_detected_by = ""

# Hook installed by gui.py: called once _filtered_list_url is saved so the GUI can
# reveal its 'Refresh Page' button. None in terminal mode. Signature: () -> None
_notify_url_saved = None


# Hook installed by gui.py so a timed-out prompt can release the blocked GUI
# input and dismiss its panel. None in terminal mode. Signature: (default) -> None
_cancel_gui_input = None

# Sentinel returned by input_with_timeout when a caller wants to detect a timeout
# rather than substitute a literal answer.
_TIMED_OUT = "\x00__TIMED_OUT__\x00"


def input_with_timeout(prompt: str, timeout: int = UPLOAD_PROMPT_TIMEOUT,
                       default: str = "y", announce=None,
                       announce_every: int = 10, announce_tail: int = 5,
                       final_msg: bool = True) -> str:
    """Ask the user, but auto-answer `default` if there is no response within
    `timeout` seconds. A live countdown is printed while waiting. Works in both
    terminal and GUI mode (GUI releases the pending prompt via _cancel_gui_input).

    `announce(remaining_secs) -> str` customizes the countdown line; when given,
    the generic final timeout line is suppressed (set `final_msg=False` so the
    caller prints its own). `announce_every` / `announce_tail` control cadence.
    """
    box = {}
    answered = threading.Event()

    def worker():
        try:
            box["value"] = input(prompt)
        except Exception:
            box["value"] = default
        finally:
            answered.set()

    threading.Thread(target=worker, daemon=True).start()

    end = time.time() + timeout
    announced = set()
    while not answered.wait(0.25):
        remaining = end - time.time()
        if remaining <= 0:
            break
        secs = int(remaining + 0.999)
        if (secs % announce_every == 0 or (announce_tail and secs <= announce_tail)) \
                and secs not in announced:
            announced.add(secs)
            if announce is not None:
                msg = announce(secs)
                if msg:
                    print(msg)
            else:
                m, s = divmod(secs, 60)
                print(f"   ⏳ Auto-continuing in {m}:{s:02d} if no response…")

    if answered.is_set():
        return box.get("value", default)

    if final_msg and announce is None:
        m, s = divmod(timeout, 60)
        print(f"   ⏱ No response after {m}:{s:02d} — continuing as if you answered "
              f"'{default}'.")
    if callable(_cancel_gui_input):
        try:
            _cancel_gui_input(default)
        except Exception:
            pass
    answered.wait(2)   # let the worker drain the injected response, if any
    return default


def _dismiss_upload_modals(page):
    """Best-effort close of the Upload sub-modal and Image Manager modal so the
    page is left clean before a unit is skipped."""
    UPLOAD = ".modal.fade.show"
    MODAL  = ".modal-mask"
    try:
        if page.locator(UPLOAD).is_visible():
            page.locator(f"{UPLOAD} .btn-modal-close").click(timeout=SLOW_TIMEOUT)
            page.locator(UPLOAD).wait_for(state="hidden", timeout=SLOW_TIMEOUT)
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    try:
        if page.locator(MODAL).is_visible():
            page.locator(f"{MODAL} button", has_text="Cancel").first.click(timeout=SLOW_TIMEOUT)
            page.locator(MODAL).wait_for(state="hidden", timeout=SLOW_TIMEOUT)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
#  "VERSION UPDATED" MODAL  →  REFRESH FROM SAVED FILTERED LIST
# ═══════════════════════════════════════════════════════════════════
# Frappe shows a "Version Updated" msgprint dialog at random mid-run. Its own
# "Refresh" button reloads the detail page (wrong — loses our place). Instead we
# reload the saved filtered list, rescan, and reprocess from card 1. The
# non_published_units=1 filter drops already-published units, so the replay only
# touches units that still need work.
_VERSION_MODAL_TITLE = "Version Updated"

# In-browser MutationObserver: fires the instant the modal is added to the DOM and
# calls the exposed Python fn. add_init_script re-installs it on every navigation.
_VERSION_OBSERVER_JS = """
    (() => {
        if (window.__versionObserverInstalled) return;
        window.__versionObserverInstalled = true;
        window.__versionPending = false;
        const check = () => {
            for (const d of document.querySelectorAll('.modal-dialog.msgprint-dialog')) {
                const t = d.querySelector('.modal-title, .modal-header, h4');
                if (t && t.innerText.includes('Version Updated')) {
                    window.__versionPending = true;
                    if (window.__onVersionModal) { try { window.__onVersionModal(); } catch (e) {} }
                    return true;
                }
            }
            return false;
        };
        check();  // catch a modal already present at install time
        const obs = new MutationObserver(check);
        obs.observe(document.documentElement, { childList: true, subtree: true });
    })();
"""


def _version_modal_visible(page) -> bool:
    """Instant JS DOM check for the 'Version Updated' dialog. Matched by
    .modal-dialog.msgprint-dialog + title text so it never collides with the
    upload sub-modal (.modal.fade.show)."""
    try:
        return bool(page.evaluate(
            """(title) => {
                for (const d of document.querySelectorAll('.modal-dialog.msgprint-dialog')) {
                    const t = d.querySelector('.modal-title, .modal-header, h4');
                    if (t && t.innerText.includes(title)) return true;
                }
                return false;
            }""",
            _VERSION_MODAL_TITLE,
        ))
    except Exception:
        return False


def _check_version_refresh(page=None):
    """Checkpoint: raise VersionRefresh if a refresh is pending (observer or GUI
    button set the flag) or — when `page` is given — the modal is on screen now."""
    global _version_refresh_pending, _version_detected_by
    if _version_refresh_pending:
        raise VersionRefresh("refresh requested")
    if page is not None and _version_modal_visible(page):
        _version_refresh_pending = True
        if not _version_detected_by:
            _version_detected_by = "checkpoint"
        raise VersionRefresh("Version Updated modal detected")


def _mark_detected(src: str):
    """Flag a refresh and record which layer caught the modal (for the log)."""
    global _version_refresh_pending, _version_detected_by
    _version_refresh_pending = True
    if not _version_detected_by:
        _version_detected_by = src


def _safe_wait_networkidle(page, timeout=SLOW_TIMEOUT, chunk=3000):
    """Drop-in for page.wait_for_load_state('networkidle', timeout=SLOW_TIMEOUT).
    Same slow-CRM behaviour — waits up to the full timeout — but polls in short
    chunks so a pending Version Updated refresh is acted on within ~chunk ms
    instead of hanging the full timeout. (The modal keeps the network busy, so a
    plain networkidle wait would otherwise stall for the entire SLOW_TIMEOUT.)"""
    global _version_refresh_pending, _version_detected_by
    elapsed = 0
    while elapsed < timeout:
        if _version_refresh_pending:
            raise VersionRefresh("refresh requested during networkidle")
        try:
            page.wait_for_load_state("networkidle", timeout=chunk)
            return
        except Exception:
            if _version_refresh_pending or _version_modal_visible(page):
                _version_refresh_pending = True   # persist so try-wrapped sites still recover
                if not _version_detected_by:
                    _version_detected_by = "networkidle"
                raise VersionRefresh("Version Updated modal during networkidle")
            elapsed += chunk
    # Full timeout elapsed without a modal — networkidle is best-effort, proceed.


def _reload_saved_list(page):
    """Reload the saved filtered list URL in the existing tab (NOT a new tab), then
    wait for the cards. Uses a HARD reload — the Version Updated modal means the app
    bundle changed, and a soft same-URL goto would not pull the new frontend (the
    page stays frozen). page.reload() reloads fresh, like the modal's own Refresh
    button. Clears the refresh flag. Shared by modal recovery + manual button."""
    global _version_refresh_pending, _version_detected_by
    _version_refresh_pending = False   # clear before reload so the observer can re-arm
    why = _version_detected_by or "refresh"
    print(f"   🔄 Version Updated — reloading the saved filtered list… (detected by: {why})")
    print(f"   ↳ URL: {_filtered_list_url}")

    # Step 1: point the tab at the saved list URL (domcontentloaded so goto doesn't
    # block on a frozen 'load' event).
    try:
        page.goto(_filtered_list_url, timeout=SLOW_TIMEOUT, wait_until="domcontentloaded")
    except Exception as e:
        print(f"   ⚠ goto hiccup: {e} — continuing")
    # Step 2: HARD reload to force the updated app bundle (equivalent to F5 /
    # the modal's Refresh). This is what actually clears the frozen state.
    try:
        page.reload(timeout=SLOW_TIMEOUT, wait_until="domcontentloaded")
    except Exception as e:
        print(f"   ⚠ reload hiccup: {e} — continuing")

    # Step 3: wait out the loading overlay — BOUNDED so it can never hang forever.
    # If it still hasn't cleared after a few long waits, hard-reload again.
    print("   ↳ Waiting for the loading overlay to clear…")
    for attempt in range(3):
        try:
            page.wait_for_selector(".freeze-message-container", state="hidden", timeout=SLOW_TIMEOUT)
            break
        except Exception:
            print(f"   ⚠ Overlay still up (attempt {attempt + 1}/3) — hard-reloading again…")
            try:
                page.reload(timeout=SLOW_TIMEOUT, wait_until="domcontentloaded")
            except Exception:
                pass

    # Step 4: wait for the cards to render.
    try:
        page.wait_for_selector("div.card.cursor-pointer.rounded-0", state="visible", timeout=SLOW_TIMEOUT)
    except Exception:
        pass
    _version_refresh_pending = False
    _version_detected_by = ""
    _version_refresh_pending = False


# ═══════════════════════════════════════════════════════════════════
#  UNIT TYPE CATEGORIES  +  SMART IMAGE-PATH FALLBACK (page 2+)
# ═══════════════════════════════════════════════════════════════════
_SMALL_TYPES = [
    "Apartment", "Branded Apartment", "Service Apartment", "Studio", "Penthouse",
    "Duplex", "Triplex", "Quattro", "Loft", "Atelier", "Chalet", "Cabin",
]
_BIG_TYPES = [
    "Standalone", "I Villa", "S Villa", "Duet Villa", "Twinhouse", "Townhouse",
    "Townhouse Corner", "Townhouse Middle", "Building",
]
_OTHER_TYPES = [
    "Land", "Bank", "Office", "Clinic", "Retail", "Commercial", "Showroom",
    "Pharmacy", "Hospital", "School", "Factory", "Storage", "Basement",
    "Food and Beverage",
]


def _norm_type(value: str) -> str:
    return re.sub(r"[\s\-_]+", "", value or "").lower()


_CATEGORY_BY_NORM = {}
for _t in _SMALL_TYPES:
    _CATEGORY_BY_NORM[_norm_type(_t)] = "Small"
for _t in _BIG_TYPES:
    _CATEGORY_BY_NORM[_norm_type(_t)] = "Big"
for _t in _OTHER_TYPES:
    _CATEGORY_BY_NORM[_norm_type(_t)] = "Other"


def category_of(unit_type: str):
    """Return 'Small' / 'Big' / 'Other', or None if the type is not categorized."""
    return _CATEGORY_BY_NORM.get(_norm_type(unit_type))


# Individual image file paths that uploaded successfully, keyed by (project, type).
# Maintained for the whole run and borrowed by the page-2+ fallback. Reset in main().
_successful_uploads = {}


def _record_successful_uploads(project: str, unit_type: str, files: list):
    """Remember the exact files that uploaded OK for a (project, type)."""
    if files:
        _successful_uploads[(project, unit_type)] = list(files)


def choose_fallback_donor(project: str, new_type: str):
    """Pick a random same-category sibling type (same project) that has recorded
    successful image files. Returns (donor_type, [files]) or None.
    Only Small/Big types are eligible — Other/uncategorized never borrow."""
    cat = category_of(new_type)
    if cat not in ("Small", "Big"):
        return None
    candidates = [
        (t, files) for (proj, t), files in _successful_uploads.items()
        if proj == project and files and t != new_type and category_of(t) == cat
    ]
    if not candidates:
        return None
    return random.choice(candidates)


def collect_path_for_new_type(project: str, new_type: str) -> list:
    """Ask the user for an image folder for a NEW per-type entry found on page 2+.
    If a same-category sibling with successful uploads exists, a 2-minute timer
    runs; on expiry it borrows that sibling's successful files. Otherwise it waits
    indefinitely (old behavior)."""
    donor = choose_fallback_donor(project, new_type)

    if donor is None:
        # Old behavior — wait indefinitely for a valid folder.
        while True:
            try:
                files = validate_folder(clean_path(input(f"\n  Folder path for [{project} -> {new_type}]: ")))
                print(f"  ✓ {len(files)} image(s) found")
                return files
            except FileNotFoundError as e:
                print(f"  ✗ {e} — try again")

    donor_type, donor_files = donor

    def announce(secs):
        m, s = divmod(secs, 60)
        return f"   ⏳ No response for {new_type} — using {donor_type} images in {m}:{s:02d}"

    raw = input_with_timeout(
        f"\n  Folder path for [{project} -> {new_type}]  "
        f"(auto-uses {donor_type} images in 2 min): ",
        timeout=120, default=_TIMED_OUT,
        announce=announce, announce_every=30, announce_tail=0, final_msg=False,
    )

    if raw == _TIMED_OUT:
        print(f"   ⏳ No response received — using {donor_type} images for {new_type}")
        return list(donor_files)

    # User engaged — validate, retry indefinitely on a bad path.
    while True:
        try:
            files = validate_folder(clean_path(raw))
            print(f"  ✓ {len(files)} image(s) found")
            return files
        except FileNotFoundError as e:
            print(f"  ✗ {e} — try again")
            raw = input(f"\n  Folder path for [{project} -> {new_type}]: ")


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


def setup_run_logging():
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    log_path = logs_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.txt"
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    original_input = builtins.input

    sys.stdout = Tee(original_stdout, log_file)
    sys.stderr = Tee(original_stderr, log_file)

    def logged_input(prompt: str = ""):
        if prompt:
            original_stdout.write(prompt)
            original_stdout.flush()
            log_file.write(f"PROMPT: {prompt.rstrip()}\n")
        response = original_input("")
        log_file.write(f"RESPONSE: {response}\n")
        return response

    builtins.input = logged_input

    print(f"  Logging to: {log_path}")
    return log_file, original_stdout, original_stderr, original_input, log_path

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
#  DETECT ACTIVE WORKSPACE TAB  (Re-Sale vs Rent vs Primary)
# ═══════════════════════════════════════════════════════════════════
def detect_active_view(page: Page) -> str:
    """Return which workspace tab is active: 'Rent', 'Re-Sale', 'Primary', or 'unknown'.

    Identity anchored on the route href (stable even if labels are translated).
    State signal preference: aria-current="page" → 'exact-active' class → URL path.
    """
    try:
        current_href = page.evaluate(
            """() => {
                const activeLink = document.querySelector('a[aria-current="page"], a.exact-active');
                return (activeLink && activeLink.getAttribute('href')) || location.pathname;
            }"""
        )
    except Exception:
        current_href = page.url

    href = current_href or ""
    if "rent-unit" in href:
        return "Rent"
    if "resale-unit" in href:
        return "Re-Sale"
    if "primary-projects" in href:
        return "Primary"
    return "unknown"

# ═══════════════════════════════════════════════════════════════════
#  SCAN PAGE → UNIQUE UNIT TYPES
# ═══════════════════════════════════════════════════════════════════
def scan_unit_types(page: Page) -> list:
    _safe_wait_networkidle(page)
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
    _safe_wait_networkidle(page)
    page.wait_for_selector("div.card.cursor-pointer.rounded-0", timeout=SLOW_TIMEOUT)
    # Read directly from the card grid to avoid multiline noise (dates, BUA, etc.)
    cards = page.locator("div.card.cursor-pointer.rounded-0")
    total = cards.count()
    projects = {}

    def looks_valid_type(t: str) -> bool:
        return t and len(t) > 2 and not any(c.isdigit() for c in t[:3])

    for i in range(total):
        try:
            card_text = cards.nth(i).locator("h5.card-title").inner_text().strip()
            title = card_text.split("\n")[0].strip()
            if not title:
                title = cards.nth(i).locator("h5.card-title").inner_text().strip()
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

def _print_mapping_summary(mapping: dict):
    """Print the IMAGE MAPPING SUMMARY block. Shared by confirm + fix flow."""
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


def confirm_mapping(mapping: dict) -> bool:
    # Mapping may be per-project (nested) or flat (type->files). Detect shape.
    _print_mapping_summary(mapping)
    while True:
        ans = input_with_timeout("\n  Look good? Start? (y/n): ",
                                 timeout=UPLOAD_PROMPT_TIMEOUT, default="y").strip().lower()
        if ans == "y": return True
        if ans == "n": return False
        print("  Enter y or n.")


# ═══════════════════════════════════════════════════════════════════
#  "ALL GOOD?" FIX FLOW  —  press 'n' to repair paths / behaviour
# ═══════════════════════════════════════════════════════════════════
_SELECT_MARK = "::SELECT::"


def _ask_select(question: str, options: list) -> str:
    """Picker. GUI -> buttons (via _SELECT_MARK encoding). Terminal -> type the
    project name. Returns the chosen option's exact label. Loops until valid."""
    gui = getattr(sys.modules.get('run') or sys.modules.get(__name__),
                  '_GUI_MODE', False)
    if gui:
        payload = f"{question}\n{_SELECT_MARK}" + "||".join(options)
        while True:
            ans = input(payload).strip()
            for o in options:
                if ans.lower() == o.lower():
                    return o
    else:
        print(f"\n  {question}")
        for o in options:
            print(f"    - {o}")
        while True:
            ans = input("  Type the name: ").strip()
            for o in options:
                if ans.lower() == o.lower():
                    return o
            print(f"  Enter one of: {', '.join(options)}")


def _reask_type_path(mapping: dict, proj: str, t: str):
    """Re-collect the folder for one [proj -> type]. Loops on bad path."""
    while True:
        try:
            files = validate_folder(clean_path(
                input(f"\n  New folder path for [{proj} -> {t}]: ")))
            print(f"  ✓ {len(files)} image(s) found")
            mapping[proj][t] = files
            return
        except FileNotFoundError as e:
            print(f"  ✗ {e} — try again")


def _reask_same_for_all(mapping: dict, proj: str):
    """Re-collect the single global folder for a same-for-all project."""
    while True:
        try:
            files = validate_folder(clean_path(
                input(f"\n  New folder path for project '{proj}' (all types): ")))
            print(f"  ✓ {len(files)} image(s) found")
            mapping[proj]['_images'] = files
            mapping[proj]['_same_for_all'] = True
            return
        except FileNotFoundError as e:
            print(f"  ✗ {e} — try again")


def _fix_one_project(mapping, states, proj, is_new, scanned_types,
                     new_types_this_page):
    """Repair one project. New project -> behaviour-or-path. Old project ->
    path only, for the types newly scanned on the current page."""
    pdata = mapping.setdefault(proj, {"_same_for_all": False})
    if is_new:
        kind = _ask_select(
            f"[{proj}] Image upload behaviour issue or path issue?",
            ["Behaviour issue", "Path issue"])
        if kind == "Behaviour issue":
            # Re-run same/different + re-collect everything for this project.
            sub = collect_image_mapping_per_project({proj: scanned_types})
            mapping[proj] = sub[proj]
            states[proj] = UploadErrorState(
                same_images_mode=mapping[proj].get('_same_for_all', False))
            return
        # Path issue.
        if pdata.get('_same_for_all'):
            _reask_same_for_all(mapping, proj)
        else:
            for t in [t for t in pdata if not t.startswith('_')]:
                _reask_type_path(mapping, proj, t)
    else:
        # Old project: path issue only, re-ask just the newly found units.
        targets = new_types_this_page or [t for t in pdata if not t.startswith('_')]
        for t in targets:
            _reask_type_path(mapping, proj, t)


def _resolve_all_good(mapping, states, scanned_types,
                      new_projects, new_types_by_proj):
    """Show the mapping summary and ask 'All good?'. On 'n', let the user pick a
    project and repair it, then re-summarize and re-ask. Loops forever until the
    user confirms 'y' (or the 2-minute timer expires -> auto 'y'). Never aborts.

    Picker visibility = projects changed on this event:
      page 1  -> every project (all first-scan)
      page 2+ -> new projects + old projects that gained new types this page
    (Old same-for-all projects gain no new types -> never listed -> invisible.)
    """
    visible = list(dict.fromkeys(
        list(new_projects) + list(new_types_by_proj.keys())))
    while True:
        _print_mapping_summary(mapping)
        ans = input_with_timeout("\n  All good? (y/n): ",
                                 timeout=UPLOAD_PROMPT_TIMEOUT,
                                 default="y").strip().lower()
        if ans == "y":
            return
        if ans != "n":
            print("  Enter y or n.")
            continue
        if not visible:
            print("  Nothing on this page to fix — continuing.")
            return
        proj = (visible[0] if len(visible) == 1
                else _ask_select("Which project has the problem?", visible))
        _fix_one_project(
            mapping, states, proj,
            is_new=(proj in new_projects),
            scanned_types=scanned_types.get(proj, []),
            new_types_this_page=new_types_by_proj.get(proj, []))
        # loop -> re-summarize -> re-ask "All good?"

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
            # Per-type project on page 2+: allow a same-category timed fallback.
            pdata[t] = collect_path_for_new_type(proj, t)
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
        ans = input_with_timeout("\n  All good? Continue? (y/n): ",
                                 timeout=UPLOAD_PROMPT_TIMEOUT, default="y").strip().lower()
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
    _safe_wait_networkidle(page)
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
                // Bail instantly if the Version Updated modal is up (set by observer).
                if (window.__versionPending) throw new Error('VERSION_UPDATED_MODAL');
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
    except VersionRefresh:
        raise
    except Exception as e:
        # The upload JS throws VERSION_UPDATED_MODAL if the modal appears mid-wait.
        if 'VERSION_UPDATED_MODAL' in str(e) or _version_refresh_pending or _version_modal_visible(page):
            _mark_detected("upload")
            raise VersionRefresh("Version Updated modal during upload wait")
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

            fail_pct = n_failed / len(paths) * 100 if paths else 100.0
            print(f"\n   ⚠ {n_failed}/{len(paths)} image(s) failed to upload ({fail_pct:.0f}%):")
            for fp in failed_paths:
                print(f"      → {fp}")

            # ── 100% failed → no images uploaded, skip the whole unit ──
            if n_success == 0:
                print("   ⏭ All images failed — skipping this unit.")
                _dismiss_upload_modals(page)
                raise UnitSkipped(f"all {n_failed} image(s) failed to upload")

            # ── >25% failed → too many bad images, skip unit and log paths ──
            if fail_pct > UPLOAD_FAIL_SKIP_PCT:
                print(f"   ⏭ {fail_pct:.0f}% failed (> {UPLOAD_FAIL_SKIP_PCT:.0f}%) — skipping this unit.")
                _dismiss_upload_modals(page)
                raise UnitSkipped(
                    f"{n_failed} image(s) failed to upload: " + "; ".join(failed_paths)
                )

            # ── ≤25% failed → ask, but auto-continue (as 'y') after timeout ──
            if state.should_ask_user(unit_type):
                ans = input_with_timeout(
                    f"\n   ❓ Continue with {n_success} image(s) instead of {len(paths)}? (y/n): ",
                    timeout=UPLOAD_PROMPT_TIMEOUT, default="y",
                ).strip().lower()

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
                    _record_successful_uploads(project, unit_type, successful_paths)
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
                _record_successful_uploads(project, unit_type, successful_paths)
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
            _record_successful_uploads(project, unit_type, list(paths))
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
    is_rent = (CURRENT_VIEW == "Rent")

    # ── Open modal ─────────────────────────────────────────────────
    print("   ↳ Opening Publish Unit (Clients) modal...")
    page.locator("button", has_text="Publish Unit (Clients)").first.click(timeout=SLOW_TIMEOUT)
    page.locator(MODAL).wait_for(state="visible", timeout=SLOW_TIMEOUT)
    page.wait_for_selector("text=Price Display", timeout=SLOW_TIMEOUT)
    print("   ✓ Modal open")

    # ── Price display (Fields tab is default) ──────────────────────
    # Rent units have no selling-price / down-payment decision, so skip the
    # whole price-display logic and go straight to image selection.
    if is_rent:
        print("   ↳ Rent unit — skipping price/down-payment logic")
    else:
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
    # Bail before any work if a refresh is pending (modal / GUI button).
    _check_version_refresh(page)

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
        _safe_wait_networkidle(page)

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

    # Rent and Re-Sale share the same flow; only the detail-link route differs.
    slug = "rent-unit" if CURRENT_VIEW == "Rent" else "resale-unit"

    links = page.locator(f"a[href*='/{slug}/'], a[href*='realestate-workspace/{slug}/']").all()
    hrefs = list(dict.fromkeys(
        a.get_attribute("href") for a in links if a.get_attribute("href")
    ))

    hrefs = [h for h in hrefs if h and f"realestate-workspace/{slug}?" not in h and not h.endswith(f"/{slug}")]

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
            except UnitSkipped as e:
                print(f"  ⏭ Skipped: {e}")
                results.append({"page": page_num, "unit": name, "url": url, "status": f"FAILED: {e}"})
                try:
                    go_back_to_list(page)
                except Exception as ge:
                    print(f"   ⚠ Could not return to list cleanly: {ge}")
            except VersionRefresh:
                raise   # bubble to main() → reload saved list, replay from card 1
            except Exception as e:
                # A modal that slipped past the checkpoints surfaces as a generic
                # error — convert to a refresh instead of stalling on the prompt.
                if _version_refresh_pending or _version_modal_visible(page):
                    raise VersionRefresh("Version Updated during unit")
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
            name = card_el.locator("h5.card-title").inner_text().strip()
        except Exception:
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
                _safe_wait_networkidle(page)

            card_el.scroll_into_view_if_needed()
            print(f"   [DEBUG] Click executing...")
            card_el.click(timeout=SLOW_TIMEOUT)
            print(f"   [DEBUG] Click executed - waiting for navigation...")
            _safe_wait_networkidle(page)

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
        except UnitSkipped as e:
            print(f"  ⏭ Skipped: {e}")
            results.append({"page": page_num, "unit": name, "url": page.url, "status": f"FAILED: {e}"})
            try:
                go_back_to_list(page)
            except Exception as ge:
                print(f"   ⚠ Could not return to list cleanly: {ge}")
        except VersionRefresh:
            raise   # bubble to main() → reload saved list, replay from card 1
        except Exception as e:
            # A modal that slipped past the checkpoints surfaces as a generic
            # error — convert to a refresh instead of stalling on the prompt.
            if _version_refresh_pending or _version_modal_visible(page):
                raise VersionRefresh("Version Updated during unit")
            print(f"  ✗ ERROR: {e}")
            results.append({"page": page_num, "unit": name, "url": page.url, "status": f"FAILED: {e}"})
            input("  ⚠ Fix manually if needed, then press Enter to continue… ")

# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════
def main():
    setup_run_logging()
    _successful_uploads.clear()
    print("\n" + "═"*50)
    print("  EGY Property Automation")
    print("═"*50)

    print("\n" + "─"*50)
    print("  PLEASE NOTE")
    print("─"*50)
    print(
        "  • When asked “Same images for all types?” — clicking Yes asks\n"
        "    you for one folder and uses that same set of images for every\n"
        "    unit type. Clicking No asks you for a separate folder per unit\n"
        "    type (per project) and uses each set only for that type. When\n"
        "    the program moves to the next page it scans for new types per\n"
        "    project, and if a new type is found it asks you for a new path\n"
        "    for it."
    )
    print(
        "  • Sometimes the CRM fails to read some images, causing an error\n"
        "    on its side. The program only checks the first upload per type\n"
        "    — if that first upload passes, no further errors will occur for\n"
        "    that type. If an error does occur, you are asked whether to\n"
        "    continue without the failed image(s): clicking Yes omits the\n"
        "    faulty image(s); clicking No shows you the path of each faulty\n"
        "    image so you can find and delete it, then asks you for a new\n"
        "    path for that project. This can also happen when you choose\n"
        "    “same images for all.”"
    )
    print(
        "  • Units are grouped by project, then by type. After each scan the\n"
        "    program shows an image-mapping summary and asks “All good?”. If\n"
        "    you click Yes (or do nothing for 2 minutes) it starts uploading.\n"
        "    If you click No it asks which project has the problem: for a\n"
        "    newly scanned project you choose whether it is a behaviour issue\n"
        "    (same-for-all vs. per-type) or a path issue; for a project seen\n"
        "    on an earlier page it only asks for new paths for the types just\n"
        "    found on this page. After any fix it re-shows the summary and\n"
        "    asks “All good?” again, looping until you confirm."
    )
    print(
        "  • Unit types are sorted into three categories: Small (apartments,\n"
        "    studios, duplexes, chalets, …), Big (villas, townhouses,\n"
        "    standalones, buildings, …) and Other (land, offices, retail,\n"
        "    clinics, …). When a new per-type unit appears on a later page and\n"
        "    you don’t give it a folder within 2 minutes, the program borrows\n"
        "    the images from another already-uploaded type in the SAME\n"
        "    category and project (e.g. a new Duplex can reuse Apartment\n"
        "    images, both Small). Other/uncategorized types never borrow — you\n"
        "    must always provide their folder."
    )
    print("─"*50)

    print("\n  Launching Chrome…")
    if not launch_chrome():
        print("\n❌  Could not start Chrome.")
        print("    Make sure Google Chrome is installed, then run again.\n")
        input("  Press Enter to exit… ")
        return

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception:
            print("\n❌  Could not connect to Chrome.")
            print("    Close ALL Chrome windows and try again.\n")
            input("  Press Enter to exit… ")
            return

        ctx  = browser.contexts[0]
        page = ctx.pages[0]
        print("\n  → Navigate to the filtered unit list in Chrome")
        print("  → Set your filters and Available checkbox")
        input("  → Press Enter when ready… \n")

        # Show the connection only after the user has navigated.
        print(f"\n✅  Connected  |  {page.url}")

        # Install the Version Updated detector. The MutationObserver fires the
        # instant the modal is added to the DOM and calls this exposed fn, which
        # sets the flag (~1ms). add_init_script re-installs the observer on every
        # navigation; evaluate runs it once on the page already loaded. Wrapped in
        # try/except so a missing API or a re-register never aborts the run.
        global _version_refresh_pending
        _version_refresh_pending = False

        def _on_version_modal():
            global _version_refresh_pending, _version_detected_by
            _version_refresh_pending = True
            _version_detected_by = "observer"

        try:
            page.expose_function("__onVersionModal", _on_version_modal)
        except Exception as _e:
            print(f"   ℹ Version-modal callback already registered ({_e})")
        try:
            page.add_init_script(_VERSION_OBSERVER_JS)
        except Exception as _e:
            print(f"   ℹ add_init_script unavailable ({_e}) — relying on post-wait checks")
        try:
            page.evaluate(_VERSION_OBSERVER_JS)   # arm on the current page too
        except Exception:
            pass

        # Detect which tab is active BEFORE scanning. Rent reuses the whole flow
        # but skips price/down-payment logic. Only Rent and Re-Sale are valid —
        # Primary/unknown means the user is on the wrong tab, so block with a
        # retry prompt until they switch to a supported tab.
        global CURRENT_VIEW
        CURRENT_VIEW = detect_active_view(page)
        while CURRENT_VIEW not in ("Rent", "Re-Sale"):
            print(f"  ⚠ Active view: {CURRENT_VIEW} — only Rent or Re-Sale are supported.")
            input("  Switch to the Rent or Re-Sale tab in Chrome, then click Retry to re-check… ")
            CURRENT_VIEW = detect_active_view(page)

        if CURRENT_VIEW == "Rent":
            print("  Active view: Rent  —  price/down-payment logic will be skipped")
        else:
            print("  Active view: Re-Sale")

        # Tab confirmed — capture the list URL and force non_published_units=1 and
        # page_length=100 (everything else in the link untouched). Save it; if
        # anything changed, reload once and wait out the loading overlay.
        global _filtered_list_url
        normalized, msgs = _normalize_list_url(page.url)
        _filtered_list_url = normalized
        if msgs:
            for _m in msgs:
                print(f"   ⚙ {_m}")
            print(f"   🔗 Updated list link:  {_filtered_list_url}")
            try:
                page.goto(_filtered_list_url, timeout=SLOW_TIMEOUT)
            except Exception as e:
                print(f"   ⚠ Reload hiccup: {e} — continuing")
            try:
                _safe_wait_networkidle(page)
            except Exception:
                pass
            print("   ⏳ Waiting for the loading overlay to clear…")
            while True:
                try:
                    page.wait_for_selector(".freeze-message-container", state="hidden", timeout=SLOW_TIMEOUT)
                    break
                except Exception:
                    continue
            try:
                page.wait_for_selector("div.card.cursor-pointer.rounded-0", state="visible", timeout=SLOW_TIMEOUT)
            except Exception:
                pass
            print("   ✓ List reloaded")

        # URL captured & saved — let the GUI reveal its 'Refresh Page' button.
        if callable(_notify_url_saved):
            try:
                _notify_url_saved()
            except Exception:
                pass

        print("  Scanning page for projects and unit types…")
        # Initial scan is the spot the modal was seen first — retry it through a
        # reload if Version Updated pops up here.
        while True:
            try:
                project_types = scan_projects_types(page)
                break
            except VersionRefresh:
                _reload_saved_list(page)
                print("   ↳ Rescanning after refresh…")
                continue
        if not project_types:
            print("  ✗ No unit projects/types found. Are you on the list page?")
            return

        mapping = collect_image_mapping_per_project(project_types)

        # Per-project upload error state tracker
        states = { proj: UploadErrorState(same_images_mode=mapping[proj].get('_same_for_all', False)) for proj in mapping }

        # "All good?" — on 'n' the user repairs paths/behaviour, then re-confirms.
        # Page 1: every project is first-scan, so all are fixable. Never aborts.
        _resolve_all_good(mapping, states, project_types,
                          new_projects=set(project_types.keys()),
                          new_types_by_proj={})
        print(f"   Projects configured: {', '.join(mapping.keys())}")

        results  = []
        page_num = 1
        abort = False

        while True:
            # Version Updated modal anywhere inside → VersionRefresh bubbles here.
            # Reload the saved list (published units drop off via non_published=1),
            # then replay this page from card 1. page_num kept for log continuity.
            try:
                process_current_page(page, mapping, page_num, results, states)
            except VersionRefresh:
                _reload_saved_list(page)
                new_count = page.locator("div.card.cursor-pointer.rounded-0").count()
                print(f"   ✓ List reloaded  —  {new_count} unit(s) remaining")
                print(f"   ↳ Resuming page {page_num}, card 1 of {new_count}")
                continue

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
            # Automatic pagination: read visible pagination controls
            try:
                cur_el = page.locator("input[type='number']").last
                total_el = page.locator("span.text-nowrap").last
                cur_str = cur_el.input_value().strip()
                cur = int(cur_str) if cur_str else 1
                total_text = total_el.inner_text() or ""
                m = re.search(r"\d+", total_text)
                total = int(m.group()) if m else cur
                print(f"  Pagination: {cur}/{total}")
            except Exception as e:
                print(f"   ⚠ Could not read pagination: {e} — falling back to manual advance")
                ans = input("\n  > ").strip().lower()
                if ans == "done":
                    break
                page_num += 1
                _safe_wait_networkidle(page)
                continue

            # If we're on the last page, finish and exit the loop
            if cur >= total:
                print("  ↳ Reached last page — finishing")
                break

            # Otherwise click Next and wait for the new page to render
            next_btn = page.locator("button.btn-icon:has(i.fa-angle-right)").last
            try:
                if not next_btn.is_enabled() or next_btn.get_attribute("disabled"):
                    print("   ↳ Next button disabled — treating as last page")
                    break
            except Exception:
                # If attribute checks fail, attempt click anyway
                pass

            print("  ↳ Clicking Next page…")
            try:
                next_btn.click(timeout=SLOW_TIMEOUT)
            except Exception as e:
                print(f"   ⚠ Next click failed: {e} — stopping")
                break

            # Wait for the URL to reflect the next page before scanning.
            # This is more reliable than waiting for the page-number input alone.
            try:
                next_page = cur + 1
                page.wait_for_url(lambda url: f"page={next_page}" in url, timeout=SLOW_TIMEOUT)
            except Exception:
                pass

            # Layered waits to ensure the new page has rendered
            try:
                _safe_wait_networkidle(page)
            except Exception:
                pass
            try:
                page.wait_for_selector("div.card.cursor-pointer.rounded-0", state="visible", timeout=SLOW_TIMEOUT)
            except Exception:
                pass

            # Wait until the visible page-number input has changed value.
            # If this times out, the URL check above already told us the page changed,
            # so we can still proceed safely.
            try:
                prev = str(cur)
                page.wait_for_function(
                    f"""() => {{
                        const els = document.querySelectorAll('input[type=\'number\']');
                        if (!els || els.length === 0) return false;
                        const v = els[els.length - 1].value;
                        return v !== '{prev}' && v !== '';
                    }}""",
                    timeout=SLOW_TIMEOUT,
                )
            except Exception:
                print("   ⚠ Timed out waiting for page number to change — proceeding anyway")

            # Update page_num to the new current value if possible
            try:
                page_num = int(page.locator("input[type='number']").last.input_value().strip())
            except Exception:
                page_num = cur + 1

            # Wait for the freeze overlay to disappear (loading screen overlay).
            # This ensures the cards are truly interactive and not obscured by loading state.
            try:
                page.wait_for_selector(".freeze-message-container", state="hidden", timeout=SLOW_TIMEOUT)
            except Exception:
                pass

            # CRM bug: before the last page, clicking Next loads an empty page.
            # Fix: click Back, wait for cards — the CRM renders the remaining units
            # on the previous page. No second Next needed; just scan what's there.
            if page.locator("div.card.cursor-pointer.rounded-0").count() == 0:
                print("  ⚠ Page appears empty (CRM bug) — clicking Back to reload…")
                try:
                    back_btn = page.locator("button.btn-icon:has(i.fa-angle-left)").last
                    back_btn.click(timeout=SLOW_TIMEOUT)
                    try:
                        page.wait_for_selector(".freeze-message-container", state="hidden", timeout=SLOW_TIMEOUT)
                    except Exception:
                        pass
                    _safe_wait_networkidle(page)
                    page.wait_for_selector("div.card.cursor-pointer.rounded-0", state="visible", timeout=SLOW_TIMEOUT)
                    try:
                        page_num = int(page.locator("input[type='number']").last.input_value().strip())
                    except Exception:
                        page_num = cur
                    print(f"  ✓ Back done — page {page_num} loaded with cards. Scanning…")
                except Exception as e:
                    print(f"  ⚠ Back reload failed: {e}")

            # ═══════════════════════════════════════════════════════════════
            #  PAGE FULLY LOADED — NOW SCAN FOR PROJECTS/TYPES
            # ═══════════════════════════════════════════════════════════════
            print(f"\n  ↳ Page {page_num} fully loaded. Scanning for projects/types…")
            current_projects = scan_projects_types(page)
            if current_projects:
                # Collect this page's changes, then run ONE "All good?" for the page.
                page_new_projects = set()   # projects first seen on this page
                page_new_types    = {}      # old project -> [types new this page]
                for proj, types in current_projects.items():
                    if proj not in mapping:
                        print(f"\n  New project found: {proj} — requesting image folders")
                        new_map = collect_image_mapping_per_project({proj: types})
                        mapping.update(new_map)
                        states[proj] = UploadErrorState(same_images_mode=mapping[proj].get('_same_for_all', False))
                        page_new_projects.add(proj)
                    else:
                        if mapping[proj].get('_same_for_all'):
                            print(f"\n  Skipping per-type checks for project '{proj}' — _same_for_all=True")
                            # nothing to do for this project
                            continue
                        new_types = [t for t in types if t not in mapping[proj]]
                        if not new_types:
                            print(f"\n  No new types for project '{proj}' — nothing to prompt")
                        else:
                            # Collect paths for the new types (keeps the timed-donor fallback).
                            for t in new_types:
                                mapping[proj][t] = collect_path_for_new_type(proj, t)
                            page_new_types[proj] = new_types

                # After scanning + collecting, show what's on the page.
                print(f"\n  Scanned projects on page {page_num}:")
                for p, ts in current_projects.items():
                    print(f"    {p}: {', '.join(ts)}")

                # "All good?" only when something was newly scanned this page.
                # On 'n': new project -> behaviour-or-path, old project -> re-ask
                # the newly found units' paths only. Never aborts; loops til 'y'.
                if page_new_projects or page_new_types:
                    _resolve_all_good(mapping, states, current_projects,
                                      new_projects=page_new_projects,
                                      new_types_by_proj=page_new_types)

                print(f"  ↳ Proceeding to process page {page_num}…")

            if abort:
                break

        # Store results for GUI export; write CSV only when running standalone
        _mod = sys.modules.get('run') or sys.modules.get(__name__)
        if _mod is not None:
            _mod._pending_results = list(results)

        if not getattr(_mod, '_GUI_MODE', False):
            with open("results.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["page", "unit", "url", "status"])
                w.writeheader()
                w.writerows(results)
            print(f"  📄  results.csv saved")

        total_ok   = sum(1 for r in results if r["status"] == "OK")
        total_fail = len(results) - total_ok
        print(f"\n{'═'*50}")
        print(f"  ✅  All done!  {total_ok} OK  |  {total_fail} failed")
        print(f"{'═'*50}\n")

if __name__ == "__main__":
    main()