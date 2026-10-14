"""
Air-Gapped Python Porter — a native desktop tool to download Python packages
(and optionally the Python interpreter itself) on an internet-connected machine,
bundle them to local files for transport across an air gap, then install them
offline with `pip install --no-index`.

Standard library ONLY (tkinter) — no pip install needed for the app itself.
The one requirement is a working Python with pip on PATH (that's what it drives).
Sibling to Air-Gapped Image Porter; same threading + plan-gate design.

Run:
    python porter.py

Why it exists: a plain `pip install` on an airgapped host has no internet. Here
you stage the exact downloads, review them, run them to produce a self-contained
bundle (wheels for each target platform + install scripts), carry it across, and
install with no network. Cross-platform wheels are the point: you can grab Linux
wheels from this Windows box.

Organization:
  1. pip_ops / runtime_ops — all subprocess + download logic (imported, threaded).
  2. JobRunner             — serializes steps on a daemon thread; a step is either
                             an argv (pip) or a callable (download). Streams to a
                             queue drained on the UI thread.
  3. App (tk.Tk)           — sidebar nav over Download / Runtimes / Targets /
                             Files / Preview screens, plus a shared log pane.

Threading rule: pip and downloads never run on the UI thread.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import pip_ops
import runtime_ops
from pip_ops import Target


# ---------------------------------------------------------------------------
# JobRunner — runs a list of steps on a background thread. A step is
# (label, action, stdin) where action is either a list[str] argv (run via pip_ops)
# or a callable(emit_line)->int (e.g. a file download). Stops at first failure.
# ---------------------------------------------------------------------------
class JobRunner:
    def __init__(self, out_q: "queue.Queue"):
        self.out_q = out_q
        self.busy = False

    def _emit(self, kind: str, text: str = ""):
        self.out_q.put((kind, text))

    def run_steps(self, steps, on_done=None):
        if self.busy:
            self._emit("line", "⚠ a job is already running — wait for it to finish.")
            return
        self.busy = True
        threading.Thread(target=self._worker, args=(steps, on_done), daemon=True).start()

    def _worker(self, steps, on_done):
        failed = None
        try:
            for label, action, stdin_text in steps:
                self._emit("step", label)
                try:
                    if callable(action):
                        code = action(lambda ln: self._emit("line", ln))
                    else:
                        self._emit("cmd", pip_ops.format_command(action))
                        code = pip_ops.run_stream(
                            action, lambda ln: self._emit("line", ln),
                            stdin_text=stdin_text)
                except FileNotFoundError:
                    self._emit("line", "✗ python/pip not found on PATH.")
                    failed = label
                    break
                except Exception as e:                      # noqa: BLE001
                    self._emit("line", f"✗ {e}")
                    failed = label
                    break
                if code != 0:
                    self._emit("line", f"✗ exit code {code}")
                    failed = label
                    break
                self._emit("ok", label)
        finally:
            self.busy = False
            self._emit("done", failed or "")
            if on_done:
                on_done(failed)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🐍 Air-Gapped Python Porter")
        self.geometry("1140x840")
        self._q: queue.Queue = queue.Queue()
        self.runner = JobRunner(self._q)
        self._action_btns: list[ttk.Button] = []
        self._pending_steps: list = []
        self._pending_after = None
        self._after_job = None
        self.targets: list[Target] = pip_ops.default_targets()
        self._target_vars: dict[int, tk.BooleanVar] = {}

        self._init_style()
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.sidebar = ttk.Frame(body, padding=(2, 6), style="Sidebar.TFrame")
        self.sidebar.pack(side="left", fill="y")
        self.nb = ttk.Notebook(body, style="Hidden.TNotebook")
        self.nb.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.nb.bind("<<NotebookTabChanged>>", self._on_screen_changed)

        self._screens = {}
        self._build_download_tab()
        self._build_runtimes_tab()
        self._build_targets_tab()
        self._build_files_tab()
        self._build_preview_tab()
        self._build_sidebar_nav()
        self._build_log_pane()

        # Confirm pip is reachable — it's the engine for everything here.
        code, out = pip_ops.run_capture(pip_ops.pip_version_argv())
        if code == 0:
            self._log(out.strip())
        else:
            self._log("⚠ could not run pip via this interpreter. Package downloads "
                      "will fail until `python -m pip` works.")
        self._log(f"Driving interpreter: {pip_ops.python_exe()}")

        self.nb.select(self._screens["Download Packages"])
        self.after(120, self._drain)

    # -- styling + nav ------------------------------------------------------
    def _init_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.layout("Hidden.TNotebook.Tab", [])
        style.configure("Sidebar.TFrame", background="#eef1f5")
        style.configure("Nav.TButton", anchor="w", padding=(10, 8), width=20,
                        font=("", 10))
        style.configure("NavHeader.TLabel", background="#eef1f5",
                        foreground="#667085", font=("", 8, "bold"))

    def _build_sidebar_nav(self):
        self._nav_buttons = {}
        ttk.Label(self.sidebar, text="🐍 Python Porter", style="NavHeader.TLabel",
                  font=("", 11, "bold")).pack(anchor="w", padx=8, pady=(2, 10))
        layout = [
            ("DOWNLOAD (online side)",
             ["Download Packages", "Python Runtimes", "Targets"]),
            ("TRANSPORT", ["Files", "Preview / Run"]),
        ]
        for header, labels in layout:
            ttk.Label(self.sidebar, text=header, style="NavHeader.TLabel").pack(
                anchor="w", padx=10, pady=(10, 2))
            for label in labels:
                b = ttk.Button(self.sidebar, text=label, style="Nav.TButton",
                               command=lambda fr=self._screens[label]: self.nb.select(fr))
                b.pack(fill="x", padx=4, pady=1)
                self._nav_buttons[label] = b

    def _on_screen_changed(self, _e=None):
        sel = self.nb.select()
        for label, b in getattr(self, "_nav_buttons", {}).items():
            b.state(["pressed"] if str(self._screens[label]) == str(sel) else ["!pressed"])
        if sel and str(sel) == str(self._screens.get("Files")):
            self._refresh_files()

    # -- Download Packages tab ---------------------------------------------
    def _build_download_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Download Packages")
        self._screens["Download Packages"] = tab

        ttk.Label(tab, text="Packages to download (one pip spec per line; "
                  "# comments ignored):").pack(anchor="w")
        self.reqs_txt = scrolledtext.ScrolledText(tab, height=9, wrap="none")
        self.reqs_txt.pack(fill="both", expand=True)
        self.reqs_txt.insert("1.0", "# e.g.\n# requests==2.32.3\n# numpy\n# "
                             "django>=4.2,<5\n")

        filebar = ttk.Frame(tab)
        filebar.pack(fill="x", pady=4)
        ttk.Button(filebar, text="Load requirements.txt…",
                   command=self._load_reqs).pack(side="left")
        self.include_deps = tk.BooleanVar(value=True)
        ttk.Checkbutton(filebar, text="include dependencies",
                        variable=self.include_deps).pack(side="left", padx=12)

        # Targets to download for (checkboxes, managed on the Targets screen).
        tf = ttk.LabelFrame(tab, text="Download for these targets", padding=6)
        tf.pack(fill="x", pady=(8, 2))
        self._targets_box = ttk.Frame(tf)
        self._targets_box.pack(fill="x")
        self._render_target_checks()

        out = ttk.Frame(tab)
        out.pack(fill="x", pady=(8, 2))
        ttk.Label(out, text="Bundle folder:").pack(side="left")
        self.bundle_var = tk.StringVar(value=str(Path.cwd() / "python-bundle"))
        ttk.Entry(out, textvariable=self.bundle_var).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(out, text="Browse…",
                   command=lambda: self._browse_into(self.bundle_var)).pack(side="left")

        act = ttk.Frame(tab)
        act.pack(fill="x", pady=(10, 0))
        b = ttk.Button(act, text="⬇ Stage download", command=self._stage_download)
        b.pack(side="left", padx=4)
        self._action_btns.append(b)
        ttk.Label(act, text="→ review exact commands in Preview / Run, then run.",
                  foreground="#777").pack(side="left", padx=8)

    def _render_target_checks(self):
        for w in self._targets_box.winfo_children():
            w.destroy()
        self._target_vars = {}
        for i, t in enumerate(self.targets):
            var = tk.BooleanVar(value=(i == 0))   # current machine on by default
            self._target_vars[i] = var
            ttk.Checkbutton(self._targets_box, text=t.name, variable=var).grid(
                row=i // 2, column=i % 2, sticky="w", padx=6, pady=2)

    def _load_reqs(self):
        path = filedialog.askopenfilename(
            title="Load requirements", filetypes=[("Requirements", "*.txt"), ("All", "*.*")])
        if path:
            try:
                self.reqs_txt.delete("1.0", "end")
                self.reqs_txt.insert("1.0", Path(path).read_text(encoding="utf-8"))
            except OSError as e:
                messagebox.showerror("Load", str(e))

    def _selected_targets(self) -> list[Target]:
        return [self.targets[i] for i, v in self._target_vars.items() if v.get()]

    def _stage_download(self):
        specs = pip_ops.parse_requirements(self.reqs_txt.get("1.0", "end"))
        if not specs:
            messagebox.showwarning("No packages", "List at least one package.")
            return
        targets = self._selected_targets()
        if not targets:
            messagebox.showwarning("No targets", "Tick at least one target.")
            return
        bundle = Path(self.bundle_var.get().strip() or "python-bundle")
        steps = []
        for t in targets:
            dest = bundle / t.subdir
            label = f"download {len(specs)} package(s) for {t.name}"
            steps.append((label, pip_ops.download_argv(
                specs, str(dest), t, self.include_deps.get()), None))
            # Each target gets its own install scaffold pointing at its subdir.
            steps.append((
                f"write install scripts for {t.name}",
                self._scaffold_step(bundle, t.subdir, specs), None))
        self._stage(steps, after=self._refresh_files)

    def _scaffold_step(self, bundle: Path, subdir: str, specs: list[str]):
        """Return a callable step that writes requirements + install scripts."""
        def _run(emit):
            try:
                written = runtime_ops.write_bundle_scaffold(bundle, subdir, specs)
                for p in written:
                    emit(f"  wrote {p.name}")
                return 0
            except OSError as e:
                emit(f"  ✗ {e}")
                return 1
        return _run

    # -- Python Runtimes tab ------------------------------------------------
    def _build_runtimes_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Python Runtimes")
        self._screens["Python Runtimes"] = tab

        ttk.Label(tab, text="Also bundle the Python interpreter itself — for "
                  "airgaps with no Python installed. Sourced from python.org.",
                  foreground="#555").pack(anchor="w")

        pick = ttk.Frame(tab)
        pick.pack(fill="x", pady=(10, 4))
        ttk.Label(pick, text="Family:").grid(row=0, column=0, sticky="e")
        self.rt_family = tk.StringVar(value=runtime_ops.RUNTIME_FAMILIES[0])
        ttk.Combobox(pick, textvariable=self.rt_family, state="readonly", width=34,
                     values=runtime_ops.RUNTIME_FAMILIES).grid(
            row=0, column=1, sticky="w", padx=4)
        ttk.Label(pick, text="Python:").grid(row=0, column=2, sticky="e", padx=(12, 0))
        self.rt_minor = tk.StringVar(value="3.12")
        ttk.Combobox(pick, textvariable=self.rt_minor, state="readonly", width=8,
                     values=pip_ops.PYTHON_MINORS).grid(row=0, column=3, sticky="w", padx=4)
        ttk.Button(pick, text="+ Add to list", command=self._add_runtime).grid(
            row=0, column=4, padx=(12, 0))

        cols = ("family", "file", "note")
        tree = ttk.Treeview(tab, columns=cols, show="headings", height=8,
                            selectmode="extended")
        for c, w in (("family", 220), ("file", 240), ("note", 420)):
            tree.heading(c, text=c.capitalize())
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True, pady=8)
        self.rt_tree = tree
        self._rt_rows: list[runtime_ops.RuntimeArtifact] = []

        out = ttk.Frame(tab)
        out.pack(fill="x", pady=(0, 4))
        ttk.Label(out, text="Runtimes folder:").pack(side="left")
        self.rt_dir = tk.StringVar(value=str(Path.cwd() / "python-bundle" / "runtimes"))
        ttk.Entry(out, textvariable=self.rt_dir).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(out, text="Browse…",
                   command=lambda: self._browse_into(self.rt_dir)).pack(side="left")

        act = ttk.Frame(tab)
        act.pack(fill="x")
        b = ttk.Button(act, text="🗑 Remove selected", command=self._remove_runtime)
        b.pack(side="left", padx=4)
        b = ttk.Button(act, text="⬇ Stage runtime download", command=self._stage_runtimes)
        b.pack(side="left", padx=4)
        self._action_btns.append(b)

    def _add_runtime(self):
        arts = runtime_ops.runtime_artifacts(self.rt_family.get(), self.rt_minor.get())
        for a in arts:
            self._rt_rows.append(a)
            self.rt_tree.insert("", "end",
                                values=(self.rt_family.get(), a.filename, a.note))

    def _remove_runtime(self):
        for iid in self.rt_tree.selection():
            idx = self.rt_tree.index(iid)
            self.rt_tree.delete(iid)
            del self._rt_rows[idx]

    def _stage_runtimes(self):
        if not self._rt_rows:
            messagebox.showwarning("Runtimes", "Add at least one runtime to the list.")
            return
        dest_dir = Path(self.rt_dir.get().strip() or "runtimes")
        steps = []
        for a in self._rt_rows:
            dest = dest_dir / a.filename
            steps.append((f"download {a.filename}",
                          self._download_step(a.url, dest), None))
        self._stage(steps, after=self._refresh_files)

    def _download_step(self, url: str, dest: Path):
        def _run(emit):
            emit(f"  GET {url}")
            return runtime_ops.download_file(url, dest, emit)
        return _run

    # -- Targets tab --------------------------------------------------------
    def _build_targets_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Targets")
        self._screens["Targets"] = tab

        ttk.Label(tab, text="Platform targets to download wheels for. The "
                  "Download screen lets you tick which of these to use.",
                  foreground="#555").pack(anchor="w")

        cols = ("name", "python", "platforms", "abi")
        tree = ttk.Treeview(tab, columns=cols, show="headings", height=9,
                            selectmode="browse")
        for c, w in (("name", 240), ("python", 70), ("platforms", 380), ("abi", 80)):
            tree.heading(c, text=c.capitalize())
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True, pady=8)
        self.tgt_tree = tree
        self._render_targets_table()

        add = ttk.LabelFrame(tab, text="Add a target", padding=6)
        add.pack(fill="x")
        ttk.Label(add, text="Platform:").grid(row=0, column=0, sticky="e")
        self.new_family = tk.StringVar(value=list(pip_ops.PLATFORM_FAMILIES)[0])
        ttk.Combobox(add, textvariable=self.new_family, state="readonly", width=32,
                     values=list(pip_ops.PLATFORM_FAMILIES)).grid(
            row=0, column=1, sticky="w", padx=4)
        ttk.Label(add, text="Python:").grid(row=0, column=2, sticky="e", padx=(12, 0))
        self.new_minor = tk.StringVar(value="3.12")
        ttk.Combobox(add, textvariable=self.new_minor, state="readonly", width=8,
                     values=pip_ops.PYTHON_MINORS).grid(row=0, column=3, sticky="w", padx=4)
        ttk.Button(add, text="+ Add target", command=self._add_target).grid(
            row=0, column=4, padx=(12, 0))
        ttk.Button(add, text="🗑 Remove selected", command=self._remove_target).grid(
            row=0, column=5, padx=4)

    def _render_targets_table(self):
        self.tgt_tree.delete(*self.tgt_tree.get_children(""))
        for t in self.targets:
            plats = "this machine" if t.current else ", ".join(t.platform_tags)
            self.tgt_tree.insert("", "end",
                                 values=(t.name, t.python_version, plats, t.abi))

    def _add_target(self):
        family = self.new_family.get()
        minor = self.new_minor.get()
        tags = pip_ops.PLATFORM_FAMILIES[family]
        name = f"{family} · py{minor}"
        if any(t.name == name for t in self.targets):
            messagebox.showinfo("Target", "That target already exists.")
            return
        self.targets.append(Target(name, minor, list(tags)))
        self._render_targets_table()
        self._render_target_checks()

    def _remove_target(self):
        sel = self.tgt_tree.selection()
        if not sel:
            return
        idx = self.tgt_tree.index(sel[0])
        if self.targets[idx].current:
            messagebox.showinfo("Target", "Keep the current-machine target.")
            return
        del self.targets[idx]
        self._render_targets_table()
        self._render_target_checks()

    # -- Files tab (directory browser) -------------------------------------
    def _build_files_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Files")
        self._screens["Files"] = tab

        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Root:").pack(side="left")
        self.files_root_var = tk.StringVar(value=str(Path.cwd()))
        ttk.Entry(top, textvariable=self.files_root_var).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="Browse…",
                   command=lambda: (self._browse_into(self.files_root_var),
                                    self._refresh_files())).pack(side="left")
        ttk.Button(top, text="⟳", width=3, command=self._refresh_files).pack(side="left")

        tree = ttk.Treeview(tab, columns=("size", "modified"), height=18)
        tree.heading("#0", text="Name")
        tree.column("#0", width=520)
        tree.heading("size", text="Size")
        tree.column("size", width=110, anchor="e")
        tree.heading("modified", text="Modified")
        tree.column("modified", width=160)
        tree.pack(fill="both", expand=True, pady=6)
        tree.bind("<<TreeviewOpen>>", self._on_file_open)
        self.files_tree = tree

        ttk.Button(tab, text="Open in file manager",
                   command=self._open_in_manager).pack(anchor="w")

    def _fmt_size(self, n: float) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _insert_dir(self, parent, path: Path):
        try:
            entries = sorted(path.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for p in entries:
            try:
                st = p.stat()
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                size = "" if p.is_dir() else self._fmt_size(st.st_size)
            except OSError:
                mtime, size = "", ""
            icon = "📁 " if p.is_dir() else "📄 "
            node = self.files_tree.insert(parent, "end", text=icon + p.name,
                                          values=(size, mtime), open=False)
            self.files_tree.item(node, tags=(str(p),))
            if p.is_dir():
                self.files_tree.insert(node, "end", text="…")

    def _on_file_open(self, _e=None):
        node = self.files_tree.focus()
        tags = self.files_tree.item(node, "tags")
        if not tags:
            return
        kids = self.files_tree.get_children(node)
        if len(kids) == 1 and not self.files_tree.item(kids[0], "tags"):
            self.files_tree.delete(kids[0])
            self._insert_dir(node, Path(tags[0]))

    def _refresh_files(self):
        if not hasattr(self, "files_tree"):
            return
        self.files_tree.delete(*self.files_tree.get_children(""))
        root = Path(self.files_root_var.get().strip() or ".")
        if root.exists():
            self._insert_dir("", root)

    def _open_in_manager(self):
        sel = self.files_tree.focus()
        tags = self.files_tree.item(sel, "tags") if sel else ()
        target = Path(tags[0]) if tags else Path(self.files_root_var.get())
        folder = target if target.is_dir() else target.parent
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as e:
            messagebox.showerror("Open", str(e))

    # -- Preview / Run tab --------------------------------------------------
    def _build_preview_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Preview / Run")
        self._screens["Preview / Run"] = tab
        self._preview_tab = tab

        ttk.Label(tab, text="Every action stages its exact steps here first. "
                  "Nothing runs until you press “Run plan”.",
                  foreground="#555").pack(anchor="w")

        cols = ("#", "action", "command")
        tree = ttk.Treeview(tab, columns=cols, show="headings", height=14)
        tree.heading("#", text="#")
        tree.column("#", width=38, anchor="e", stretch=False)
        tree.heading("action", text="Action")
        tree.column("action", width=300, stretch=False)
        tree.heading("command", text="Command / operation")
        tree.column("command", width=640)
        tree.pack(fill="both", expand=True, pady=6)
        self.plan_tree = tree

        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        self.run_plan_btn = ttk.Button(bar, text="▶ Run plan",
                                       command=self._run_plan, state="disabled")
        self.run_plan_btn.pack(side="left")
        ttk.Button(bar, text="Clear plan", command=self._clear_plan).pack(
            side="left", padx=6)
        self.plan_label = ttk.Label(bar, text="No plan staged.", foreground="#777")
        self.plan_label.pack(side="left", padx=12)

    def _render_plan(self):
        self.plan_tree.delete(*self.plan_tree.get_children(""))
        for i, (label, action, _stdin) in enumerate(self._pending_steps, 1):
            shown = "(local operation)" if callable(action) else pip_ops.format_command(action)
            self.plan_tree.insert("", "end", values=(i, label, shown))
        n = len(self._pending_steps)
        self.plan_label.config(
            text=(f"{n} step(s) staged — review, then Run plan."
                  if n else "No plan staged."))
        self.run_plan_btn.config(state=("normal" if n and not self.runner.busy
                                        else "disabled"))

    def _clear_plan(self):
        self._pending_steps = []
        self._pending_after = None
        self._render_plan()

    def _run_plan(self):
        if self.runner.busy or not self._pending_steps:
            return
        steps = self._pending_steps
        self._after_job = self._pending_after
        self._pending_steps = []
        self._pending_after = None
        self._render_plan()
        self._set_busy(True)
        self.runner.run_steps(steps)

    # -- log pane + plumbing ------------------------------------------------
    def _build_log_pane(self):
        f = ttk.LabelFrame(self, text="Output", padding=4)
        f.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log = scrolledtext.ScrolledText(f, height=11, wrap="word",
                                             state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        bottom = ttk.Frame(f)
        bottom.pack(fill="x", pady=(4, 0))
        self.status = ttk.Label(bottom, text="Ready.")
        self.status.pack(side="left")
        ttk.Button(bottom, text="Clear", command=self._clear_log).pack(side="right")

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _log(self, text: str):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for b in self._action_btns:
            b.config(state=state)
        self.run_plan_btn.config(
            state=("disabled" if busy or not self._pending_steps else "normal"))
        self.status.config(text="Working…" if busy else "Ready.")

    def _stage(self, steps, after=None):
        if self.runner.busy:
            messagebox.showinfo("Busy", "A job is already running.")
            return
        if not steps:
            return
        self._pending_steps = steps
        self._pending_after = after
        self._render_plan()
        self.nb.select(self._preview_tab)
        self._log(f"Staged {len(steps)} step(s) → review in Preview / Run.")

    def _drain(self):
        try:
            while True:
                kind, text = self._q.get_nowait()
                if kind == "step":
                    self._log(f"\n▶ {text}")
                elif kind == "cmd":
                    self._log(f"  $ {text}")
                elif kind == "line":
                    self._log(f"    {text}")
                elif kind == "ok":
                    self._log(f"  ✓ {text}")
                elif kind == "done":
                    self._set_busy(False)
                    self._log(f"\n✗ stopped at: {text}" if text
                              else "\n✓ all steps completed.")
                    cb, self._after_job = self._after_job, None
                    if cb:
                        cb()
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _browse_into(self, var):
        d = filedialog.askdirectory(title="Choose folder")
        if d:
            var.set(d)


if __name__ == "__main__":
    App().mainloop()
