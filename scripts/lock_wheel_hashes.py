"""Create a wheel-hash lock for a specific platform from a resolved pin file."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from packaging.utils import canonicalize_name, parse_wheel_filename

PIN = re.compile(
    r"^([A-Za-z0-9_.-]+)(?:\[[^\]\r\n]+\])?==([^\s\\]+)", re.MULTILINE
)


def render_lock(resolved: Path, wheelhouse: Path) -> str:
    pins = {
        (canonicalize_name(name), version)
        for name, version in PIN.findall(resolved.read_text(encoding="utf-8"))
    }
    hashes: dict[tuple[str, str], list[str]] = {pin: [] for pin in pins}
    for wheel in sorted(wheelhouse.glob("*.whl")):
        name, version, _build, _tags = parse_wheel_filename(wheel.name)
        pin = (canonicalize_name(name), str(version))
        if pin in hashes:
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            hashes[pin].append(digest)
    missing = sorted(f"{name}=={version}" for (name, version), values in hashes.items() if not values)
    if missing:
        raise RuntimeError("No compatible wheel downloaded for: " + ", ".join(missing))

    lines = [
        "# Generated pins for CPython 3.12 Windows x64; hashes cover compatible wheels.",
        "# Refresh pins with pip-compile --allow-unsafe, pip download --platform win_amd64,",
        "# then scripts/lock_wheel_hashes.py. PyInstaller builds are platform-specific.",
        "",
    ]
    for (name, version), values in sorted(hashes.items()):
        lines.append(f"{name}=={version} \\")
        digests = sorted(set(values))
        lines.extend(f"    --hash=sha256:{digest} \\" for digest in digests[:-1])
        lines.append(f"    --hash=sha256:{digests[-1]}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render_lock(args.resolved, args.wheelhouse), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
