"""
runtime_ops — fetch the Python interpreter itself (for airgaps with no Python)
and generate the offline install bundle (requirements.txt + install scripts +
README). Standard library only; downloads go through urllib with a progress
callback so the GUI stays responsive.

Interpreter sources are python.org's FTP mirror, which has stable, predictable
URLs. The embeddable zip is the nicest airgap artifact for Windows: unzip and
run, no installer, no admin. For Linux we hand you the source tarball plus a
note, since prebuilt portable Linux builds live on a separately-versioned
project (python-build-standalone) that's better fetched by hand.
"""
from __future__ import annotations

import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Latest patch release we point at per minor line (python.org/ftp/python/<v>/).
LATEST_PATCH = {
    "3.9": "3.9.13",
    "3.10": "3.10.11",
    "3.11": "3.11.9",
    "3.12": "3.12.7",
    "3.13": "3.13.0",
}

# family label -> builder(full_version) -> list of (url, suggested filename, note)
_FTP = "https://www.python.org/ftp/python"


@dataclass
class RuntimeArtifact:
    url: str
    filename: str
    note: str


def runtime_artifacts(family: str, minor: str) -> list[RuntimeArtifact]:
    """Download descriptors for a given interpreter family + python minor."""
    v = LATEST_PATCH.get(minor, minor)
    base = f"{_FTP}/{v}"
    if family == "Windows installer (.exe, amd64)":
        return [RuntimeArtifact(f"{base}/python-{v}-amd64.exe",
                                f"python-{v}-amd64.exe",
                                "Run on the airgap host; check 'Add to PATH'.")]
    if family == "Windows embeddable zip (amd64)":
        return [RuntimeArtifact(f"{base}/python-{v}-embed-amd64.zip",
                                f"python-{v}-embed-amd64.zip",
                                "No installer: unzip anywhere and run python.exe.")]
    if family == "macOS universal2 (.pkg)":
        return [RuntimeArtifact(f"{base}/python-{v}-macos11.pkg",
                                f"python-{v}-macos11.pkg",
                                "Double-click to install on macOS 11+.")]
    if family == "Linux source tarball (.tgz)":
        return [RuntimeArtifact(f"{base}/Python-{v}.tgz",
                                f"Python-{v}.tgz",
                                "Source — ./configure && make on the target, or "
                                "use python-build-standalone for a prebuilt binary.")]
    return []


RUNTIME_FAMILIES = [
    "Windows installer (.exe, amd64)",
    "Windows embeddable zip (amd64)",
    "macOS universal2 (.pkg)",
    "Linux source tarball (.tgz)",
]


def download_file(url: str, dest: Path, on_line) -> int:
    """Download url -> dest with coarse progress. Returns 0 on success, 1 on error.

    Signature matches a JobRunner "callable step": it takes the emit-line fn and
    returns an exit code, so it can be staged alongside pip subprocess steps.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "airgap-python-porter"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (trusted host)
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            last = 0.0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last > 0.4:          # throttle progress lines
                        last = now
                        if total:
                            pct = done * 100 // total
                            on_line(f"    {pct:3d}%  {done >> 20} / {total >> 20} MB")
                        else:
                            on_line(f"    {done >> 20} MB")
        tmp.replace(dest)
        on_line(f"  saved → {dest}")
        return 0
    except Exception as e:                         # noqa: BLE001 - surface any failure
        on_line(f"  download failed: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return 1


# ---------------------------------------------------------------------------
# bundle scaffolding — what the airgap side actually runs
# ---------------------------------------------------------------------------
_INSTALL_PS1 = """\
# install.ps1 — run on the AIRGAPPED Windows host (no internet needed).
# Installs every wheel in this folder from local files only.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
python -m pip install --no-index --find-links "$here\\{subdir}" -r "$here\\requirements.txt"
Write-Host "Done. Installed from local wheels only." -ForegroundColor Green
"""

_INSTALL_SH = """\
#!/usr/bin/env bash
# install.sh — run on the AIRGAPPED host (no internet needed).
# Installs every wheel in this folder from local files only.
set -euo pipefail
here="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
python3 -m pip install --no-index --find-links "$here/{subdir}" -r "$here/requirements.txt"
echo "Done. Installed from local wheels only."
"""

_README = """\
# Air-gap Python bundle

Generated by Air-Gapped Python Porter on {when}.

## What's here
- `{subdir}/` — the downloaded wheels/sdists for this target.
- `requirements.txt` — the packages you asked for (feed to pip).
- `install.ps1` / `install.sh` — install on the airgapped host with no internet.
- any `python-*` interpreter files, if you also downloaded a runtime.

## Install on the airgapped host
If Python/pip is already present:

    # Windows
    .\\install.ps1

    # Linux / macOS
    bash install.sh

Or directly:

    python -m pip install --no-index --find-links ./{subdir} -r requirements.txt

`--no-index` guarantees pip never reaches out to the internet; `--find-links`
points it at the local folder. If an install complains about a missing
dependency, it wasn't downloaded for this platform — re-download on the online
side with that package added and "include dependencies" on.
"""


def write_bundle_scaffold(bundle_dir: Path, subdir: str, specs: list[str]) -> list[Path]:
    """Write requirements.txt, install scripts and README into bundle_dir."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    written = []

    req = bundle_dir / "requirements.txt"
    req.write_text("\n".join(specs) + "\n", encoding="utf-8")
    written.append(req)

    ps1 = bundle_dir / "install.ps1"
    ps1.write_text(_INSTALL_PS1.format(subdir=subdir), encoding="utf-8")
    written.append(ps1)

    sh = bundle_dir / "install.sh"
    sh.write_text(_INSTALL_SH.format(subdir=subdir), encoding="utf-8", newline="\n")
    written.append(sh)

    readme = bundle_dir / "README.md"
    readme.write_text(
        _README.format(subdir=subdir, when=time.strftime("%Y-%m-%d %H:%M")),
        encoding="utf-8")
    written.append(readme)
    return written
