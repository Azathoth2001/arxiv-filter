"""Trim vendored KaTeX CSS to the woff2-only subset.

The upstream katex.min.css lists woff2 + woff + ttf for each of 20 fonts. We ship
only woff2 (supported by every browser this app targets), so the other two
formats would 404 on every page load. Removing them keeps the console clean
without touching the metrics or class rules.

Run after replacing the vendored KaTeX copy:

    python packaging/trim_katex_css.py
    # or: python scripts/../packaging/trim_katex_css.py from the repo root
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "src" / "arxiv_filter" / "static" / "vendor" / "katex" / "katex.min.css"

#: `,url(fonts/Foo.woff) format("woff")` — same shape for ttf/truetype.
#: Note the space between url() and format(): katex.min.css has it, so the
#: pattern tolerates optional whitespace rather than assuming a minified run.
DROP_SOURCE = re.compile(
    r",\s*url\(fonts/[^)]+?\.(?:woff|ttf)\)\s*format\(\s*[\"']?(?:woff|truetype)[\"']?\s*\)"
)


def main() -> int:
    if not CSS.is_file():
        print(f"not found: {CSS}")
        return 1

    text = CSS.read_text(encoding="utf-8")
    before = text.count("url(fonts/")
    trimmed, count = DROP_SOURCE.subn("", text)
    after = trimmed.count("url(fonts/")

    if count == 0 and after == before:
        print("nothing to trim (already woff2-only)")
        return 0

    CSS.write_text(trimmed, encoding="utf-8", newline="\n")
    print(f"removed {count} non-woff2 sources; url(fonts/…) {before} -> {after}")

    remaining = sorted(set(re.findall(r"url\(fonts/[^)]+\.(\w+)\)", trimmed)))
    print(f"remaining formats: {remaining}")
    if remaining != ["woff2"]:
        print("WARNING: expected woff2 only")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
