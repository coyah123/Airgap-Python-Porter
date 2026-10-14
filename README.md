# 🐍 Air-Gapped Python Porter

A native desktop tool to pull Python packages (and optionally the Python
interpreter itself) down to local files on an internet-connected machine,
bundle them for transport across an air gap, then install them **offline** with
`pip install --no-index`.

Sibling to the Air-Gapped Image Porter — same design: standard-library tkinter
only (no `pip install` to run the app), every action is staged as an explicit
plan you review before it runs, and work happens off the UI thread.

## Why
A plain `pip install` on an airgapped host has no internet. This tool stages the
exact downloads on the online side, produces a self-contained bundle (wheels per
target platform + install scripts), which you carry across and install with no
network. **Cross-platform wheels are the point:** you can grab Linux/macOS
wheels from a Windows box, because it drives `pip download` with explicit
`--platform / --python-version / --abi / --only-binary=:all:` flags.

## Requirements
- A working Python with pip on PATH (that's the engine it drives).
- Internet on the machine you run it on. The airgap side needs nothing but Python.

## Run
```
python porter.py
```

## Workflow
1. **Download Packages** — list your pip specs, tick which targets to build for,
   pick a bundle folder, Stage.
2. **Python Runtimes** (optional) — add interpreter installers (Windows .exe /
   embeddable zip, macOS .pkg, Linux source) from python.org, Stage.
3. **Targets** — manage the platform/python-version combos offered on screen 1.
4. **Preview / Run** — review the exact commands, then **Run plan**.
5. **Files** — browse the produced bundle; open it in your file manager to copy
   to transport media.

## On the airgapped host
The bundle contains `requirements.txt`, an `install.ps1` / `install.sh`, and a
`README.md`. Install with no internet:

```
# Windows
.\install.ps1

# Linux / macOS
bash install.sh

# or directly
python -m pip install --no-index --find-links ./<target-subdir> -r requirements.txt
```

`--no-index` guarantees pip never touches the network; `--find-links` points it
at the local wheels. If install complains about a missing dependency, re-download
on the online side with that package added and "include dependencies" on.

## Files
- `porter.py` — the tkinter GUI (sidebar nav, plan-gate, log pane).
- `pip_ops.py` — pip command building, targets, subprocess streaming.
- `runtime_ops.py` — interpreter downloads + bundle scaffolding (install scripts).
