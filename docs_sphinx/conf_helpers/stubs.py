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
    """Write api/namespace_<slug>.md for one namespace. Returns (anchor, fname)."""
    import xml.etree.ElementTree as _ET
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

    # Read member sections from patched XML (has inlined group memberdefs).
    ns_sections: dict[str, list[dict]] = {}
    ns_xml_path = _PATCHED_XML_DIR / f"{ns['refid']}.xml" if ns.get("refid") else None
    if ns_xml_path and ns_xml_path.is_file():
        try:
            cd_ns = _ET.parse(ns_xml_path).getroot().find("compounddef")
            if cd_ns is not None:
                ns_pfx = ns["name"] + "::"
                for sd in cd_ns.findall("sectiondef"):
                    for md in sd.findall("memberdef"):
                        kind = md.get("kind", "")
                        section_title = dict(_MEMBERDEF_SECTIONS).get(kind)
                        if not section_title:
                            continue
                        qualified = (md.findtext("qualifiedname") or "").strip() or \
                                    (md.findtext("name") or "").strip()
                        # Skip class methods / sub-namespace members.
                        if qualified.startswith(ns_pfx) and "::" in qualified[len(ns_pfx):]:
                            continue
                        def _pt(p) -> str:
                            t = _itertext(p.find("type"))
                            arr = (p.findtext("array") or "").strip()
                            return (t + arr) if arr else t
                        enum_values = []
                        if kind == "enum":
                            for ev in md.findall("enumvalue"):
                                enum_values.append({
                                    "name":        (ev.findtext("name") or "").strip(),
                                    "initializer": (ev.findtext("initializer") or "").strip(),
                                    "brief":       _itertext(ev.find("briefdescription")),
                                })
                        ns_sections.setdefault(section_title, []).append({
                            "id":          md.get("id", ""),
                            "kind":        kind,
                            "name":        (md.findtext("name") or "").strip(),
                            "qualified":   qualified,
                            "type":        _itertext(md.find("type")),
                            "type_elem":   md.find("type"),
                            "static":      md.get("static") == "yes",
                            "args":        (md.findtext("argsstring") or "").strip(),
                            "param_types": [_pt(p) for p in md.findall("param")],
                            "brief":       _itertext(md.find("briefdescription")),
                            "enum_values": enum_values,
                            "strong":      md.get("strong", "no") == "yes",
                        })
        except _ET.ParseError:
            pass

    # Sub-namespaces listed in this namespace's XML.
    ns_prefix = ns["name"] + "::"
    innernamespaces = []
    if ns_xml_path and ns_xml_path.is_file():
        try:
            cd2 = _ET.parse(ns_xml_path).getroot().find("compounddef")
            if cd2 is not None:
                for inn in cd2.findall("innernamespace"):
                    iname = (inn.text or "").strip()
                    irefid = inn.get("refid", "")
                    if iname:
                        innernamespaces.append((iname, irefid))
        except _ET.ParseError:
            pass

    def _ns_has_content(refid: str) -> bool:
        f = xml_dir / f"{refid}.xml"
        if not f.is_file():
            return True
        try:
            cd3 = _ET.parse(f).getroot().find("compounddef")
            return cd3 is not None and bool(
                cd3.findall("sectiondef") or cd3.findall("innerclass") or
                cd3.findall("innernamespace"))
        except _ET.ParseError:
            return True

    nonempty_ns = [(n, r) for n, r in innernamespaces if _ns_has_content(r)]
    if nonempty_ns:
        lines += ["## Namespaces", "", "| Namespace |", "|---|"]
        for iname, _ in sorted(nonempty_ns, key=lambda x: x[0].lower()):
            short = iname[len(ns_prefix):] if iname.startswith(ns_prefix) else iname
            islug = iname.replace("::", "__")
            lines.append(f"| [namespace {short}](namespace_{islug}.md) |")
        lines.append("")

    # Classes directly in this namespace.
    refid_prefix = ns["name"].replace("::", "_1_1") + "_1_1"
    innerclasses = []
    for kind in ("struct", "class"):
        for xml_file in sorted(xml_dir.glob(f"{kind}{refid_prefix}*.xml")):
            try:
                cd2 = _ET.parse(xml_file).getroot().find("compounddef")
                if cd2 is None:
                    continue
                cname = (cd2.findtext("compoundname") or "").strip()
                # Skip classes in a sub-namespace; allow template specializations
                # whose parameters may contain qualified names (e.g. cv::Affine3<_Tp>).
                bare = cname[len(ns_prefix):].split("<")[0]
                if "::" in bare:
                    continue
                brief = _itertext(cd2.find("briefdescription"))
                innerclasses.append((xml_file.stem, cname, kind, brief))
            except _ET.ParseError:
                continue
    if innerclasses:
        lines += ["## Classes", "", "| Name |", "|---|"]
        for ic_refid, ic_name, ic_kind, ic_brief in innerclasses:
            page = _class_page_name(ic_refid)
            short_name = ic_name[len(ns_prefix):]
            lines.append(f"| [`{ic_kind} {short_name}`]({page}.md) |")
        lines.append("")

    # Member summary tables.
    for kind_key, section_title in _MEMBERDEF_SECTIONS:
        items = ns_sections.get(section_title, [])
        if not items:
            continue
        lines.append(f"## {section_title}")
        lines.append("")
        if section_title == "Functions":
            lines += ["| Return | Name |", "|---|---|"]
            for m in items:
                ret_md = _type_to_md(m.get("type_elem"))
                if not ret_md:
                    ret_md = _md_escape_cell(m["type"]) or "&nbsp;"
                if m.get("static"):
                    ret_md = "static " + ret_md
                label = f"{m['name']}{_md_escape_cell(m['args'])}"
                lines.append(f"| {ret_md} | [`{label}`](#{m['id']}) |")
        elif section_title in ("Typedefs", "Variables"):
            for m in items:
                lines.append("```cpp")
                lines.append(f"typedef {m['type']} {m['name']}" if section_title == "Typedefs"
                              else f"{m['type']} {m['name']}")
                lines.append("```")
                lines.append("")
            continue
        elif section_title == "Enumerations":
            for m in items:
                if m["brief"]:
                    lines.append(_md_escape_cell(m["brief"]))
                    lines.append("")
                lines.append("```cpp")
                lines.extend(_enum_synopsis_lines(m))
                lines.append("```")
                lines.append("")
            continue
        else:
            lines += ["| Name | Description |", "|---|---|"]
            for m in items:
                lines.append(f"| [`{m['name']}`](#{m['id']}) | {_md_escape_cell(m['brief'])} |")
        lines.append("")

    # Detailed description or doxygennamespace fallback.
    if not ns_sections and not innerclasses:
        lines += ["## Detailed Description", "",
                  f"```{{doxygennamespace}} {ns['name']}", ":project: opencv", "```", ""]
    elif ns.get("detailed"):
        lines += ["## Detailed Description", "", ns["detailed"], ""]

    # Per-member detail blocks.
    seen_define_names: set[str] = set()
    for kind_key, section_title in _MEMBERDEF_SECTIONS:
        items = ns_sections.get(section_title, [])
        if not items:
            continue
        if section_title == "Enumerations":
            enum_items = [m for m in items if "<" not in (m.get("name") or "")]
            if enum_items:
                lines.append(f"## {_MEMBER_DETAIL_SECTION[section_title]}")
                lines.append("")
                for m in enum_items:
                    qualified = m["qualified"] or m["name"]
                    keyword = "enum class" if m.get("strong") else "enum"
                    lines.append(f"({m['id']})=")
                    lines.append(f"### {m['name']}")
                    lines.append("")
                    lines += [f"`{keyword} {qualified}`", ""]
                    if m.get("brief"):
                        lines += [_md_escape_cell(m["brief"]), ""]
                    vals = m.get("enum_values") or []
                    if vals:
                        has_desc = any(v.get("brief") for v in vals)
                        if has_desc:
                            lines += ["| Enumerator | Description |", "|---|---|"]
                        else:
                            lines += ["| Enumerator |", "|---|"]
                        for v in vals:
                            scope = qualified if m.get("strong") else ns["name"]
                            cpp_key = f"{scope}::{v['name']}"
                            py_entries = _PY_SIGNATURES.get(cpp_key, [])
                            py_name = py_entries[0]["name"] if py_entries else None
                            cell = f"`{v['name']}`"
                            if py_name:
                                cell += f"<br>Python: `{py_name}`"
                            if has_desc:
                                lines.append(f"| {cell} | {_md_escape_cell(v.get('brief') or '')} |")
                            else:
                                lines.append(f"| {cell} |")
                        lines.append("")
            continue
        directive = _MEMBER_DIRECTIVE.get(kind_key)
        if not directive:
            continue
        rendered = []
        for m in items:
            if "<" in (m.get("name") or ""):
                continue
            qualified = m["qualified"] or m["name"]
            if m["kind"] == "function":
                spec = qualified + _function_signature(m)
            elif m["kind"] == "define":
                if m["name"] in seen_define_names:
                    continue
                seen_define_names.add(m["name"])
                spec = m["name"]
            else:
                spec = qualified
            rendered.append((spec, directive))
        if not rendered:
            continue
        lines.append(f"## {_MEMBER_DETAIL_SECTION[section_title]}")
        lines.append("")
        for spec, dname in rendered:
            short = spec.split("(")[0].split("::")[-1]
            suffix = "()" if dname == "doxygenfunction" else ""
            lines += [f"### {short}{suffix}", "",
                      f"```{{{dname}}} {spec}", ":project: opencv", "```", ""]

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
        lines += ["## Topics", ""]
        for child in node["children"]:
            lines.append(f"- @subpage api_{child['name']}")
        lines.append("")

    _has_content = bool(node["innerclasses"] or node["sections"] or node["children"])
    if node["detailed"]:
        lines += ["## Detailed Description", "", node["detailed"], ""]
    elif _has_content:
        lines += ["## Detailed Description", ""]

    if ns_map and ns_map.get(name):
        lines += _namespaces_section(ns_map[name])

    if node["children"]:
        _stub_write(out, "\n".join(lines) + "\n")
        for child in node["children"]:
            _write_api_stub(child, out_dir, classes_seen, ns_map)
        return

    # ---- Leaf page ----------------------------------------------------------

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

    for _, section_title in _MEMBERDEF_SECTIONS:
        items = node["sections"].get(section_title, [])
        if not items:
            continue
        lines.append(f"## {section_title}")
        lines.append("")
        if section_title == "Functions":
            lines += ["{.api-reference-table .api-function-table}",
                      "| Return | Name | Description |", "|---|---|---|"]
            # Rich Return cell on api/core_basic only.
            _rich_return = (name == "core_basic")
            for m in items:
                ret_type = _md_escape_cell(m["type"]) or "&nbsp;"
                label = f"{m['name']}{_md_escape_cell(m['args'])}"
                sig_link = _member_anchor_link(m, label)
                if _rich_return:
                    ret_type = re.sub(r"^CV_EXPORTS(?:_[A-Z]+)?\s+", "", ret_type)
                    storage = "static " if m.get("static") else ""
                    if m.get("template"):
                        ret = f"`{m['template']}`<br>`{storage}{ret_type}`"
                    else:
                        ret = f"`{storage}{ret_type}`"
                else:
                    ret = f"`{ret_type}`"
                lines.append(
                    f"| {ret} | {sig_link} | {_md_escape_cell(m['brief'])} |")
        elif section_title in ("Typedefs", "Variables"):
            marker = ("{.api-typedef-table}" if section_title == "Typedefs"
                      else "{.api-reference-table}")
            lines += [marker, "| Type | Name | Description |", "|---|---|---|"]
            for m in items:
                t = _md_escape_cell(m["type"]) or "&nbsp;"
                name_link = _member_anchor_link(m, m["name"])
                lines.append(f"| `{t}` | {name_link} | {_md_escape_cell(m['brief'])} |")
        elif section_title == "Enumerations":
<<<<<<< HEAD
            # Code-style synopsis (Doxygen layout) instead of name/desc table.
            # On most group pages we emit the synopsis only — the per-value
            # initializer list is already self-explanatory. On core_basic we
            # additionally append a "More..." link after each synopsis,
            # pointing to that enum's detail block in the "Enumeration Type
            # Documentation" section emitted by the detail loop below.
=======
            # core_basic also links to detail.
>>>>>>> abhishek/doc_optimizations_v2_squashed
            _enum_more_link = (name == "core_basic")
            for m in items:
                _more = ""
                if _enum_more_link:
<<<<<<< HEAD
                    _qual = m["qualified"] or m["name"]
                    _eid = _sphinx_cpp_v4_id(_qual)
                    _more = f"[More...](#{_eid})"
                # Synopsis first, then the brief description line below it
                # — with "More..." appended inline at the end of that
                # description (matching the live Doxygen group page
                # layout). When there's no brief, the link stands alone
                # below the synopsis.
=======
                    _eid = _sphinx_cpp_v4_id(m["qualified"] or m["name"])
                    _more = f" [View details](#{_eid})"
                if m["brief"]:
                    # Link inline at end of brief.
                    lines.append(_md_escape_cell(m["brief"]) + _more)
                    lines.append("")
                elif _more:
                    lines.append(_more.strip())
                    lines.append("")
>>>>>>> abhishek/doc_optimizations_v2_squashed
                lines.append("```cpp")
                lines.extend(_enum_synopsis_lines(m))
                lines.append("```")
                if m["brief"]:
                    line = _md_escape_cell(m["brief"])
                    if _more:
                        line = f"{line} {_more}"
                        _more = ""
                    lines.append(line)
                if _more:
                    lines.append(_more)
                lines.append("")
            continue   # already appended trailing blank
        else:  # Macros
            lines += ["{.api-reference-table}",
                      "| Name | Description |", "|---|---|"]
            for m in items:
                name_link = _member_anchor_link(m, m["name"])
                lines.append(f"| {name_link} | {_md_escape_cell(m['brief'])} |")
        lines.append("")

    # Detail blocks via `_render_member_detail` (breathe chokes); macros keep
    # `{doxygendefine}`; enums/class-members/template-specs skipped.
    seen_define_names: set[str] = set()
    for kind_key, section_title in _MEMBERDEF_SECTIONS:
        items = node["sections"].get(section_title, [])
        if not items:
<<<<<<< HEAD
            continue
        # Enum detail blocks are emitted only on core_basic — every other
        # group page is content with the summary-only synopsis. The
        # detail block provides the `_CPPv4…` anchor target for the
        # "More..." link added by the summary above.
        if kind_key == "enum" and name != "core_basic":
=======
>>>>>>> abhishek/doc_optimizations_v2_squashed
            continue
        # Enum detail blocks: core_basic only.
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
            if kind_key in ("function", "variable") and _is_class_member(m):
                continue
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
<<<<<<< HEAD
                # core_basic-only path (gated by the early `continue` above).
                # Breathe's `{doxygenenum}` handles cv:: enums reliably and
                # emits the `_CPPv4N2cv<len><name>E` anchor that the
                # synopsis "More..." link targets.
=======
                # core_basic only; doxygenenum emits anchor.
>>>>>>> abhishek/doc_optimizations_v2_squashed
                blocks.append([
                    f"```{{doxygenenum}} {m['qualified'] or m['name']}",
                    ":project: opencv",
                    "```",
                    "",
                ])
                continue
            if kind_key == "define":
                if m["name"] in seen_define_names:  # dedupe arity overloads
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
    out += ["```cpp"] + ([tmpl] if tmpl else []) + [decl, "```", ""]

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
    if m.get("template"):
        out += [f"`{m['template']}`", ""]
    ret = re.sub(r"^CV_EXPORTS(?:_[A-Z]+)?\s+", "", m.get("type") or "")
    storage = ("static " if m.get("static") else "") \
        + ("inline " if m.get("inline") else "")
    qname = m["qualified"] or m["name"]
    out += [f"`{storage}{ret} {qname}{m['args']}`", ""]
    if m.get("include_file"):
        out += [f"`#include <{m['include_file']}>`", ""]
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
