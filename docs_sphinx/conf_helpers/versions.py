# This file is part of OpenCV project.
# It is subject to the license terms in the LICENSE file found in the top-level directory
# of this distribution and at http://opencv.org/license.html.
# Copyright (C) 2026, BigVision LLC, all rights reserved.
# Third party copyrights are property of their respective owners.

"""Build-time fetch of the OpenCV documentation version list.

The legacy `docs.opencv.org` site embeds a `<select>` of every published
version in its Doxygen footer. We scrape that list at Sphinx-build start
and emit `_static/versions.js`, which the navbar's `<select>` populates
from on page load — so the dropdown refreshes whenever the docs are
rebuilt without needing manual edits when OpenCV cuts a new release.

If the build host can't reach `docs.opencv.org` (offline build, transient
DNS / TLS failure), we fall back to a snapshot list so the build still
succeeds with a usable — if temporarily stale — dropdown.
"""

from __future__ import annotations
import json
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request

# docs.opencv.org loads its dropdown from a single canonical JS file at
# the site root (`/version.js`). It contains a literal array of
# `['<label>', '<relative-path>']` pairs which the Doxygen page-load
# script renders into a `<select>` client-side. We pull the same file
# and parse the array directly — far more stable than scraping the
# rendered HTML, since the array literal hasn't changed shape across
# years of OpenCV releases.
_SRC_URL = "https://docs.opencv.org/version.js"
_BASE_URL = "https://docs.opencv.org"
_TIMEOUT = 10  # seconds

# Snapshot used only when the live fetch fails. Kept lean — the build's
# expected path is the live fetch, this is a last-resort fallback so a
# broken network doesn't break the docs build.
_FALLBACK: list[dict] = [
    {"label": "4.x",    "url": "https://docs.opencv.org/4.x/"},
    {"label": "4.11.0", "url": "https://docs.opencv.org/4.11.0/"},
    {"label": "4.10.0", "url": "https://docs.opencv.org/4.10.0/"},
    {"label": "4.9.0",  "url": "https://docs.opencv.org/4.9.0/"},
    {"label": "4.8.0",  "url": "https://docs.opencv.org/4.8.0/"},
    {"label": "4.7.0",  "url": "https://docs.opencv.org/4.7.0/"},
    {"label": "4.6.0",  "url": "https://docs.opencv.org/4.6.0/"},
    {"label": "4.5.5",  "url": "https://docs.opencv.org/4.5.5/"},
    {"label": "4.5.4",  "url": "https://docs.opencv.org/4.5.4/"},
    {"label": "4.5.3",  "url": "https://docs.opencv.org/4.5.3/"},
    {"label": "4.5.2",  "url": "https://docs.opencv.org/4.5.2/"},
    {"label": "4.5.1",  "url": "https://docs.opencv.org/4.5.1/"},
    {"label": "4.5.0",  "url": "https://docs.opencv.org/4.5.0/"},
    {"label": "4.4.0",  "url": "https://docs.opencv.org/4.4.0/"},
    {"label": "4.3.0",  "url": "https://docs.opencv.org/4.3.0/"},
    {"label": "4.2.0",  "url": "https://docs.opencv.org/4.2.0/"},
    {"label": "4.1.2",  "url": "https://docs.opencv.org/4.1.2/"},
    {"label": "4.1.1",  "url": "https://docs.opencv.org/4.1.1/"},
    {"label": "4.1.0",  "url": "https://docs.opencv.org/4.1.0/"},
    {"label": "4.0.1",  "url": "https://docs.opencv.org/4.0.1/"},
    {"label": "4.0.0",  "url": "https://docs.opencv.org/4.0.0/"},
    {"label": "3.4.20", "url": "https://docs.opencv.org/3.4.20/"},
    {"label": "3.4.19", "url": "https://docs.opencv.org/3.4.19/"},
    {"label": "3.4.18", "url": "https://docs.opencv.org/3.4.18/"},
    {"label": "3.4.17", "url": "https://docs.opencv.org/3.4.17/"},
    {"label": "3.4.16", "url": "https://docs.opencv.org/3.4.16/"},
    {"label": "3.4.15", "url": "https://docs.opencv.org/3.4.15/"},
    {"label": "3.4.14", "url": "https://docs.opencv.org/3.4.14/"},
    {"label": "3.4.13", "url": "https://docs.opencv.org/3.4.13/"},
    {"label": "3.4.12", "url": "https://docs.opencv.org/3.4.12/"},
    {"label": "3.4.11", "url": "https://docs.opencv.org/3.4.11/"},
    {"label": "3.4.10", "url": "https://docs.opencv.org/3.4.10/"},
    {"label": "3.4.9",  "url": "https://docs.opencv.org/3.4.9/"},
    {"label": "3.4.8",  "url": "https://docs.opencv.org/3.4.8/"},
    {"label": "3.4.7",  "url": "https://docs.opencv.org/3.4.7/"},
    {"label": "3.4.6",  "url": "https://docs.opencv.org/3.4.6/"},
    {"label": "3.4.5",  "url": "https://docs.opencv.org/3.4.5/"},
    {"label": "3.4.4",  "url": "https://docs.opencv.org/3.4.4/"},
    {"label": "3.4.3",  "url": "https://docs.opencv.org/3.4.3/"},
    {"label": "3.4.2",  "url": "https://docs.opencv.org/3.4.2/"},
    {"label": "3.4.1",  "url": "https://docs.opencv.org/3.4.1/"},
    {"label": "3.4.0",  "url": "https://docs.opencv.org/3.4.0/"},
    {"label": "3.3.1",  "url": "https://docs.opencv.org/3.3.1/"},
    {"label": "3.3.0",  "url": "https://docs.opencv.org/3.3.0/"},
    {"label": "3.2.0",  "url": "https://docs.opencv.org/3.2.0/"},
    {"label": "3.1.0",  "url": "https://docs.opencv.org/3.1.0/"},
    {"label": "3.0.0",  "url": "https://docs.opencv.org/3.0.0/"},
]

# Strip Doxygen's "5.0.0-pre" entry — it's the slot our new "5.0" build
# now occupies as the dropdown's current selection, so listing it again
# alongside the legacy versions would be redundant. Pre-release suffixes
# vary ("-pre", "-rc1", …), so match by leading "5." rather than literal.
_DROP_LABEL_RE = re.compile(r"^5\.", re.IGNORECASE)


def _resolve_url(raw: str) -> str:
    """The upstream `version.js` stores each entry's destination as a
    site-absolute path like `/4.x` or `/3.4.20`. Join against the
    docs.opencv.org origin so the dropdown's `<option value>` is the
    full absolute URL needed for cross-origin navigation from our
    build."""
    raw = raw.strip()
    if raw.startswith(("http://", "https://")):
        return raw
    if not raw.startswith("/"):
        raw = "/" + raw
    if not raw.endswith("/"):
        raw = raw + "/"
    return _BASE_URL + raw


# Match a single `['label', 'path']` entry inside the array literal.
# Tolerates both single and double quotes (the upstream file uses
# single) and arbitrary whitespace between tokens.
_TUPLE_RE = re.compile(
    r"""\[\s*(['"])(?P<label>[^'"]+)\1\s*,\s*(['"])(?P<path>[^'"]+)\3\s*\]"""
)
# Strip `// …` line comments before parsing so commented-out tuples
# (e.g. `// no more 3.4 releases: ['3.4.21-pre', '/3.4'],`) don't get
# picked up. Block `/* … */` comments are unlikely in this file but
# also dropped for safety.
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _parse_versions(js: str) -> list[dict]:
    """Extract every `['<label>', '<path>']` tuple from the `versions`
    array literal in `version.js`. Order in the source file is
    preserved — matches the order docs.opencv.org renders in its own
    dropdown."""
    src = _BLOCK_COMMENT_RE.sub("", js)
    src = _LINE_COMMENT_RE.sub("", src)
    out: list[dict] = []
    for m in _TUPLE_RE.finditer(src):
        out.append({
            "label": m.group("label").strip(),
            "url": _resolve_url(m.group("path")),
        })
    return out


def _filter(versions: list[dict]) -> list[dict]:
    """Drop pre-3.0 releases (per user spec, list starts at 3.0.0) and
    any 5.* entry (replaced by our new build's "current" selection)."""
    out: list[dict] = []
    for v in versions:
        label = v["label"]
        if _DROP_LABEL_RE.match(label):
            continue
        m = re.match(r"^(\d+)", label)
        if not m:
            continue
        if int(m.group(1)) < 3:
            continue
        out.append(v)
    return out


def _fetch_live() -> list[dict]:
    try:
        req = urllib.request.Request(_SRC_URL, headers={
            # docs.opencv.org sometimes 403s the default urllib UA.
            "User-Agent": "Mozilla/5.0 (OpenCV docs build)",
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
        # Pages may declare UTF-8 or fall back to latin-1; `errors=replace`
        # keeps the parser robust against odd bytes in the legacy markup.
        html = raw.decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return []
    return _parse_versions(html)


def write_versions_js(static_dir: pathlib.Path, current_label: str) -> None:
    """Emit `<static_dir>/versions.js` with the populated version list.

    The file defines `window.OPENCV_VERSIONS = {current, versions}` —
    `_templates/opencv-version-switcher.html` reads this on page load to
    fill the navbar `<select>`. Falls back to the embedded snapshot when
    the live fetch returns nothing usable."""
    versions = _filter(_fetch_live())
    if not versions:
        versions = _FALLBACK
    payload = {"current": current_label, "versions": versions}
    static_dir.mkdir(parents=True, exist_ok=True)
    js = (
        "/* Auto-generated by conf_helpers/versions.py at Sphinx-build\n"
        " * start. Lists OpenCV documentation versions >= 3.0.0, scraped\n"
        " * live from docs.opencv.org. Read by\n"
        " * _templates/opencv-version-switcher.html to populate the navbar\n"
        " * dropdown — do not edit by hand, regenerate via a fresh build. */\n"
        "window.OPENCV_VERSIONS = "
        + json.dumps(payload, indent=2, ensure_ascii=False)
        + ";\n"
    )
    (static_dir / "versions.js").write_text(js, encoding="utf-8")
