"""API-reference stub writers. Entry point: ``_generate_api_stubs``."""
from __future__ import annotations
import pathlib, re, os as _os, shutil as _shutil, textwrap as _textwrap
from .state import *
from .xml_render import *
from .examples import (
    _find_examples_for_class, _render_examples_block, _generate_example_pages,
)


# Drives write-if-changed and the stale-file sweep.
_stub_written: set[pathlib.Path] = set()


def _stub_write(path: pathlib.Path, content: str) -> None:
    """Write only if changed; mark path live for this run."""
    if not (path.is_file() and path.read_text(encoding="utf-8") == content):
        path.write_text(content, encoding="utf-8")
    _stub_written.add(path)


def _group_by_section_header(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """Split members into contiguous runs sharing a Doxygen `@name` header."""
    groups: list[tuple[str, list[dict]]] = []
    for m in items:
        hdr = m.get("section_header") or ""
        if not groups or groups[-1][0] != hdr:
            groups.append((hdr, []))
        groups[-1][1].append(m)
    return groups


def _collect_all_group_names(node: dict) -> list[str]:
    """Flatten group hierarchy to every group's `name`."""
    return [node["name"]] + [n for c in node["children"]
                             for n in _collect_all_group_names(c)]


def _namespaces_section(entries: list) -> list[str]:
    """`## Namespaces` block of `@subpage` entries; `entries` is `(ns_name, anchor)`."""
    lines = ["## Namespaces", ""]
    for _ns_name, anchor in entries:
        lines.append(f"- @subpage {anchor}")
    lines.append("")
    return lines


def _write_namespace_stub(ns: dict, out_dir: pathlib.Path,
                          xml_dir: pathlib.Path,
                          ns_group_map: dict | None = None,
                          group_info: dict | None = None) -> tuple[str, str]:
    """Write ``api/namespace_<slug>.md`` for one namespace. Returns
    ``(anchor, filename)``.

    The page is an index, not a second home for member detail: it lists the
    namespace's classes and a summary table per member kind, each row linking
    to the canonical ``#<refid>`` anchor that the owning *group* page already
    emits. Deliberately no per-member ``(id)=`` targets here — those live on
    the group pages."""
    slug = ns["name"].replace("::", "__")
    anchor = f"api_ns_{slug}"
    fname = f"namespace_{slug}.md"
    lines = [f"# {ns['name']} namespace {{#{anchor}}}", ""]

    # Breadcrumbs: link back to the group pages that contain this namespace.
    if ns_group_map and group_info:
        crumbs: list[str] = []
        for grp in sorted(ns_group_map.get(ns["name"], set())):
            chain: list[str] = []
            cur: str | None = grp
            while cur and cur in group_info:
                chain.append(cur)
                cur = group_info[cur]["parent"]
            chain.reverse()
            parts = [f"[{group_info[g]['title']}]({g}.md)" for g in chain]
            if parts:
                crumbs.append(" » ".join(parts))
        if crumbs:
            lines += [" | ".join(crumbs), ""]

    if ns.get("brief"):
        lines += [ns["brief"], ""]

    ns_prefix = ns["name"] + "::"
    innerclasses = _namespace_innerclasses(ns["name"], xml_dir)
    if innerclasses:
        lines += ["## Classes", "", "{.api-reference-table}",
                  "| Name | Description |", "|---|---|"]
        for ic_refid, ic_name, ic_kind, ic_brief in innerclasses:
            page = _class_page_name(ic_refid)
            short = ic_name[len(ns_prefix):] if ic_name.startswith(ns_prefix) else ic_name
            lines.append(
                f"| [`{ic_kind} {short}`]({page}.md) | {_md_escape_cell(ic_brief)} |")
        lines.append("")

    ns_sections = _read_namespace_member_sections(ns.get("refid", ""),
                                                  _PATCHED_XML_DIR)

    def _ns_summary_block(section_title: str, members: list) -> list[str]:
        """Summary table for one member kind on a namespace page — same shape
        as the group-page blocks, but links target the canonical `#refid`
        anchor on the owning group page (namespaces don't re-emit targets)."""
        out: list[str] = []
        if section_title == "Functions":
            out += ["{.api-reference-table .api-function-table}",
                    "| Return | Name | Description |", "|---|---|---|"]
            for m in members:
                ret = _md_escape_cell(m["type"]) or "&nbsp;"
                label = f"{m['name']}{_md_escape_cell(m['args'])}"
                out.append(
                    f"| `{ret}` | [`{label}`](#{m['id']}) | {_md_escape_cell(m['brief'])} |")
        elif section_title == "Typedefs":
            out += ["{.api-typedef-table}",
                    "| Type | Name | Description |", "|---|---|---|"]
            for m in members:
                t = _md_escape_cell(m["type"]) or "&nbsp;"
                qname = f"{ns['name']}::{m['name']}"
                out.append(
                    f"| typedef {t} | [`{qname}`](#{m['id']}) | "
                    f"{_md_escape_cell(m['brief'])} |")
        elif section_title == "Variables":
            out += ["{.api-reference-table}",
                    "| Type | Name | Description |", "|---|---|---|"]
            for m in members:
                t = _md_escape_cell(m["type"]) or "&nbsp;"
                out.append(
                    f"| `{t}` | [`{m['name']}`](#{m['id']}) | {_md_escape_cell(m['brief'])} |")
        elif section_title == "Enumerations":
            for m in members:
                if m["brief"]:
                    out.append(_md_escape_cell(m["brief"]))
                    out.append("")
                out.append("```cpp")
                out.extend(_enum_synopsis_lines(m))
                out.append("```")
                out.append("")
        else:  # Macros
            out += ["{.api-reference-table}", "| Name | Description |", "|---|---|"]
            for m in members:
                out.append(
                    f"| [`{m['name']}`](#{m['id']}) | {_md_escape_cell(m['brief'])} |")
        return out

    # Standard sections list only ungrouped members; @name groups get their own
    # `##` section after the standard ones (same layout as the group pages).
    _ns_named_groups: list[tuple[str, str, list]] = []
    for kind_key, section_title in _MEMBERDEF_SECTIONS:
        items = ns_sections.get(section_title, [])
        if not items:
            continue
        ungrouped = [m for m in items if not (m.get("section_header") or "")]
        for _hdr, _members in _group_by_section_header(
                [m for m in items if (m.get("section_header") or "")]):
            _ns_named_groups.append((_hdr, section_title, _members))
        if ungrouped:
            lines.append(f"## {section_title}")
            lines.append("")
            lines += _ns_summary_block(section_title, ungrouped)
            lines.append("")
    for _hdr, section_title, _members in _ns_named_groups:
        lines.append(f"## {_hdr}")
        lines.append("")
        lines += _ns_summary_block(section_title, _members)
        lines.append("")

    # Namespace's own prose; bare `{doxygennamespace}` would re-dump members.
    if ns.get("detailed"):
        lines += ["## Detailed Description", "", ns["detailed"], ""]

    _stub_write(out_dir / fname, "\n".join(lines) + "\n")
    return anchor, fname


def _write_api_stub(node: dict, out_dir: pathlib.Path,
                    classes_seen: dict, ns_map: dict | None = None) -> None:
    """Write one .md per group node, recursing into children.

    Parent groups → @subpage index pages; leaf groups → summary tables + detail
    blocks. Inner classes populate `classes_seen` for later page emission."""
    name = node["name"]
    title = node["title"]
    out = out_dir / f"{name}.md"

    lines = [f"# {title} {{#api_{name}}}", ""]

    if node["children"]:
        # Navigation index page: intro prose, namespaces, then @subpage topics
        # (_subpage_list_to_toctree converts them to a real toctree).
        lines = [f"# {title} {{#api_{name}}}", ""]
        if node["detailed"]:
            lines += [node["detailed"], ""]
        if ns_map and ns_map.get(name):
            lines += _namespaces_section(ns_map[name])
        lines += ["## Topics", ""]
        for child in node["children"]:
            lines.append(f"- @subpage api_{child['name']}")
        _stub_write(out, "\n".join(lines) + "\n")
        for child in node["children"]:
            _write_api_stub(child, out_dir, classes_seen, ns_map)
        return

    # ---- Leaf page ----------------------------------------------------------
    lines = [f"# {title} {{#api_{name}}}", ""]
    if ns_map and ns_map.get(name):
        lines += _namespaces_section(ns_map[name])

    if node["innerclasses"]:
        lines += ["## Classes", "", "{.api-reference-table}",
                  "| Name | Description |", "|---|---|"]
        for c in node["innerclasses"]:
            classes_seen.setdefault(c["refid"], c)
            page = _class_page_name(c["refid"])
            link = f"[`{c['kind']} {c['name']}`]({page}.md)"
            lines.append(f"| {link} | {_md_escape_cell(c['brief'])} |")
        lines.append("")

    # Class members listed in group sections render on the class page.
    class_qualifieds = {c.get("qualified") for c in classes_seen.values()
                        if c.get("qualified")}

    def _is_class_member(m: dict) -> bool:
        q = m.get("qualified") or ""
        if "::" not in q:
            return False
        parent = q.rsplit("::", 1)[0]
        return parent in class_qualifieds

    def _is_template_spec(m: dict) -> bool:
        # breathe's C++ parser rejects `<…>` names; skip detail block.
        return "<" in (m.get("name") or "")


    # Class members lack an in-page anchor; link to the class page.
    def _member_anchor_link(m: dict, label: str) -> str:
        if _is_class_member(m):
            q = m["qualified"]
            parent_qualified = q.rsplit("::", 1)[0]
            for c in classes_seen.values():
                if c.get("qualified") == parent_qualified:
                    return f"[`{label}`]({_class_page_name(c['refid'])}.md)"
        return f"[`{label}`](#{m['id']})"

    # Renders the summary table (or enum synopsis) for one member kind given a
    # list of members — used both for the standard per-kind sections and for
    # the @name-group sections appended afterwards. Returns markdown lines
    # (no `## heading`); enum output already carries its own trailing blanks.
    _rich_return = (name == "core_basic")

    def _summary_block(section_title: str, members: list) -> list[str]:
        out: list[str] = []
        if section_title == "Functions":
            out += ["{.api-reference-table .api-function-table}",
                    "| Return | Name | Description |", "|---|---|---|"]
            for m in members:
                ret_type = _md_escape_cell(m["type"]) or "&nbsp;"
                label = f"{m['name']}{_md_escape_cell(m['args'])}"
                sig_link = _member_anchor_link(m, label)
                if _rich_return:
                    # Hide the Doxygen-visible CV_EXPORTS* macro (live docs do).
                    ret_type = re.sub(r"^CV_EXPORTS(?:_[A-Z]+)?\s+", "", ret_type)
                    storage = "static " if m.get("static") else ""
                    if m.get("template"):
                        ret = f"`{m['template']}`<br>`{storage}{ret_type}`"
                    else:
                        ret = f"`{storage}{ret_type}`"
                else:
                    ret = f"`{ret_type}`"
                out.append(f"| {ret} | {sig_link} | {_md_escape_cell(m['brief'])} |")
        elif section_title == "Typedefs":
            # Typedef summary: two-column card (`.api-typedef-table`). The Type
            # cell leads with the literal `typedef ` keyword (no backticks — the
            # td is already monospace via CSS) and the Name cell shows the
            # fully-qualified `cv::<alias>` (e.g. `typedef Matx<…>` | `cv::Matx33f`).
            out += ["{.api-typedef-table}",
                    "| Type | Name | Description |", "|---|---|---|"]
            for m in members:
                t = _md_escape_cell(m["type"]) or "&nbsp;"
                name_link = _member_anchor_link(m, f"cv::{m['name']}")
                out.append(f"| typedef {t} | {name_link} | {_md_escape_cell(m['brief'])} |")
        elif section_title == "Variables":
            out += ["{.api-reference-table}",
                    "| Type | Name | Description |", "|---|---|---|"]
            for m in members:
                t = _md_escape_cell(m["type"]) or "&nbsp;"
                name_link = _member_anchor_link(m, m["name"])
                out.append(f"| `{t}` | {name_link} | {_md_escape_cell(m['brief'])} |")
        elif section_title == "Enumerations":
            # Code-style synopsis (Doxygen layout) instead of name/desc table.
            # On most group pages we emit the synopsis only — the per-value
            # initializer list is already self-explanatory. On core_basic we
            # additionally append a "More..." link, inline at the end of
            # the brief description (or alone below the synopsis when no
            # brief exists), pointing to that enum's detail block in the
            # "Enumeration Type Documentation" section emitted by the
            # detail loop below.
            _enum_more_link = (name == "core_basic")
            # On core_basic the synopsis is emitted as raw HTML (NOT a
            # ```cpp code fence) so every `cv::…` token becomes its own
            # `<a>` linking to the enum's detail block. The user's spec
            # is "anything starting with cv:: is clickable, blue in
            # light mode, and the `=initializer` tail must NOT be part
            # of the link." We hand-roll Pygments-style spans (`k`, `n`,
            # `p`) so the existing `.highlight pre` styling kicks in and
            # the synopsis still looks like a code block.
            _clickable_synopsis = (name == "core_basic")
            import html as _html_mod
            for m in members:
                _more = ""
                if _enum_more_link:
                    # Link to the enum detail block's heading-slug id
                    # (`### AccessFlag` → `#accessflag`). Same target
                    # the clickable synopsis tokens use, and a literal
                    # match on the actual element id on the page —
                    # whereas `#_CPPv4…E` would be slugified to
                    # `cppv4…e` by MyST and the link would resolve
                    # nowhere.
                    _more = f"[More...](#{m['name'].lower()})"
                # Synopsis block.
                if _clickable_synopsis:
                    _qual = m["qualified"] or m["name"]
                    _is_strong = bool(m.get("strong"))
                    # Scoped enums render as `enum struct` in the
                    # summary synopsis (matches the user's spec for the
                    # Param block — Doxygen reports `strong="yes"` for
                    # `enum class` declarations, and the displayed
                    # keyword on this page should be `enum struct`).
                    _keyword = "enum struct" if _is_strong else "enum"
                    # Enumerator-name prefix: scoped → "cv::EnumName::",
                    # unscoped → the enum's parent scope (so values
                    # render as `cv::ACCESS_READ`, matching the live
                    # Doxygen page).
                    if _is_strong:
                        _val_prefix = _qual + "::"
                    elif "::" in _qual:
                        _val_prefix = _qual.rsplit("::", 1)[0] + "::"
                    else:
                        _val_prefix = ""
                    _href = f"#{m['name'].lower()}"  # enum detail block id
                    # HTML-entity-encode the `::` separator inside the
                    # anchor TEXT. `_translate`'s `_linkify_cv_symbols`
                    # pass (translate.py) runs after this raw HTML is
                    # written and wraps every `cv::Name` text it sees in
                    # an external `docs.opencv.org` `<a>` — that would
                    # nest a second anchor inside ours and steal the
                    # click. Encoding the colons keeps the displayed
                    # text identical (`cv::…` in the browser) while
                    # making the regex `cv(?:\.|::)` skip past it.
                    def _safe(s: str) -> str:
                        return _html_mod.escape(s).replace("::", "&#58;&#58;")
                    out.append(
                        '<div class="highlight-cpp notranslate '
                        'opencv-enum-clickable"><div class="highlight"><pre>'
                    )
                    out.append(
                        f'<span class="k">{_html_mod.escape(_keyword)}</span> '
                        f'<a class="reference internal" href="{_href}">'
                        f'<span class="n">{_safe(_qual)}</span></a> '
                        f'<span class="p">{{</span>'
                    )
                    _vals = m.get("enum_values") or []
                    for _i, _v in enumerate(_vals):
                        _comma = ('<span class="p">,</span>'
                                  if _i < len(_vals) - 1 else '')
                        _init = (' ' + _html_mod.escape(_v["initializer"])
                                 if _v.get("initializer") else '')
                        _full = _val_prefix + _v["name"]
                        out.append(
                            f'    <a class="reference internal" href="{_href}">'
                            f'<span class="n">{_safe(_full)}</span></a>'
                            f'{_init}{_comma}'
                        )
                    out.append('<span class="p">}</span></pre></div></div>')
                    # Blank line closes the HTML block per CommonMark
                    # rule 7. Without it the description + "More..."
                    # markdown line that follows would be swallowed as
                    # raw text continuation of the synopsis div, leaving
                    # `[More...](#…)` visible verbatim instead of as a
                    # rendered link.
                    out.append("")
                else:
                    out.append("```cpp")
                    out.extend(_enum_synopsis_lines(m))
                    out.append("```")
                if m["brief"]:
                    line = _md_escape_cell(m["brief"])
                    if _more:
                        line = f"{line} {_more}"
                        _more = ""
                    out.append(line)
                if _more:
                    out.append(_more)
                out.append("")
        else:  # Macros
            out += ["{.api-reference-table}", "| Name | Description |", "|---|---|"]
            for m in members:
                name_link = _member_anchor_link(m, m["name"])
                out.append(f"| {name_link} | {_md_escape_cell(m['brief'])} |")
        return out

    # Standard per-kind summary sections list only the *ungrouped* members (no
    # Doxygen `@name` header). Members that DO carry an `@name` header — e.g.
    # the "Shorter aliases for the most popular specializations of Vec<T,n>"
    # group — are pulled out and rendered as their own `##` sections AFTER the
    # standard sections (per the requested layout: typedefs, then functions,
    # then each named group with its members).
    _named_groups: list[tuple[str, str, list]] = []   # (header, section_title, members)
    for _, section_title in _MEMBERDEF_SECTIONS:
        items = node["sections"].get(section_title, [])
        if not items:
            continue
        ungrouped = [m for m in items if not (m.get("section_header") or "")]
        for _hdr, _members in _group_by_section_header(
                [m for m in items if (m.get("section_header") or "")]):
            _named_groups.append((_hdr, section_title, _members))
        if ungrouped:
            lines.append(f"## {section_title}")
            lines.append("")
            lines += _summary_block(section_title, ungrouped)
            lines.append("")
    for _hdr, section_title, _members in _named_groups:
        lines.append(f"## {_hdr}")
        lines.append("")
        lines += _summary_block(section_title, _members)
        lines.append("")

    # Detail blocks via `_render_member_detail` (breathe chokes); macros keep
    # `{doxygendefine}`; enums/class-members/template-specs skipped.
    seen_define_names: set[str] = set()
    for kind_key, section_title in _MEMBERDEF_SECTIONS:
        items = node["sections"].get(section_title, [])
        if not items:
            continue
        # Enum detail blocks are emitted only on core_basic — every other
        # group page is content with the summary-only synopsis. The
        # detail block provides the `_CPPv4…` anchor target for the
        # "More..." link added by the summary above.
        if kind_key == "enum" and name != "core_basic":
            continue
        # core_basic funcs: count overloads first for `[i/n]` headings.
        _core_basic_funcs = (name == "core_basic" and kind_key == "function")
        _ov_total: dict[str, int] = {}
        _ov_idx: dict[str, int] = {}
        _slug_seen: set[str] = set()
        if _core_basic_funcs:
            for m in items:
                if _is_class_member(m) or _is_template_spec(m):
                    continue
                _ov_total[m["name"]] = _ov_total.get(m["name"], 0) + 1
        blocks: list[list[str]] = []
        for m in items:
            # Class members render on their own class page; skip on the group.
            if kind_key in ("function", "variable") and _is_class_member(m):
                continue
            # Template specializations carry `<…>`; summary table still lists them.
            if _is_template_spec(m):
                continue
            if _core_basic_funcs:
                short = m["name"]
                _ov_idx[short] = _ov_idx.get(short, 0) + 1
                slug = _func_slug(short)
                emit_anchor = slug not in _slug_seen
                _slug_seen.add(slug)
                blocks.append(_render_core_basic_func(
                    m, _ov_idx[short], _ov_total.get(short, 1), emit_anchor))
                continue
            if kind_key == "enum":
                # core_basic-only path (gated by the early `continue` above).
                # Hand-rolled in place of `{doxygenenum}` — breathe's directive
                # drops every enumerator's initializer and `briefdescription`
                # (renders the `<dd>` empty), so the live page's per-value
                # `=1<<24` constants and one-line descriptions vanished.
                # Pulling from the XML metadata ourselves restores them.
                _qual = m["qualified"] or m["name"]
                _eid = _sphinx_cpp_v4_id(_qual)        # "More..." target
                _is_strong = bool(m.get("strong"))
                _keyword = "enum class" if _is_strong else "enum"
                # Heading is just the short name (`AccessFlag`,
                # `DataLayout`, …) — matching the live page's per-enum
                # title. The `enum cv::Foo` signature line lives in the
                # body below.
                # Signature line emitted as raw HTML rather than a
                # markdown code span: we need the FULL `cv::EnumName` to
                # be a single clickable anchor, but translate step 8g's
                # auto-tokenizer would only wrap the bare `EnumName`
                # part (leaving `cv::` outside the link). Hand-rolling
                # the `<code><a>cv::EnumName</a></code>` ourselves
                # bypasses the tokenizer; the `opencv-enum-sig` class
                # on the `<code>` keeps the existing light-mode-blue
                # CSS rule applicable.
                _enum_href = f"#{m['name'].lower()}"
                blk: list[str] = [
                    f"({_eid})=",
                    f"### {m['name']}",
                    "",
                    f'<code class="docutils literal notranslate opencv-enum-sig">'
                    f'{_keyword} <a class="reference internal" '
                    f'href="{_enum_href}">{_qual}</a></code>',
                    "",
                ]
                # `#include <…>` line — the live Doxygen page shows this
                # immediately under the signature, with the header path
                # rendered as a blue link to its Doxygen file page. The
                # link target comes from the tagfile's `<compound
                # kind="file">` entries via `_FILE_URL`; the prefix
                # `../../../doc/doxygen/html/` matches how the navbar
                # reaches the Doxygen tree from an `api/<page>.html`
                # (depth-1 page → 3 levels up to the build root).
                if m.get("include_file"):
                    _ipath = m["include_file"]
                    _ifile = _FILE_URL.get(_ipath)
                    if _ifile:
                        _href = f"../../../doc/doxygen/html/{_ifile}"
                        # The `opencv-include-link` class lets the
                        # light-mode CSS rule beat the generic
                        # `code > a { color: inherit }` cascade in
                        # custom.css (which would otherwise drop the
                        # link's blue color).
                        blk += [
                            f'<code class="docutils literal notranslate">'
                            f'#include &lt;<a class="reference external '
                            f'opencv-include-link" '
                            f'href="{_href}">{_ipath}</a>&gt;</code>',
                            "",
                        ]
                    else:
                        blk += [f"`#include <{_ipath}>`", ""]
                if m.get("brief"):
                    blk += [m["brief"], ""]
                if m.get("detailed"):
                    blk += [m["detailed"], ""]
                _vals = m.get("enum_values") or []
                if _vals:
                    blk += ["**Enumerator:**", "",
                            "| | |", "|---|---|"]
                    for _v in _vals:
                        _nm = _v["name"]
                        _init = _v.get("initializer") or ""
                        _vbrief = (_v.get("brief") or "").replace("|", "\\|").replace("\n", " ")
                        # Cell shows the enumerator name (as a code chip)
                        # and, when a Python binding exists, the
                        # `Python: cv.NAME` line below it. The C++
                        # initializer (`=1<<24`, `= 0`, `=ACCESS_RW`,
                        # …) is intentionally omitted — it adds noise
                        # without conveying anything readers can't get
                        # from the synopsis above.
                        _cell = f"`{_nm}`"
                        _py = _python_enum_name(_qual, _nm, _is_strong)
                        if _py:
                            _cell = f"{_cell}<br>Python: `{_py}`"
                        blk.append(f"| {_cell} | {_vbrief} |")
                    blk.append("")
                blocks.append(blk)
                continue
            if kind_key == "define":
                # Macros aren't namespaced; dedupe arity-overloaded ones.
                if m["name"] in seen_define_names:
                    continue
                seen_define_names.add(m["name"])
                # No `(id)=`: {doxygendefine} already registers the target.
                blocks.append([
                    f"```{{doxygendefine}} {m['name']}",
                    ":project: opencv",
                    "```",
                    "",
                ])
            else:
                blocks.append(
                    _render_member_detail(m, m["qualified"] or m["name"]))
        if not blocks:
            continue
        lines.append(f"## {_MEMBER_DETAIL_SECTION[section_title]}")
        lines.append("")
        for b in blocks:
            lines += b

    # Hidden toctree registers per-class pages in the sidebar.
    if node["innerclasses"]:
        lines += ["```{toctree}", ":hidden:", ":maxdepth: 1", ""]
        for c in node["innerclasses"]:
            lines.append(_class_page_name(c["refid"]))
        lines += ["```", ""]

    _stub_write(out, "\n".join(lines) + "\n")


# sectiondef kind → summary heading, in Doxygen order.
_CLASS_SUMMARY_SECTIONS = [
    ("public-type",             "Public Types"),
    ("public-func",             "Public Member Functions"),
    ("public-static-func",      "Static Public Member Functions"),
    ("public-attrib",           "Public Attributes"),
    ("public-static-attrib",    "Static Public Attributes"),
    ("protected-type",          "Protected Types"),
    ("protected-func",          "Protected Member Functions"),
    ("protected-static-func",   "Static Protected Member Functions"),
    ("protected-attrib",        "Protected Attributes"),
    ("protected-static-attrib", "Static Protected Attributes"),
    ("friend",                  "Friends"),
]


def _render_member_detail(m: dict, full_name: str) -> list[str]:
    """Render one member's detail block from XML (no breathe; it chokes).

    `full_name` is the declaration name; `(id)=` keeps `#refid` links working."""
    short = m["name"]
    kind = m["kind"]
    head = short + (m.get("args", "") if kind == "function" else "")
    out = [f"({m['id']})=", f"### {head}".rstrip(), ""]

    # Declaration (template line, if any, then the C++ signature).
    tmpl = m.get("template") or ""
    prefix = "static " if m.get("static") else ""
    typ = (m.get("type") or "").strip()
    if kind == "typedef":
        decl = f"typedef {typ} {full_name}".strip()
    elif kind == "function":
        decl = (f"{prefix}{typ + ' ' if typ else ''}"
                f"{full_name}{m.get('args', '')}").strip()
    else:  # variable / attribute
        decl = f"{prefix}{typ + ' ' if typ else ''}{full_name}".strip()
    # Template clause + declaration as inline code (keeps token-linkifier active).
    _sig = ([f"`{tmpl}`"] if tmpl else []) + [f"`{decl}`"]
    out += ["\\\n".join(_sig), ""]

    # `#include <…>` card row, like docs.opencv.org.
    inc = (m.get("include_file") or "").strip()
    if inc:
        out += ["{.opencv-api-include}", f"`#include <{inc}>`", ""]

    if m.get("brief"):
        out += [m["brief"], ""]
    if m.get("detailed"):
        out += [m["detailed"], ""]
    if m.get("params"):
        out += ["**Parameters**", ""]
        for nm, desc in m["params"]:
            out.append(f"- `{nm}` — {desc}" if desc else f"- `{nm}`")
        out.append("")
    if m.get("returns"):
        out += ["**Returns**", "", m["returns"], ""]
    return out


def _render_core_basic_func(m: dict, idx: int, total: int,
                            emit_anchor: bool) -> list[str]:
    """Hand-rolled Function block for api/core_basic (breathe can't parse it).

    Signature is inline code for token-linkifier (translate step 8g); heading
    `{#cv-slug}` anchor (first overload) is the Functions-table target (step 8i)."""
    short = m["name"]
    slug = _func_slug(short)
    suffix = f" [{idx}/{total}]" if total > 1 else ""
    head = f"### {short}(){suffix}"
    out = [f"{head} {{#{slug}}}" if emit_anchor else head, ""]
    # Template clause + signature as inline code (keeps token-linkifier active).
    ret = re.sub(r"^CV_EXPORTS(?:_[A-Z]+)?\s+", "", m.get("type") or "")
    storage = ("static " if m.get("static") else "") \
        + ("inline " if m.get("inline") else "")
    qname = m["qualified"] or m["name"]
    _sig = ([f"`{m['template']}`"] if m.get("template") else []) + \
        [f"`{storage}{ret} {qname}{m['args']}`"]
    out += ["\\\n".join(_sig), ""]
    if m.get("include_file"):
        out += ["{.opencv-api-include}",
                f"`#include <{m['include_file']}>`", ""]
    if m.get("brief"):
        out += [m["brief"], ""]
    if m.get("detailed"):
        out += [m["detailed"], ""]
    if m.get("params"):
        out += ["**Parameters**", ""]
        for nm, desc in m["params"]:
            out.append(f"- `{nm}` — {desc}" if desc else f"- `{nm}`")
        out.append("")
    if m.get("returns"):
        out += [f"**Returns** — {m['returns']}", ""]
    return out


def _write_class_stub(cls: dict, out_dir: pathlib.Path,
                      xml_dir: pathlib.Path) -> None:
    """One .md per inner class, mirroring Doxygen's class-page layout.

    Falls back to `{doxygenclass}`/`{doxygenstruct}` if class XML can't be read."""
    page = _class_page_name(cls["refid"])
    out = out_dir / f"{page}.md"
    qualified = cls["qualified"] or cls["name"]
    kind_label = cls["kind"].title()
    title = f"{kind_label} {qualified}"
    # No `{#refid}` anchor; `_generate_api_stubs` seeds `_ANCHOR_TO_DOC` instead.
    lines = [f"# {title}", ""]

    # Class-page header: brief + `More...` + `#include` line.
    _header_data = _read_class_data(cls["refid"], xml_dir)
    if _header_data is not None:
        import html as _html_pkg
        _brief = (_header_data.get("brief") or "").strip()
        if _brief:
            # `More...` only when there's a detailed description to jump to.
            _more = (
                ' <a class="opencv-class-more" href="#detailed-description">More...</a>'
                if _header_data.get("detailed") else ""
            )
            lines.append(
                f'<p class="opencv-class-brief">'
                f'{_html_pkg.escape(_brief)}{_more}</p>'
            )
            lines.append("")
        _inc = (_header_data.get("include") or "").strip()
        if _inc:
            lines.append(
                f'<div class="opencv-class-include">'
                f'<code>#include &lt;{_html_pkg.escape(_inc)}&gt;</code></div>'
            )
            lines.append("")

    # Collaboration diagram: reuse legacy Doxygen HTML build's SVG.
    _svg = _find_collaboration_svg(cls["refid"], xml_dir.parent / "html")
    _light_name = _dark_name = None
    if _svg is not None:
        import hashlib as _hashlib
        try:
            _raw = _svg.read_text(encoding="utf-8")
            # Light/dark variants; content-hashed names bust browser caches.
            _light_txt = _svg_make_transparent(_raw)
            _dark_txt = _svg_dark_variant(_raw)
            _lh = _hashlib.md5(_light_txt.encode("utf-8")).hexdigest()[:10]
            _dh = _hashlib.md5(_dark_txt.encode("utf-8")).hexdigest()[:10]
            _light_name = f"{_svg.stem}.{_lh}.svg"
            _dark_name = f"{_svg.stem}.{_dh}.dark.svg"
            (out_dir / _light_name).write_text(_light_txt, encoding="utf-8")
            (out_dir / _dark_name).write_text(_dark_txt, encoding="utf-8")
            # Register so the stale-file sweep keeps them.
            _stub_written.add(out_dir / _light_name)
            _stub_written.add(out_dir / _dark_name)
        except OSError:
            _light_name = _dark_name = None
    if _light_name is not None:
        # only-light/only-dark: pydata theme-aware image classes.
        lines += [
            f"Collaboration diagram for {qualified}:",
            "",
            f"![Collaboration diagram for {qualified}]({_light_name})"
            "{.opencv-coll-graph .only-light}",
            "",
            f"![Collaboration diagram for {qualified}]({_dark_name})"
            "{.opencv-coll-graph .only-dark}",
            "",
        ]

    data = _read_class_data(cls["refid"], xml_dir)
    if data is None:  # missing XML
        directive = "doxygenstruct" if cls["kind"] == "struct" else "doxygenclass"
        lines += [
            f"```{{{directive}}} {qualified}",
            ":project: opencv",
            ":members:",
            ":protected-members:",
            ":undoc-members:",
            "```",
            "",
        ]
        _stub_write(out, "\n".join(lines))
        return

    # 1) Summary tables in Doxygen's order.
    for sd_kind, summary_title in _CLASS_SUMMARY_SECTIONS:
        items = data["sections"].get(sd_kind, [])
        if not items:
            continue
        lines.append(f"## {summary_title}")
        lines.append("")
        non_enum_items = [m for m in items if m["kind"] != "enum"]
        enum_items = [m for m in items if m["kind"] == "enum"]
        if non_enum_items:
            lines += ["{.api-reference-table}",
                      "| Return | Name | Description |", "|---|---|---|"]
            for m in non_enum_items:
                ret = _md_escape_cell(m["type"]) or "&nbsp;"
                if m["static"]:
                    ret = "static " + ret
                sig = f"{m['name']}{_md_escape_cell(m['args'])}"
                sig_link = f"[`{sig}`](#{m['id']})"
                lines.append(
                    f"| `{ret}` | {sig_link} | {_md_escape_cell(m['brief'])} |")
            lines.append("")
        for m in enum_items:
            if m["brief"]:
                lines.append(_md_escape_cell(m["brief"]))
                lines.append("")
            # HTML synopsis: `<a>` ids match the `_CPPv4…` detail anchors.
            lines.extend(_enum_synopsis_html(m, strip_scope=qualified))
            lines.append("")

    # 2) Detailed Description via Breathe (description-only; no
    #    breathe_default_members). `:no-members:` is NOT valid Breathe. Breathe's
    #    duplicate header stripped later by `_strip_breathe_class_clutter`.
    _directive = "doxygenstruct" if cls["kind"] == "struct" else "doxygenclass"
    examples = _find_examples_for_class(qualified.rsplit("::", 1)[-1])
    if data["detailed"]:
        # Register this class as one whose stub renders a "Detailed
        # Description" section so translator step 8e knows to keep the
        # "More..." link on its Classes-table row (otherwise the link
        # would land at the top of a page with nothing to "expand").
        _CLASSES_WITH_DETAIL.add(_class_page_name(cls["refid"]))
        lines += [
            "## Detailed Description",
            "",
            f"```{{{_directive}}} {qualified}",
            ":project: opencv",
            "```",
            "",
        ]
        lines += _render_examples_block(examples)
    elif examples:
        lines += ["## Examples", ""]
        lines += _render_examples_block(examples)

    # 3) Per-member detail blocks (ctor/dtor split from other functions).
    class_simple = qualified.rsplit("::", 1)[-1]

    typedef_items: list[dict] = []
    enum_items_all: list[dict] = []
    ctor_dtor_items: list[dict] = []
    func_items: list[dict] = []
    var_items: list[dict] = []
    for sd_items in data["sections"].values():
        for m in sd_items:
            if m["kind"] == "typedef":
                typedef_items.append(m)
            elif m["kind"] == "enum":
                enum_items_all.append(m)
            elif m["kind"] == "function":
                if m["name"] == class_simple or m["name"] == f"~{class_simple}":
                    ctor_dtor_items.append(m)
                else:
                    func_items.append(m)
            elif m["kind"] == "variable":
                var_items.append(m)

    if typedef_items:
        lines += ["## Member Typedef Documentation", ""]
        for m in typedef_items:
            lines += _render_member_detail(m, f"{qualified}::{m['name']}")

    if enum_items_all:
        # Target of the Public Types synopsis links.
        import html as _html
        lines += ["## Member Enumeration Documentation", ""]
        for m in enum_items_all:
            lines.append(f"({m['id']})=")  # legacy MyST anchor for old @ref
            enum_qualified = m.get("qualified") or m["name"]
            enum_id = _sphinx_cpp_v4_id(enum_qualified)
            enum_short = enum_qualified.rsplit("::", 1)[-1]
            lines.append(
                f'<h3 class="opencv-enum-heading" id="{enum_id}">'
                f'enum <span class="opencv-enum-name">{_html.escape(enum_short)}</span></h3>'
            )
            if m["brief"]:
                lines.append(f"<p>{_html.escape(_md_escape_cell(m['brief']))}</p>")
            # Each `<dt>` carries its own id for per-value linking.
            lines.append('<dl class="opencv-enum-detail">')
            for _v in (m.get("enum_values") or []):
                val_id = _sphinx_cpp_v4_id(f"{enum_qualified}::{_v['name']}")
                init = _html.escape(_v["initializer"]) if _v["initializer"] else ""
                init_html = f' <span class="opencv-enum-init">{init}</span>' if init else ""
                _py = _python_enum_name(enum_qualified, _v["name"],
                                        bool(m.get("strong")))
                py_html = (f' <span class="opencv-enum-pyname">Python: '
                           f'<code>{_html.escape(_py)}</code></span>') if _py else ""
                lines.append(
                    f'  <dt id="{val_id}">'
                    f'<span class="opencv-enum-name">{_html.escape(_v["name"])}</span>'
                    f'{init_html}{py_html}</dt>'
                )
                brief = (_v.get("brief") or "").strip()
                if brief:
                    lines.append(f'  <dd>{_html.escape(brief)}</dd>')
            lines.append('</dl>')
            lines.append("")

    # Dedupe by refid (a memberdef can span sectiondefs).
    def _dedupe(items: list[dict]) -> list[dict]:
        seen, out = set(), []
        for m in items:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            out.append(m)
        return out

    if ctor_dtor_items:
        lines += ["## Constructor & Destructor Documentation", ""]
        for m in _dedupe(ctor_dtor_items):
            lines += _render_member_detail(m, f"{qualified}::{m['name']}")

    if func_items:
        lines += ["## Member Function Documentation", ""]
        for m in _dedupe(func_items):
            lines += _render_member_detail(m, f"{qualified}::{m['name']}")

    if var_items:
        lines += ["## Member Data Documentation", ""]
        for m in _dedupe(var_items):
            lines += _render_member_detail(m, f"{qualified}::{m['name']}")

    _stub_write(out, "\n".join(lines))


def _generate_api_stubs(modules, xml_dir, out_dir):
    """Generate the api/ stub tree: group/namespace pages, then class pages."""
    if not modules:
        return
    if not xml_dir.is_dir():
        return  # No XML yet; degrade silently.

    # Freshness guard: skip rebuild if tree newer than XML and has ns stubs.
    src_index = xml_dir / "index.xml"
    root_md = out_dir / "api_root.markdown"
    if (src_index.is_file() and root_md.is_file()
            and root_md.stat().st_mtime >= src_index.stat().st_mtime
            and any(p.name.startswith("namespace_") and p.suffix == ".md"
                    for p in out_dir.iterdir())):
        for stub in out_dir.iterdir():
            n = stub.name
            if n.endswith(".md") and (n.startswith("class") or n.startswith("struct")):
                _ANCHOR_TO_DOC[n[:-3]] = f"api/{n[:-3]}"
        return

    import shutil
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    global _stub_written
    _stub_written = set()
    root_lines = [
        "API Reference {#api_root}",
        "=============",
        "",
        "Sphinx-rendered API reference for OpenCV main modules. Each entry",
        "below is a module's umbrella `@defgroup`; sub-pages mirror the",
        "Doxygen subgroup hierarchy.",
        "",
    ]
    classes_seen: dict[str, dict] = {}
    # Pass 1 — accumulate group_info + ns_group_map across all modules so
    # namespaces in multiple modules (e.g. cv::traits) get all breadcrumbs.
    global_group_info: dict[str, dict] = {}
    global_ns_group_map: dict[str, set] = {}
    trees: list = []
    for m in modules:
        tree = _build_api_hierarchy("group__" + m.replace("_", "__"), xml_dir)
        if tree is None:
            continue
        trees.append(tree)
        root_lines.append(f"- @subpage api_{tree['name']}")
        all_group_names = _collect_all_group_names(tree)
        all_refids = ["group__" + n.replace("_", "__") for n in all_group_names]
        for ns_name, grps in _build_ns_group_map(all_refids, xml_dir).items():
            global_ns_group_map.setdefault(ns_name, set()).update(grps)
        def _flatten(node: dict, parent: str | None) -> None:
            global_group_info[node["name"]] = {"title": node["title"], "parent": parent}
            for child in node.get("children", []):
                _flatten(child, node["name"])
        _flatten(tree, None)
    # Pass 2 — write namespace stubs (once each) then group stubs.
    written_ns: set[str] = set()
    for tree in trees:
        all_group_names = _collect_all_group_names(tree)
        ns_map: dict[str, list] = {}
        for group_name in all_group_names:
            for ns in _namespaces_for_group(group_name, xml_dir, global_ns_group_map):
                anchor = f"api_ns_{ns['name'].replace('::', '__')}"
                if ns["name"] not in written_ns:
                    _write_namespace_stub(ns, out_dir, xml_dir,
                                          global_ns_group_map, global_group_info)
                    written_ns.add(ns["name"])
                ns_map.setdefault(group_name, []).append((ns["name"], anchor))
        _write_api_stub(tree, out_dir, classes_seen, ns_map)
    # Per-class pages; seed `_ANCHOR_TO_DOC` refid→docname for `@ref`.
    for cls in classes_seen.values():
        _write_class_stub(cls, out_dir, xml_dir)
        _ANCHOR_TO_DOC[cls["refid"]] = f"api/{_class_page_name(cls['refid'])}"
    _stub_write(out_dir / "api_root.markdown", "\n".join(root_lines) + "\n")
    # Sweep stale files.
    for _p in list(out_dir.iterdir()):
        if _p.is_file() and _p not in _stub_written:
            _p.unlink()
    # Flush per-sample example pages now to avoid orphans.
    _generate_example_pages(out_dir.parent / "examples")
