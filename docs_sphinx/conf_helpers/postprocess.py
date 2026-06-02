"""build-finished hook: inline coll-diagram SVGs, strip Breathe clutter, re-theme Doxygen."""
from __future__ import annotations
import pathlib, re

from .state import (
    DOXYGEN_BASE_URL,
    _doxy_page_to_local,
    _DOXY_ANCHOR_TO_MEMBER,
    _LOCAL_CLASS_URL,
    _LOCAL_TYPEDEF_URL,
    _FILE_URL,
)


def _doxy_parent_page(page: str, api_dir: pathlib.Path) -> str:
    """Nested types (e.g. `structcv_1_1SparseMat_1_1Hdr`) get no standalone
    Sphinx page — they're documented inline on the enclosing class. Walk up the
    `_1_1`-separated scope to the nearest ancestor page that DOES exist locally.
    Returns "" if no ancestor page exists."""
    stem = page[:-5] if page.endswith(".html") else page
    rest = None
    for pref in ("class", "struct", "union"):
        if stem.startswith(pref):
            rest = stem[len(pref):]
            break
    if rest is None:
        return ""
    while "_1_1" in rest:
        rest = rest.rsplit("_1_1", 1)[0]
        for pref in ("class", "struct", "union"):
            cand = f"{pref}{rest}.html"
            if (api_dir / cand).is_file():
                return cand
    return ""


def _inline_collaboration_svgs(api_dir: pathlib.Path,
                              image_dir: pathlib.Path) -> None:
    """Inline coll-diagram SVGs so their links work; idempotent."""
    import re
    if not api_dir.is_dir():
        return
    img_re = re.compile(
        r'<img alt="(?P<alt>[^"]*)" '
        r'class="(?P<cls>opencv-coll-graph[^"]*)" '
        r'src="\.\./_images/(?P<file>[^"]+\.svg)"\s*/?>')
    href_re = re.compile(r'xlink:href="(?P<path>[^"]+)"')

    def _rewrite_href(m: "re.Match") -> str:
        path = m.group("path")
        if "://" in path:
            return m.group(0)
        base = path.rsplit("/", 1)[-1]
        page, _, frag = base.partition("#")   # split off the Doxygen anchor
        # Resolve the Doxygen page to its Sphinx equivalent; whatever the
        # original docs linked, link the same — but into the NEW Sphinx docs.
        local = _doxy_page_to_local(page)
        if not (api_dir / local).is_file():
            # Nested type with no own page -> enclosing class page (inline docs).
            parent = _doxy_parent_page(page, api_dir)
            if parent:
                local = parent
            # else keep `local` even if not generated this build (contrib/CUDA
            # off, or the _Tp stub) — never fall back to docs.opencv.org.
        # Jump to the exact member when the Doxygen anchor maps to Sphinx's.
        member = _DOXY_ANCHOR_TO_MEMBER.get(frag) if frag else None
        if member:
            return f'xlink:href="{local}#{member}"'
        return f'xlink:href="{local}"'

    for html in api_dir.glob("*.html"):
        text = html.read_text(encoding="utf-8")
        if "opencv-coll-graph" not in text:
            continue

        def _inline(m: "re.Match") -> str:
            svg_path = image_dir / m.group("file")
            if not svg_path.is_file():
                return m.group(0)
            svg = svg_path.read_text(encoding="utf-8")
            start = svg.find("<svg")
            if start < 0:
                return m.group(0)
            svg = href_re.sub(_rewrite_href, svg[start:])
            # carry theme classes + alt for dark mode / a11y
            return svg.replace(
                "<svg ",
                f'<svg class="{m.group("cls")}" role="img" '
                f'aria-label="{m.group("alt")}" ', 1)

        new = img_re.sub(_inline, text)
        if new != text:
            html.write_text(new, encoding="utf-8")


def _strip_breathe_class_clutter(api_dir: pathlib.Path) -> None:
    """Drop Breathe's duplicate class signature header; idempotent."""
    import re
    if not api_dir.is_dir():
        return
    section_re = re.compile(
        r'(<section id="detailed-description"[^>]*>)'
        r'(?P<body>[\s\S]*?)'
        r'(</section>)'
    )
    dl_re = re.compile(
        r'<dl[^>]*\bclass="[^"]*\bclass\b[^"]*"[^>]*>\s*'
        r'<dt[^>]*>[\s\S]*?</dt>\s*'
        r'<dd>(?P<dd>[\s\S]*?)</dd>\s*'
        r'</dl>'
    )
    subclassed_re = re.compile(r'<p>Subclassed by[\s\S]*?</p>\s*')

    for h in api_dir.glob("classcv*.html"):
        text = h.read_text(encoding="utf-8")
        if "detailed-description" not in text:
            continue

        def _strip_section(sm):
            head, body, tail = sm.group(1), sm.group("body"), sm.group(3)

            def _strip_dl(dm):
                dd_body = dm.group("dd").strip()
                dd_body = subclassed_re.sub("", dd_body).strip()
                return dd_body

            new_body = dl_re.sub(_strip_dl, body, count=1)
            return head + new_body + tail

        new = section_re.sub(_strip_section, text, count=1)
        if new != text:
            h.write_text(new, encoding="utf-8")


def _generate_search_map(out_dir: pathlib.Path) -> None:
    """Write _static/search_map.js: stem→Sphinx-path for every built HTML page."""
    import json
    skip = {"_static", "_sources", "_images", "_sphinx_design_static"}
    mapping = {}
    for f in out_dir.rglob("*.html"):
        rel = f.relative_to(out_dir)
        if rel.parts[0] in skip:
            continue
        mapping[f.stem] = rel.as_posix()
    lines = ["var sphinxPageMap = {"]
    for k, v in sorted(mapping.items()):
        lines.append(f"  {json.dumps(k)}: {json.dumps(v)},")
    lines.append("};")
    (out_dir / "_static" / "search_map.js").write_text("\n".join(lines), encoding="utf-8")


# Pygments emits `<span class="n">NAME</span>` (and `class="nc"`/`"nf"`
# for class/function tokens) inside its rendered `<pre>` for every C++
# identifier in a code block. The example pages, snippet pages, tutorial
# samples, and any `:::{code-block} cpp` fence all go through Pygments,
# so by the time the HTML is written the code blocks have full syntax
# colouring but ZERO clickable tokens — `Mat`, `InputArray`,
# `getOptimalDFTSize`, etc. are inert text.
#
# This pass wraps each such span in an `<a class="reference internal"
# href="…">` when the token resolves via `_LOCAL_CLASS_URL` /
# `_LOCAL_TYPEDEF_URL`, mirroring what the API-stub renderer already
# does for inline `<programlisting>`. Idempotent: skips spans already
# inside an `<a>`.
_PYG_IDENT_SPAN_RE = re.compile(
    r'(?P<prefix><span class="(?:n|nc|nf|nb|nv|na)">)'
    r'(?P<name>[A-Za-z_][A-Za-z0-9_]*)'
    r'(?P<suffix></span>)'
)

# Pygments preprocessor-file-name span:
#   `<span class="cpf">&quot;opencv2/core.hpp&quot;</span>`  (or &lt;…&gt;)
# The quote characters are HTML-escaped (`&quot;` / `&lt;` / `&gt;`).
# Capture the inner path so we can wrap it in an `<a>` linking to the
# local Doxygen file page (`_FILE_URL` map). The quote chars stay
# outside the anchor — mirrors how the enum-detail `#include` line is
# rendered (`#include <a href="…">opencv2/core.hpp</a>`).
_PYG_CPF_SPAN_RE = re.compile(
    r'(?P<prefix><span class="cpf">)'
    r'(?P<openq>&quot;|&lt;)'
    r'(?P<path>[A-Za-z0-9_./+\-]+\.[A-Za-z0-9]+)'
    r'(?P<closeq>&quot;|&gt;)'
    r'(?P<suffix></span>)'
)


def _linkify_code_blocks(html_dir: pathlib.Path) -> None:
    """Walk every `.html` under `html_dir` and turn known identifier
    tokens inside Pygments-rendered `<pre>` blocks into clickable
    anchors. The substitution is scoped to spans inside `<pre>` so we
    don't accidentally repaint inline `<code class="n">` chips in
    prose; the rule above already targets only Pygments span classes
    that Pygments uses inside its `<pre>` output."""
    if not (_LOCAL_CLASS_URL or _LOCAL_TYPEDEF_URL or _FILE_URL):
        return
    if not html_dir.is_dir():
        return
    import os

    def _resolve(name: str) -> str | None:
        return _LOCAL_CLASS_URL.get(name) or _LOCAL_TYPEDEF_URL.get(name)

    # `<pre>…</pre>` blocks only — keeps the substitution from touching
    # inline `<span class="n">` runs that may appear in other contexts.
    _PRE_BLOCK_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL)

    # Relative path from each rendered `.html` file's directory to the
    # Doxygen html tree (which sits alongside `docs_sphinx/html/` at
    # `doc/doxygen/html/`). Reused per file so paths render with the
    # right number of `../` segments regardless of subdir depth.
    _DOXY_ROOT = html_dir.parent.parent / "doc" / "doxygen" / "html"

    def _doxy_rel(html_path: pathlib.Path, file_url: str) -> str:
        target = _DOXY_ROOT / file_url
        try:
            return os.path.relpath(target, start=html_path.parent)
        except ValueError:
            return f"../../../doc/doxygen/html/{file_url}"

    def _wrap_span(m: re.Match) -> str:
        name = m.group("name")
        url = _resolve(name)
        if not url:
            return m.group(0)
        return (f'<a class="reference internal" href="{url}">'
                f'{m.group("prefix")}{name}{m.group("suffix")}</a>')

    def _wrap_cpf(m: re.Match, current_html: pathlib.Path) -> str:
        path = m.group("path")
        # Only opencv headers — `<iostream>`, `<stdio.h>` are not in
        # `_FILE_URL` and stay plain.
        file_url = _FILE_URL.get(path)
        if not file_url:
            return m.group(0)
        href = _doxy_rel(current_html, file_url)
        # Keep the opening/closing quotes outside the `<a>` (so they
        # render as plain `"` / `<`/`>`), and put the link on just
        # the path text — same shape the enum-detail `#include` line
        # already uses elsewhere.
        return (f'{m.group("prefix")}{m.group("openq")}'
                f'<a class="reference external opencv-include-link" '
                f'href="{href}">{path}</a>'
                f'{m.group("closeq")}{m.group("suffix")}')

    def _rewrite_pre(m: re.Match, current_html: pathlib.Path) -> str:
        inner = m.group(1)
        # Skip spans already wrapped: if `<a …>` immediately precedes
        # the `<span class="n">…</span>`, leave it. The Pygments
        # output doesn't generate `<a>` itself, so the only place
        # `<a>` appears in the inner text is from a prior pass — we
        # detect by scanning for `<a `/`</a>` pairs and only rewrite
        # text outside them.
        out: list[str] = []
        i, n = 0, len(inner)
        while i < n:
            if inner.startswith("<a ", i):
                j = inner.find("</a>", i)
                if j < 0:
                    out.append(inner[i:])
                    break
                out.append(inner[i:j + 4])
                i = j + 4
            else:
                k = inner.find("<a ", i)
                if k < 0:
                    seg = inner[i:]
                    seg = _PYG_IDENT_SPAN_RE.sub(_wrap_span, seg)
                    seg = _PYG_CPF_SPAN_RE.sub(
                        lambda mm: _wrap_cpf(mm, current_html), seg)
                    out.append(seg)
                    break
                seg = inner[i:k]
                seg = _PYG_IDENT_SPAN_RE.sub(_wrap_span, seg)
                seg = _PYG_CPF_SPAN_RE.sub(
                    lambda mm: _wrap_cpf(mm, current_html), seg)
                out.append(seg)
                i = k
        return "<pre>" + "".join(out) + "</pre>"

    for html in html_dir.rglob("*.html"):
        try:
            text = html.read_text(encoding="utf-8")
        except OSError:
            continue
        if "<pre>" not in text:
            continue
        new_text = _PRE_BLOCK_RE.sub(
            lambda m: _rewrite_pre(m, html), text)
        if new_text != text:
            try:
                html.write_text(new_text, encoding="utf-8")
            except OSError:
                pass


def _inline_coll_graphs_on_finish(app, exception):
    """build-finished entry point."""
    if exception is not None:
        return
    out = pathlib.Path(app.outdir)
    _inline_collaboration_svgs(out / "api", out / "_images")
    _strip_breathe_class_clutter(out / "api")
    _linkify_code_blocks(out)
    _generate_search_map(out)
