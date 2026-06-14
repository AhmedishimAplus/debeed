"""
gui.py — Debeed UI
Entry point for EGY Property automation with a professional Tkinter interface.
Run this file instead of run.py.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import queue
import builtins
import sys
import re
import io
import csv as _csv_mod
import shutil
from pathlib import Path
from datetime import datetime

# ── Module-level log state ─────────────────────────────────────────
_log_file = None
_log_path = None

def _gui_setup_logging():
    global _log_file, _log_path
    Path("logs").mkdir(exist_ok=True)
    _log_path = Path("logs") / f"run_{datetime.now():%Y%m%d_%H%M%S}.txt"
    _log_file = open(_log_path, "a", encoding="utf-8", buffering=1)
    print(f"  Logging to: {_log_path}")

import run as _run
_run.setup_run_logging = _gui_setup_logging
_run._GUI_MODE        = True
_run._pending_results = None

# ── Colour palette ─────────────────────────────────────────────────
BG_MAIN    = "#0d1b2a"
BG_PANEL   = "#122235"
BG_ACTION  = "#162b40"
BORDER     = "#1e3a55"

FG_TEXT    = "#d8eaf5"
FG_MUTED   = "#4d6e88"
FG_GOLD    = "#c9a84c"
FG_SUCCESS = "#3ddc84"
FG_ERROR   = "#ff5c5c"
FG_WARNING = "#ffb347"
FG_INFO    = "#64b5f6"
FG_STEP    = "#80cbc4"

BTN_YES_BG  = "#0b3320"
BTN_YES_FG  = "#a5d6a7"
BTN_NO_BG   = "#4a0e0e"
BTN_NO_FG   = "#ffaaaa"
BTN_GOLD_BG = FG_GOLD
BTN_GOLD_FG = "#111111"
BTN_BLUE_BG = "#1a3a5c"
BTN_BLUE_FG = "#90caf9"
BTN_CONT_BG = "#0b3320"
BTN_CONT_FG = "#b9f6ca"


class DebeedApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Debeed  —  EGY Property Unit Publisher")
        self.root.geometry("980x740")
        self.root.minsize(820, 580)
        self.root.configure(bg=BG_MAIN)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Queues — automation thread <-> GUI main thread
        self._log_q      = queue.Queue()
        self._input_q    = queue.Queue()
        self._response_q = queue.Queue()
        self._wait_evt   = threading.Event()

        # Runtime state
        self._started              = False
        self._pending_folder       = ""
        self._pending_folder_valid = False
        self._log_cleaned          = False

        self._build_ui()
        self._patch_io()
        self._poll()
        self.root.after(300, self._ensure_chrome_path)

    # ═══════════════════════════════════════════════════════════════
    #  UI CONSTRUCTION
    # ═══════════════════════════════════════════════════════════════

    def _build_ui(self):
        # ── Header bar ──────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=BG_PANEL, height=58)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)

        logo_frame = tk.Frame(hdr, bg=BG_PANEL)
        logo_frame.pack(side=tk.LEFT, padx=22, pady=0, fill=tk.Y)

        tk.Label(logo_frame, text="EGY PROPERTY",
                 bg=BG_PANEL, fg=FG_GOLD,
                 font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT, pady=16)
        tk.Label(logo_frame, text="  ›  Unit Publisher",
                 bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 11)).pack(side=tk.LEFT)

        self._status_var = tk.StringVar(value="Ready  —  Launch Chrome, then click Start")
        tk.Label(hdr, textvariable=self._status_var,
                 bg=BG_PANEL, fg=FG_MUTED,
                 font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=20)

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill=tk.X)

        # ── Gold progress bar ────────────────────────────────────────
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Gold.Horizontal.TProgressbar",
                        background=FG_GOLD, troughcolor="#080f18",
                        borderwidth=0, lightcolor=FG_GOLD, darkcolor=FG_GOLD)
        self._prog = ttk.Progressbar(self.root,
                                     style="Gold.Horizontal.TProgressbar",
                                     mode="determinate", maximum=100, value=0)
        self._prog.pack(fill=tk.X)

        # ── Log section ─────────────────────────────────────────────
        outer = tk.Frame(self.root, bg=BG_MAIN)
        outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=(10, 6))

        lbl_row = tk.Frame(outer, bg=BG_MAIN)
        lbl_row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(lbl_row, text="ACTIVITY LOG",
                 bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 7, "bold")).pack(side=tk.LEFT)
        self._unit_counter = tk.StringVar(value="")
        tk.Label(lbl_row, textvariable=self._unit_counter,
                 bg=BG_MAIN, fg=FG_MUTED,
                 font=("Segoe UI", 7)).pack(side=tk.RIGHT)

        border_frame = tk.Frame(outer, bg=BORDER, padx=1, pady=1)
        border_frame.pack(fill=tk.BOTH, expand=True)

        self._log = tk.Text(
            border_frame,
            bg=BG_PANEL, fg=FG_TEXT,
            font=("Consolas", 9),
            relief=tk.FLAT, wrap=tk.WORD,
            state=tk.DISABLED,
            selectbackground="#1e3a55",
            padx=12, pady=8,
            cursor="arrow",
        )
        sb = tk.Scrollbar(border_frame, command=self._log.yview,
                          bg=BG_PANEL, troughcolor=BG_MAIN,
                          width=8, relief=tk.FLAT, bd=0)
        self._log.config(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._log.tag_config("ts",      foreground="#2a4560", font=("Consolas", 8))
        self._log.tag_config("success", foreground=FG_SUCCESS)
        self._log.tag_config("error",   foreground=FG_ERROR)
        self._log.tag_config("warning", foreground=FG_WARNING)
        self._log.tag_config("info",    foreground=FG_INFO)
        self._log.tag_config("step",    foreground=FG_STEP,   font=("Consolas", 9, "bold"))
        self._log.tag_config("unit",    foreground=FG_GOLD,   font=("Consolas", 9, "bold"))
        self._log.tag_config("muted",   foreground=FG_MUTED,  font=("Consolas", 8))
        self._log.tag_config("normal",  foreground=FG_TEXT)
        self._log.tag_config("done",    foreground=FG_SUCCESS, font=("Consolas", 9, "bold"))

        # ── Action panel ─────────────────────────────────────────────
        self._action = tk.Frame(self.root, bg=BG_ACTION,
                                highlightthickness=1,
                                highlightbackground=BORDER)
        self._action.pack(fill=tk.X, padx=14, pady=(0, 4))
        self._panel_start()

        # ── Bottom button bar ─────────────────────────────────────────
        self._build_btn_bar()

    def _build_btn_bar(self):
        bar = tk.Frame(self.root, bg=BG_MAIN)
        bar.pack(fill=tk.X, padx=14, pady=(0, 10))

        # Left group — log save always visible; results revealed on completion
        self._btn_left = tk.Frame(bar, bg=BG_MAIN)
        self._btn_left.pack(side=tk.LEFT)

        tk.Button(self._btn_left,
                  text="📥  Save Log",
                  bg=BTN_BLUE_BG, fg=BTN_BLUE_FG,
                  font=("Segoe UI", 9, "bold"),
                  relief=tk.FLAT, padx=12, pady=5,
                  cursor="hand2",
                  command=self._save_log,
                  ).pack(side=tk.LEFT)

        # Created but not packed — shown only after completion
        self._btn_results = tk.Button(self._btn_left,
                                      text="📊  Save Results",
                                      bg=BTN_CONT_BG, fg=BTN_CONT_FG,
                                      font=("Segoe UI", 9, "bold"),
                                      relief=tk.FLAT, padx=12, pady=5,
                                      cursor="hand2",
                                      command=self._save_results)

        # Right — always visible quit
        tk.Button(bar,
                  text="✕  Quit",
                  bg=BTN_NO_BG, fg=BTN_NO_FG,
                  font=("Segoe UI", 9, "bold"),
                  relief=tk.FLAT, padx=12, pady=5,
                  cursor="hand2",
                  command=self._quit,
                  ).pack(side=tk.RIGHT)

    # ═══════════════════════════════════════════════════════════════
    #  ACTION PANELS
    # ═══════════════════════════════════════════════════════════════

    def _clear(self):
        for w in self._action.winfo_children():
            w.destroy()

    def _panel_start(self):
        self._clear()
        f = tk.Frame(self._action, bg=BG_ACTION)
        f.pack(fill=tk.X, padx=20, pady=14)

        import platform as _plat
        _step1 = (
            "1.  Click  Start  below — Chrome will open automatically\n"
            if _plat.system() == "Darwin" else
            "1.  Open  launch_chrome.bat  and wait for Chrome to launch\n"
        )
        tk.Label(f,
                 text=_step1
                      + "2.  Log in to the CRM and navigate to your filtered unit list\n"
                        "3.  Apply filters, tick  Available,  then click  Start  below",
                 bg=BG_ACTION, fg=FG_TEXT,
                 font=("Segoe UI", 10), justify=tk.LEFT,
                 ).pack(side=tk.LEFT, padx=(0, 24))

        tk.Button(f, text="▶  Start",
                  bg=BTN_GOLD_BG, fg=BTN_GOLD_FG,
                  font=("Segoe UI", 12, "bold"),
                  relief=tk.FLAT, padx=28, pady=10,
                  cursor="hand2",
                  command=self._start,
                  ).pack(side=tk.RIGHT)

    def _panel_running(self, message="Automation running  —  waiting for next action…"):
        self._clear()
        f = tk.Frame(self._action, bg=BG_ACTION)
        f.pack(fill=tk.X, padx=20, pady=12)
        tk.Label(f, text=f"●  {message}",
                 bg=BG_ACTION, fg=FG_SUCCESS,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT)

    def _panel_yes_no(self, prompt: str):
        self._clear()
        f = tk.Frame(self._action, bg=BG_ACTION)
        f.pack(fill=tk.X, padx=20, pady=14)

        clean = re.sub(r"\(y/n\):?", "", prompt, flags=re.IGNORECASE).strip()
        clean = clean.strip().rstrip(":").strip()
        if not clean.endswith("?"):
            clean += "?"

        pl = prompt.lower()
        if "same images" in pl:
            yes_label = "✓  Yes, same images for all"
            no_label  = "✗  No, different images per type"
        elif "look good" in pl or "start?" in pl:
            yes_label = "✓  Yes, start processing"
            no_label  = "✗  No, cancel"
        elif "continue with" in pl and "image" in pl:
            yes_label = "✓  Yes, continue without failed images"
            no_label  = "✗  No, I'll provide a new folder"
        elif "all good" in pl or "continue?" in pl:
            yes_label = "✓  Yes, continue"
            no_label  = "✗  No, cancel"
        else:
            yes_label = "✓  Yes"
            no_label  = "✗  No"

        tk.Label(f, text=clean,
                 bg=BG_ACTION, fg=FG_TEXT,
                 font=("Segoe UI", 11),
                 wraplength=580, justify=tk.LEFT,
                 ).pack(side=tk.LEFT, padx=(0, 20), anchor="w")

        btns = tk.Frame(f, bg=BG_ACTION)
        btns.pack(side=tk.RIGHT)

        tk.Button(btns, text=no_label,
                  bg=BTN_NO_BG, fg=BTN_NO_FG,
                  font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=14, pady=8,
                  cursor="hand2",
                  command=lambda: self._respond("n"),
                  ).pack(side=tk.RIGHT, padx=(8, 0))

        tk.Button(btns, text=yes_label,
                  bg=BTN_YES_BG, fg=BTN_YES_FG,
                  font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=14, pady=8,
                  cursor="hand2",
                  command=lambda: self._respond("y"),
                  ).pack(side=tk.RIGHT, padx=(8, 0))

    def _panel_folder(self, prompt: str):
        self._clear()
        self._pending_folder       = ""
        self._pending_folder_valid = False

        f = tk.Frame(self._action, bg=BG_ACTION)
        f.pack(fill=tk.X, padx=20, pady=14)

        clean = prompt.strip().rstrip(":").strip()
        tk.Label(f, text=clean,
                 bg=BG_ACTION, fg=FG_TEXT,
                 font=("Segoe UI", 11),
                 ).pack(anchor="w", pady=(0, 10))

        row1 = tk.Frame(f, bg=BG_ACTION)
        row1.pack(fill=tk.X)

        path_var   = tk.StringVar(value="No folder selected")
        status_var = tk.StringVar(value="")
        _confirm   = []

        def browse():
            folder = filedialog.askdirectory(title="Select Image Folder", mustexist=True)
            if not folder:
                return
            self._pending_folder = folder
            path_var.set(folder)

            imgs = [x for x in Path(folder).iterdir()
                    if x.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]

            if not imgs:
                status_var.set("✗  No images found — choose a different folder")
                status_lbl.config(fg=FG_ERROR)
                _confirm[0].config(state=tk.DISABLED)
                self._pending_folder_valid = False
            else:
                n = len(imgs)
                status_var.set(f"✓  {n} image{'s' if n > 1 else ''} found")
                status_lbl.config(fg=FG_SUCCESS)
                _confirm[0].config(state=tk.NORMAL)
                self._pending_folder_valid = True

        tk.Button(row1, text="📁  Browse Folder",
                  bg=BTN_BLUE_BG, fg=BTN_BLUE_FG,
                  font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=14, pady=7,
                  cursor="hand2",
                  command=browse,
                  ).pack(side=tk.LEFT)

        tk.Label(row1, textvariable=path_var,
                 bg=BG_ACTION, fg=FG_MUTED,
                 font=("Consolas", 9),
                 ).pack(side=tk.LEFT, padx=12, fill=tk.X, expand=True)

        row2 = tk.Frame(f, bg=BG_ACTION)
        row2.pack(fill=tk.X, pady=(8, 0))

        status_lbl = tk.Label(row2, textvariable=status_var,
                               bg=BG_ACTION, fg=FG_MUTED,
                               font=("Segoe UI", 9))
        status_lbl.pack(side=tk.LEFT)

        confirm_btn = tk.Button(row2, text="→  Confirm",
                                bg=BTN_CONT_BG, fg=BTN_CONT_FG,
                                font=("Segoe UI", 10, "bold"),
                                relief=tk.FLAT, padx=16, pady=7,
                                cursor="hand2",
                                state=tk.DISABLED,
                                command=lambda: self._respond(self._pending_folder),
                                )
        confirm_btn.pack(side=tk.RIGHT)
        _confirm.append(confirm_btn)

    def _panel_continue(self, prompt: str):
        self._clear()
        f = tk.Frame(self._action, bg=BG_ACTION)
        f.pack(fill=tk.X, padx=20, pady=14)

        is_err = any(w in prompt.lower()
                     for w in ("fix", "error", "failed", "manual", "faulty"))
        color  = FG_WARNING if is_err else FG_INFO
        icon   = "⚠" if is_err else "ℹ"

        clean = prompt.strip()
        for tail in ("Press Enter when ready…", "press Enter to continue…",
                     "then press Enter to continue…", "Press Enter when ready",
                     "press Enter when ready"):
            clean = clean.replace(tail, "").strip()
        clean = clean.strip().rstrip("…").strip()
        if not clean:
            clean = "Ready to continue"

        tk.Label(f, text=f"{icon}  {clean}",
                 bg=BG_ACTION, fg=color,
                 font=("Segoe UI", 10),
                 wraplength=700, justify=tk.LEFT,
                 ).pack(side=tk.LEFT, padx=(0, 20), anchor="w")

        tk.Button(f, text="→  Continue",
                  bg=BTN_CONT_BG, fg=BTN_CONT_FG,
                  font=("Segoe UI", 11, "bold"),
                  relief=tk.FLAT, padx=22, pady=9,
                  cursor="hand2",
                  command=lambda: self._respond(""),
                  ).pack(side=tk.RIGHT)

    def _panel_complete(self):
        self._clear()
        f = tk.Frame(self._action, bg=BG_ACTION)
        f.pack(fill=tk.X, padx=20, pady=14)
        tk.Label(f,
                 text="✅  All units processed.  Use the buttons below to save results and log.",
                 bg=BG_ACTION, fg=FG_SUCCESS,
                 font=("Segoe UI", 11, "bold"),
                 ).pack(side=tk.LEFT)
        self._status_var.set("Complete")
        self._prog["value"] = 100

        # Reveal results download button
        self._btn_results.pack(side=tk.LEFT, padx=(8, 0))

        # Ask about log after UI renders
        self.root.after(1500, self._ask_keep_log)

    # ═══════════════════════════════════════════════════════════════
    #  FILE SAVE & CLEANUP
    # ═══════════════════════════════════════════════════════════════

    def _save_log(self):
        if _log_file:
            try:
                _log_file.flush()
            except Exception:
                pass

        init_name = _log_path.name if _log_path else "debeed_log.txt"
        dst = filedialog.asksaveasfilename(
            title="Save Activity Log",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile=init_name,
            parent=self.root,
        )
        if not dst:
            return

        try:
            if _log_path and _log_path.exists():
                shutil.copy2(str(_log_path), dst)
            else:
                # Log file not started yet — save widget content
                content = self._log.get("1.0", tk.END)
                Path(dst).write_text(content, encoding="utf-8")
            self._append_log(f"  💾  Log saved → {dst}\n")
        except Exception as e:
            messagebox.showerror("Save Failed", f"Could not save log:\n{e}", parent=self.root)

    def _save_results(self):
        results = getattr(_run, '_pending_results', None)
        if not results:
            messagebox.showinfo("No Results", "No results available yet.", parent=self.root)
            return

        dst = filedialog.asksaveasfilename(
            title="Save Results",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile="results.csv",
            parent=self.root,
        )
        if not dst:
            return

        try:
            with open(dst, "w", newline="", encoding="utf-8") as f:
                w = _csv_mod.DictWriter(f, fieldnames=["page", "unit", "url", "status"])
                w.writeheader()
                w.writerows(results)
            self._btn_results.config(state=tk.DISABLED, text="✓  Results saved")
            self._append_log(f"  📄  Results saved → {dst}\n")
        except Exception as e:
            messagebox.showerror("Save Failed", f"Could not save results:\n{e}", parent=self.root)

    def _ask_keep_log(self):
        keep = messagebox.askyesno(
            "Save Activity Log?",
            "Automation complete.\n\n"
            "Would you like to save the activity log?\n"
            "(If you already used the Save Log button, click No.)",
            parent=self.root,
        )
        if keep:
            self._save_log()
        self._cleanup_log()
        self._log_cleaned = True

    def _cleanup_log(self):
        global _log_file
        if _log_file:
            try:
                _log_file.close()
            except Exception:
                pass
            _log_file = None
        if _log_path and _log_path.exists():
            try:
                _log_path.unlink()
            except Exception:
                pass

    def _quit(self):
        if self._started and not self._log_cleaned:
            if not messagebox.askyesno(
                "Quit",
                "Automation may still be running.\n\n"
                "Quit anyway? Unsaved logs will be deleted.",
                parent=self.root,
            ):
                return
        self._cleanup_log()
        self.root.destroy()

    # ═══════════════════════════════════════════════════════════════
    #  RESPONSE & AUTOMATION THREAD
    # ═══════════════════════════════════════════════════════════════

    def _respond(self, value: str):
        self._response_q.put(value)
        self._wait_evt.set()
        self._panel_running()

    def _ensure_chrome_path(self):
        try:
            from _exe_setup import get_chrome_path
        except ImportError:
            return
        path = get_chrome_path()
        if path:
            self._status_var.set("Ready  —  click Start to begin")
        else:
            self._status_var.set("Chrome not set  —  reopen app to choose it")
            messagebox.showwarning(
                "Chrome Not Found",
                "Chrome could not be located automatically.\n\n"
                "You can still click Start — but if Chrome doesn't open, "
                "close and reopen the app to select chrome.exe manually.",
                parent=self.root,
            )

    def _start(self):
        if self._started:
            return
        self._started = True
        self._panel_running("Connecting to Chrome…")
        threading.Thread(target=self._run_automation, daemon=True).start()

    def _run_automation(self):
        try:
            _run.main()
        except SystemExit:
            pass
        except Exception as exc:
            self._log_q.put(("log", f"\n❌  Fatal error: {exc}\n"))
        finally:
            self._log_q.put(("done", None))

    # ═══════════════════════════════════════════════════════════════
    #  IO PATCHING
    # ═══════════════════════════════════════════════════════════════

    def _patch_io(self):
        app = self

        class GUIStream:
            def write(self, text):
                if text:
                    app._log_q.put(("log", text))
                    if _log_file:
                        try:
                            _log_file.write(text)
                            _log_file.flush()
                        except Exception:
                            pass

            def flush(self):
                pass

            def fileno(self):
                raise io.UnsupportedOperation("no fileno")

        sys.stdout = GUIStream()
        sys.stderr = GUIStream()

        def gui_input(prompt: str = "") -> str:
            if _log_file and prompt:
                try:
                    _log_file.write(f"PROMPT: {prompt.rstrip()}\n")
                    _log_file.flush()
                except Exception:
                    pass

            p = prompt.lower()
            if "(y/n)" in p or "y/n" in p:
                kind = "yes_no"
            elif "folder path" in p or "path for" in p or "new folder" in p:
                kind = "folder"
            else:
                kind = "enter"

            app._input_q.put((kind, prompt))
            app._wait_evt.clear()
            app._wait_evt.wait()

            response = app._response_q.get()

            if _log_file:
                try:
                    _log_file.write(f"RESPONSE: {response}\n")
                    _log_file.flush()
                except Exception:
                    pass

            return response

        builtins.input = gui_input

    # ═══════════════════════════════════════════════════════════════
    #  LOG RENDERING
    # ═══════════════════════════════════════════════════════════════

    def _tag_for(self, line: str) -> str:
        s = line.strip()
        if not s:
            return "normal"
        if s.startswith(("✅", "✓ ", "✓\t")):
            return "success"
        if s.startswith(("✗ ", "❌")):
            return "error"
        if s.startswith("⚠"):
            return "warning"
        if s.startswith("ℹ"):
            return "info"
        if re.match(r"^──+\s*STEP", s):
            return "step"
        if re.match(r"^──", s):
            return "muted"
        if re.match(r"^\s*\[\d+/\d+\]", s):
            return "unit"
        if "═" in s or "──" in s:
            return "muted"
        if "[DEBUG]" in s or "DEBUG" in s or re.match(r"^\s*\[DEBUG\]", s):
            return "muted"
        if s.startswith(("↳", "→ ", "↩")):
            return "info"
        if s.startswith("["):
            return "muted"
        return "normal"

    def _append_log(self, text: str):
        self._log.config(state=tk.NORMAL)
        ts = f"[{datetime.now():%H:%M:%S}]  "

        for raw in text.splitlines(keepends=True):
            stripped = raw.rstrip("\r\n")
            if not stripped:
                self._log.insert(tk.END, "\n")
                continue
            tag = self._tag_for(stripped)
            self._log.insert(tk.END, ts,              "ts")
            self._log.insert(tk.END, stripped + "\n", tag)

        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

        m = re.search(r"\[(\d+)/(\d+)\]", text)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            pct = int(done / total * 100) if total else 0
            self._prog["value"] = pct
            self._unit_counter.set(f"Unit  {done} / {total}")
            self._status_var.set(f"Processing  |  Unit {done} of {total}")

    # ═══════════════════════════════════════════════════════════════
    #  QUEUE POLLING (main thread, every 80 ms)
    # ═══════════════════════════════════════════════════════════════

    def _poll(self):
        try:
            while True:
                kind, payload = self._log_q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self._panel_complete()
        except queue.Empty:
            pass

        try:
            kind, prompt = self._input_q.get_nowait()
            if kind == "yes_no":
                self._panel_yes_no(prompt)
            elif kind == "folder":
                self._panel_folder(prompt)
            else:
                self._panel_continue(prompt)
        except queue.Empty:
            pass

        self.root.after(80, self._poll)

    # ═══════════════════════════════════════════════════════════════
    #  WINDOW CLOSE
    # ═══════════════════════════════════════════════════════════════

    def _on_close(self):
        self._quit()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = DebeedApp()
    app.run()
