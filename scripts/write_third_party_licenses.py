"""Write notices for every pinned runtime distribution in a release bundle."""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import re
from pathlib import Path

LOCKED_PACKAGE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", re.MULTILINE)
LICENSE_PREFIXES = ("license", "copying", "notice", "third-party")
MAX_LICENSE_BYTES = 2 * 1024 * 1024


def packages_from_lock(lock_path: Path) -> list[tuple[str, str]]:
    text = lock_path.read_text(encoding="utf-8")
    return sorted(
        {(name, version) for name, version in LOCKED_PACKAGE.findall(text)},
        key=lambda item: item[0].casefold(),
    )


def license_files(distribution: metadata.Distribution) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for file in distribution.files or ():
        name = Path(file.name).name
        folded = name.casefold()
        if not folded.startswith(LICENSE_PREFIXES):
            continue
        try:
            path = Path(distribution.locate_file(file))
            if not path.is_file() or path.stat().st_size > MAX_LICENSE_BYTES:
                continue
            contents = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if contents:
            found.append((str(file), contents))
    return found


def render_notices(lock_path: Path) -> str:
    sections = [
        "THIRD-PARTY LICENSES",
        "",
        "The following runtime distributions are included in arXiv Filter.",
        "Each distribution retains its upstream license; see the included text below.",
    ]
    for name, locked_version in packages_from_lock(lock_path):
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"Locked runtime package is not installed: {name}") from exc
        installed_version = distribution.version
        if installed_version != locked_version:
            raise RuntimeError(
                f"Runtime package version mismatch for {name}: "
                f"lock={locked_version}, installed={installed_version}"
            )
        license_expression = distribution.metadata.get("License-Expression") or ""
        license_summary = distribution.metadata.get("License") or ""
        if not license_summary:
            classifiers = [
                value.partition(" :: License :: ")[2]
                for value in distribution.metadata.get_all("Classifier", [])
                if " :: License :: " in value
            ]
            license_summary = "; ".join(classifiers)
        files = license_files(distribution)
        sections.extend(
            [
                "",
                "=" * 78,
                f"{distribution.metadata.get('Name', name)} {installed_version}",
                f"License-Expression: {license_expression or '(not declared)'}",
                f"License: {license_summary or '(see upstream package metadata)'}",
            ]
        )
        if files:
            for relative_path, contents in files:
                sections.extend(["", f"--- {relative_path} ---", contents])
        else:
            sections.extend(
                [
                    "",
                    "No standalone license text was present in the installed distribution.",
                    "Consult the upstream project license identified in its package metadata.",
                ]
            )
    return "\n".join(sections).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_notices(args.lock), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
