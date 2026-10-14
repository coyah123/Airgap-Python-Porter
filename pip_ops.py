"""
pip_ops — all pip/subprocess logic for the Python Porter, kept out of the UI.

Standard library only. Shells out to `python -m pip download` and streams output
line-by-line so the GUI can show progress. The important bit is cross-platform
targeting: to grab wheels for a platform other than the one you're running on,
pip needs --only-binary=:all: plus explicit --platform / --python-version /
--implementation / --abi flags (it can't build an sdist for a foreign platform).
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# A Target describes one (platform, python version) combo to download wheels
# for. `current` means "this machine" — download for the running interpreter,
# allowing source dists too. Otherwise we cross-target with wheel-only flags.
# ---------------------------------------------------------------------------
@dataclass
class Target:
    name: str                       # human label shown in the UI
    python_version: str             # e.g. "3.12"
    platform_tags: list[str] = field(default_factory=list)  # pip --platform values
    implementation: str = "cp"      # cp = CPython
    current: bool = False           # this machine; ignore the tags above

    @property
    def abi(self) -> str:
        # cp312, cp39, ... — derived from the python version.
        major, minor = self.python_version.split(".")[:2]
        return f"{self.implementation}{major}{minor}"

    @property
    def subdir(self) -> str:
        """Folder name for this target's wheels inside the bundle."""
        safe = self.name.lower()
        for ch in ' ()·./':
            safe = safe.replace(ch, "-")
        while "--" in safe:
            safe = safe.replace("--", "-")
        return safe.strip("-")


def default_targets() -> list[Target]:
    """A sensible starting set — the common airgap destinations."""
    pv = f"{sys.version_info.major}.{sys.version_info.minor}"
    return [
        Target("This machine (current)", pv, current=True),
        Target("Linux x86_64 · py3.12", "3.12",
               ["manylinux2014_x86_64", "manylinux_2_17_x86_64"]),
        Target("Linux aarch64 · py3.12", "3.12",
               ["manylinux2014_aarch64", "manylinux_2_17_aarch64"]),
        Target("Windows x86_64 · py3.12", "3.12", ["win_amd64"]),
        Target("macOS arm64 · py3.12", "3.12", ["macosx_11_0_arm64"]),
    ]


PLATFORM_FAMILIES = {
    "Linux x86_64 (manylinux2014)": ["manylinux2014_x86_64", "manylinux_2_17_x86_64"],
    "Linux aarch64 (manylinux2014)": ["manylinux2014_aarch64", "manylinux_2_17_aarch64"],
    "Windows x86_64": ["win_amd64"],
    "Windows x86 (32-bit)": ["win32"],
    "macOS arm64 (Apple Silicon)": ["macosx_11_0_arm64"],
    "macOS x86_64 (Intel)": ["macosx_10_9_x86_64"],
}

PYTHON_MINORS = ["3.9", "3.10", "3.11", "3.12", "3.13"]


# ---------------------------------------------------------------------------
# requirements parsing
# ---------------------------------------------------------------------------
def parse_requirements(text: str) -> list[str]:
    """One spec per line; drop blanks and # comments. Keeps pip syntax intact."""
    specs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        specs.append(line)
    return specs


# ---------------------------------------------------------------------------
# command building
# ---------------------------------------------------------------------------
def python_exe() -> str:
    """The interpreter running this app — what we drive pip through."""
    return sys.executable or "python"


def download_argv(specs: list[str], dest: str, target: Target,
                  include_deps: bool = True) -> list[str]:
    """Build the `python -m pip download` argv for one target.

    For a cross-platform target we force wheel-only (--only-binary=:all:) and
    pin platform/abi/version, because pip cannot build a source dist for a
    platform that isn't the one running. For the current machine we allow sdists.
    """
    argv = [python_exe(), "-m", "pip", "download", "--dest", dest]
    if not target.current:
        argv.append("--only-binary=:all:")
        for tag in target.platform_tags:
            argv += ["--platform", tag]
        argv += ["--python-version", target.python_version,
                 "--implementation", target.implementation,
                 "--abi", target.abi]
    if not include_deps:
        argv.append("--no-deps")
    argv += specs
    return argv


def pip_version_argv() -> list[str]:
    return [python_exe(), "-m", "pip", "--version"]


# ---------------------------------------------------------------------------
# subprocess helpers (mirrors the Image Porter's docker_ops)
# ---------------------------------------------------------------------------
def format_command(argv: list[str]) -> str:
    """Render argv for display, quoting args that contain spaces."""
    out = []
    for a in argv:
        out.append(f'"{a}"' if (" " in a or "\t" in a) else a)
    return " ".join(out)


def _no_window_kwargs() -> dict:
    """Keep a console window from flashing up on Windows for each subprocess."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def run_stream(argv: list[str], on_line, stdin_text: str | None = None) -> int:
    """Run argv, feeding each stdout/stderr line to on_line. Returns exit code."""
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        **_no_window_kwargs())
    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    return proc.wait()


def run_capture(argv: list[str]) -> tuple[int, str]:
    """Run argv and capture combined output as a string."""
    proc = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", **_no_window_kwargs())
    return proc.returncode, proc.stdout
