#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
xlsx_shift_rows.py — Shift all row references in an unpacked xlsx working directory
after inserting or deleting rows.

Usage:
    # Insert 2 rows at row 5 (rows 5+ shift down by 2)
    python3 xlsx_shift_rows.py <work_dir> insert 5 2 [--sheet NAME|INDEX]

    # Delete 1 row at row 8 (row 8 content is removed; rows 9+ shift up by 1)
    python3 xlsx_shift_rows.py <work_dir> delete 8 1 [--sheet NAME|INDEX]

Sheet targeting:
    Row structure (sheetData) is mutated on ONE worksheet. ``--sheet`` selects
    it by 1-based index or by the display name in xl/workbook.xml. When the
    workbook has a single worksheet it is the default. When it has several and
    ``--sheet`` is omitted the script refuses and lists the sheets rather than
    deleting rows from every sheet by accident.

What it updates in the targeted worksheet (and workbook-level ranges that point
at it):
  - <row r="N"> attributes in worksheet sheetData
  - <c r="XN"> cell address attributes in worksheet sheetData
  - <f> formula text: absolute row references (e.g. B7, $B$7, $B7)
  - <mergeCell ref="A5:C7"> ranges (endpoints inside the deleted block are
    clamped; a range that collapses to nothing is removed and the parent
    <mergeCells count> is decremented)
  - <conditionalFormatting sqref="..."> ranges
  - <dataValidation sqref="..."> ranges
  - <dimension ref="A1:D20"> extent marker (recomputed from surviving rows
    in delete mode)
  - Table <table ref="A1:D20"> in xl/tables/*.xml
  - Chart series <numRef><f> and <strRef><f> range references in xl/charts/*.xml
  - PivotCache source <worksheetSource ref="..."> in xl/pivotCaches/*.xml

IMPORTANT: Run this script on the UNPACKED directory before repacking.
After running, repack with xlsx_pack.py and re-validate with formula_check.py.

Limitations:
  - Named ranges in workbook.xml <definedNames> are NOT updated automatically.
    Review them manually after running this script.
  - Structured table references (Table[@Column]) are NOT updated.
  - External workbook links in xl/externalLinks/ are NOT updated.
  - In delete mode, formulas that point INTO the deleted block are shifted to
    the first surviving row rather than rewritten to #REF!; review them.
  - Rows without an explicit ``r`` attribute (implicit numbering) are left
    untouched; most writers emit explicit ``r``.
"""

import sys
import os
import re
import xml.etree.ElementTree as ET
import xml.dom.minidom


def col_letter(n: int) -> str:
    """Convert 1-based column number to Excel column letter(s)."""
    r = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        r = chr(65 + rem) + r
    return r


def col_number(s: str) -> int:
    """Convert Excel column letter(s) to 1-based column number."""
    n = 0
    for c in s.upper():
        n = n * 26 + (ord(c) - 64)
    return n


# ---------------------------------------------------------------------------
# Core shifting logic for formula strings
# ---------------------------------------------------------------------------

def _shift_refs(text: str, at: int, delta: int) -> str:
    """Shift cell references in a non-quoted formula fragment."""
    def replacer(m: re.Match) -> str:
        dollar_col = m.group(1)   # "$" or ""
        col_part = m.group(2)     # e.g. "B" or "AB"
        dollar_row = m.group(3)   # "$" or ""
        row_str = m.group(4)      # e.g. "7"
        row = int(row_str)
        if row >= at:
            row = max(1, row + delta)
        return f"{dollar_col}{col_part}{dollar_row}{row}"

    pattern = r'(\$?)([A-Z]+)(\$?)(\d+)'
    return re.sub(pattern, replacer, text)


def shift_formula(formula: str, at: int, delta: int) -> str:
    """
    Shift absolute and mixed row references >= `at` by `delta` in a formula string.

    Handles:
      B7       (relative col, absolute row — shifts if row >= at)
      $B$7     (absolute col, absolute row — shifts)
      $B7      (absolute col, relative row — shifts)
      B$7      (relative col, absolute — shifts)
      BUT NOT:  B:B  (whole-column reference — left as-is)

    Skips content inside single-quoted sheet name prefixes to avoid
    corrupting names like 'Budget FY2025' (where FY2025 is NOT a cell ref).

    Does NOT handle:
      - Named ranges
      - Structured references (Table[@Col])
      - R1C1 notation
    """
    # Split on quoted sheet names: 'Sheet Name' portions are odd-indexed
    segments = re.split(r"('[^']*(?:''[^']*)*')", formula)
    result = []
    for i, seg in enumerate(segments):
        if i % 2 == 1:
            result.append(seg)
        else:
            result.append(_shift_refs(seg, at, delta))
    return "".join(result)


# ---------------------------------------------------------------------------
# Range endpoint clamping (shared by dimension/mergeCell/sqref/table/chart/pivot)
# ---------------------------------------------------------------------------

_CELL_RE = re.compile(r'^(\$?)([A-Z]+)(\$?)(\d+)$')


def _row_after(row: int, at: int, delta: int,
               delete_from: int | None, delete_count: int,
               is_end: bool) -> int:
    """Map a single range endpoint row to its post-operation row.

    Insert mode (``delete_from`` is None) simply shifts rows >= ``at``.
    Delete mode clamps endpoints that fall INSIDE the deleted block
    ``[delete_from, delete_from + delete_count)``: a start endpoint lands on
    ``delete_from`` (the first surviving row), an end endpoint lands on
    ``delete_from - 1``. Rows below the block shift up by ``delta``.
    """
    if delete_from is None:
        return max(1, row + delta) if row >= at else row
    if row < delete_from:
        return row
    if row < delete_from + delete_count:
        return delete_from if not is_end else delete_from - 1
    return max(1, row + delta)


def _shift_cell(cell: str, at: int, delta: int,
                delete_from: int | None, delete_count: int,
                is_end: bool) -> str:
    m = _CELL_RE.match(cell)
    if not m:
        return cell
    new_row = _row_after(int(m.group(4)), at, delta, delete_from, delete_count, is_end)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}{new_row}"


def adjust_range_ref(ref: str, at: int, delta: int,
                     delete_from: int | None = None,
                     delete_count: int = 0) -> str:
    """Shift/clamp every endpoint in a space-separated sqref string.

    Returns "" when a single range collapses (its end row ends up above its
    start row because the whole span was deleted), signalling the caller to
    drop the element. In insert mode this is equivalent to the old
    ``shift_sqref`` (delta is positive, nothing collapses).
    """
    parts = ref.split()
    out = []
    for part in parts:
        if ':' in part:
            left, right = part.split(':', 1)
            nl = _shift_cell(left, at, delta, delete_from, delete_count, False)
            nr = _shift_cell(right, at, delta, delete_from, delete_count, True)
            lm = _CELL_RE.match(nl)
            rm = _CELL_RE.match(nr)
            if lm and rm and int(rm.group(4)) < int(lm.group(4)):
                continue  # collapsed range: drop it
            out.append(f"{nl}:{nr}")
        else:
            out.append(_shift_cell(part, at, delta, delete_from, delete_count, False))
    return " ".join(out)


def shift_sqref(sqref: str, at: int, delta: int) -> str:
    """Insert-mode sqref shift (kept for parity with the original path)."""
    return adjust_range_ref(sqref, at, delta, None, 0)


def shift_chart_range(text: str, at: int, delta: int,
                      delete_from: int | None = None,
                      delete_count: int = 0,
                      filter_sheet: str | None = None) -> str:
    """
    Shift row references inside a chart range formula like:
      Sheet1!$B$5:$B$20
      'Q1 Data'!$A$3:$A$15
    When ``filter_sheet`` is set, ranges pointing at a different sheet are
    returned unchanged (multi-sheet workbooks only touch the target sheet).
    """
    if '!' not in text:
        return text
    bang = text.index('!')
    sheet_part = text[:bang + 1]
    range_part = text[bang + 1:]
    if filter_sheet is not None:
        name = text[:bang].strip().strip("'")
        if name != filter_sheet:
            return text
    return sheet_part + adjust_range_ref(range_part, at, delta, delete_from, delete_count)


# ---------------------------------------------------------------------------
# XML file processors
# ---------------------------------------------------------------------------

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/chartDrawing"

# Namespace map used by ElementTree for tag lookup
NSMAP = {"ss": NS_MAIN}


def _tag(local: str) -> str:
    return f"{{{NS_MAIN}}}{local}"


def _recompute_dimension(old: str, surviving_rows: list[int],
                         at: int, delta: int,
                         delete_from: int | None, delete_count: int) -> str:
    """Delete-mode dimension: keep the original column letters but set the
    row span to the true extent of the surviving sheetData rows."""
    if ':' in old:
        start, end = old.split(':', 1)
        sm = _CELL_RE.match(start)
        em = _CELL_RE.match(end)
        if sm and em and surviving_rows:
            min_row = min(surviving_rows)
            max_row = max(surviving_rows)
            ns = f"{sm.group(1)}{sm.group(2)}{sm.group(3)}{min_row}"
            ne = f"{em.group(1)}{em.group(2)}{em.group(3)}{max_row}"
            return f"{ns}:{ne}"
    return adjust_range_ref(old, at, delta, delete_from, delete_count)


def process_worksheet(path: str, at: int, delta: int,
                      delete_from: int | None = None,
                      delete_count: int = 0) -> int:
    """Update row/cell references in a worksheet XML. Returns change count.

    When ``delete_from`` is set, row elements whose ``r`` falls in
    ``[delete_from, delete_from + delete_count)`` are dropped (their content
    is gone, as the usage docstring promises) instead of being relabeled onto
    the rows above them. ``at`` is then the shift threshold for the surviving
    rows (``at = delete_from + delete_count`` for delete mode). Range
    endpoints inside the deleted block are clamped so the result stays a
    valid worksheet.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    changes = 0
    surviving_rows: list[int] = []

    # 1. <row r="N"> and <c r="XN"> inside sheetData
    sheet_data = root.find(_tag("sheetData"))
    if sheet_data is not None:
        for row_el in list(sheet_data):
            r_str = row_el.get("r")
            if r_str is None:
                continue
            r = int(r_str)

            # Delete mode: drop the rows whose content is being removed.
            if (delete_from is not None
                    and delete_from <= r < delete_from + delete_count):
                sheet_data.remove(row_el)
                changes += 1
                continue

            if r >= at:
                new_r = max(1, r + delta)
                row_el.set("r", str(new_r))
                changes += 1
                # Update each cell's r attribute
                for cell_el in row_el:
                    cell_ref = cell_el.get("r", "")
                    if cell_ref:
                        new_ref = shift_formula(cell_ref, at, delta)
                        if new_ref != cell_ref:
                            cell_el.set("r", new_ref)
                            changes += 1
                surviving_rows.append(new_r)
            else:
                surviving_rows.append(r)

            # Also update formulas in every row (formulas can reference any row)
            for cell_el in row_el:
                f_el = cell_el.find(_tag("f"))
                if f_el is not None and f_el.text:
                    new_f = shift_formula(f_el.text, at, delta)
                    if new_f != f_el.text:
                        f_el.text = new_f
                        changes += 1

    # 2. <dimension ref="A1:D20">
    for dim in root.iter(_tag("dimension")):
        old = dim.get("ref", "")
        if delete_from is None:
            new = shift_sqref(old, at, delta)
        else:
            new = _recompute_dimension(old, surviving_rows, at, delta,
                                       delete_from, delete_count)
        if new != old:
            dim.set("ref", new)
            changes += 1

    # 3. <mergeCell ref="A5:C7"> — clamp endpoints; drop collapsed ranges
    #    and keep the parent <mergeCells count> accurate.
    for mc_parent in root.iter(_tag("mergeCells")):
        removed = 0
        for mc in list(mc_parent):
            old = mc.get("ref", "")
            new = adjust_range_ref(old, at, delta, delete_from, delete_count)
            if new == "" and old != "":
                mc_parent.remove(mc)
                removed += 1
                changes += 1
            elif new != old:
                mc.set("ref", new)
                changes += 1
        if removed:
            cnt = mc_parent.get("count")
            if cnt is not None:
                mc_parent.set("count", str(max(0, int(cnt) - removed)))

    # 4. <conditionalFormatting sqref="...">
    for cf in root.iter(_tag("conditionalFormatting")):
        old = cf.get("sqref", "")
        new = adjust_range_ref(old, at, delta, delete_from, delete_count)
        if new != old:
            cf.set("sqref", new)
            changes += 1

    # 5. <dataValidation sqref="...">
    for dv in root.iter(_tag("dataValidation")):
        old = dv.get("sqref", "")
        new = adjust_range_ref(old, at, delta, delete_from, delete_count)
        if new != old:
            dv.set("sqref", new)
            changes += 1

    if changes > 0:
        _write_tree(tree, path)
    return changes


def process_chart(path: str, at: int, delta: int,
                  delete_from: int | None = None,
                  delete_count: int = 0,
                  filter_sheet: str | None = None) -> int:
    """Update data range references in a chart XML."""
    # Charts use DrawingML namespace; we look for <f> elements with range strings
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    # Pattern matches content of <f>Sheet1!$A$1:$A$10</f> style elements
    def replace_f(m: re.Match) -> str:
        tag_open = m.group(1)
        inner = m.group(2)
        tag_close = m.group(3)
        new_inner = shift_chart_range(inner, at, delta,
                                      delete_from, delete_count, filter_sheet)
        return f"{tag_open}{new_inner}{tag_close}"

    new_content = re.sub(r'(<(?:[^:>]+:)?f>)([^<]+)(</(?:[^:>]+:)?f>)',
                          replace_f, content)
    changes = content != new_content
    if changes:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_content)
    return 1 if changes else 0


def process_table(path: str, at: int, delta: int,
                  delete_from: int | None = None,
                  delete_count: int = 0) -> int:
    """Update the ref attribute on the <table> root element."""
    tree = ET.parse(path)
    root = tree.getroot()
    # The root element IS the table
    old = root.get("ref", "")
    if not old:
        return 0
    new = adjust_range_ref(old, at, delta, delete_from, delete_count)
    if not new or new == old:
        return 0
    root.set("ref", new)
    _write_tree(tree, path)
    return 1


def process_pivot_cache(path: str, at: int, delta: int,
                        delete_from: int | None = None,
                        delete_count: int = 0,
                        filter_sheet: str | None = None) -> int:
    """Update worksheetSource ref in a pivot cache definition."""
    tree = ET.parse(path)
    root = tree.getroot()
    changes = 0
    # Look for <worksheetSource ref="A1:D100" sheet="Sheet1" ...>
    for ws in root.iter():
        if ws.tag.endswith("}worksheetSource") or ws.tag == "worksheetSource":
            if filter_sheet is not None and ws.get("sheet") not in (None, filter_sheet):
                continue
            old = ws.get("ref", "")
            if old:
                new = adjust_range_ref(old, at, delta, delete_from, delete_count)
                if new and new != old:
                    ws.set("ref", new)
                    changes += 1
    if changes:
        _write_tree(tree, path)
    return changes


def _write_tree(tree: ET.ElementTree, path: str) -> None:
    """Write ElementTree back to file with pretty-printing."""
    tree.write(path, encoding="unicode", xml_declaration=False)
    # Re-pretty-print for readability
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        dom = xml.dom.minidom.parseString(raw.encode("utf-8"))
        pretty = dom.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
        lines = [line for line in pretty.splitlines() if line.strip()]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        pass  # If pretty-print fails, leave the file as-is


# ---------------------------------------------------------------------------
# Sheet targeting
# ---------------------------------------------------------------------------

def _load_sheet_map(work_dir: str):
    """Return (ordered_paths, name_by_path) for xl/worksheets/*.xml.

    Display names come from xl/workbook.xml <sheet name=... r:id=...> resolved
    through xl/_rels/workbook.xml.rels. When those are absent (e.g. hand-built
    fixtures) the worksheet file basename is used as the name.
    """
    ws_dir = os.path.join(work_dir, "xl", "worksheets")
    ordered = [os.path.join(ws_dir, f)
               for f in sorted(os.listdir(ws_dir)) if f.endswith(".xml")]
    names: dict[str, str] = {}
    wb = os.path.join(work_dir, "xl", "workbook.xml")
    rels = os.path.join(work_dir, "xl", "_rels", "workbook.xml.rels")
    if os.path.isfile(wb):
        try:
            rid_attr = ("{http://schemas.openxmlformats.org/officeDocument/"
                        "2006/relationships}id")
            entries = []
            for sh in ET.parse(wb).getroot().iter():
                if sh.tag.endswith("}sheet") or sh.tag == "sheet":
                    nm = sh.get("name")
                    if nm:
                        entries.append((nm, sh.get(rid_attr)))
            rid_target = {}
            if os.path.isfile(rels):
                for rel in ET.parse(rels).getroot():
                    rid_target[rel.get("Id")] = rel.get("Target")
            for nm, rid in entries:
                tgt = rid_target.get(rid)
                if not tgt:
                    continue
                base = os.path.basename(tgt.replace("\\", "/"))
                for fp in ordered:
                    if os.path.basename(fp) == base:
                        names[fp] = nm
        except Exception:
            pass
    for fp in ordered:
        names.setdefault(fp, os.path.splitext(os.path.basename(fp))[0])
    return ordered, names


def _sheet_table_files(work_dir: str, target_ws: str):
    """Resolve the xl/tables/*.xml files referenced by a worksheet's tableParts."""
    base = os.path.basename(target_ws)
    rels = os.path.join(work_dir, "xl", "worksheets", "_rels", f"{base}.rels")
    if not os.path.isfile(rels):
        return set()
    out = set()
    try:
        for rel in ET.parse(rels).getroot():
            tgt = rel.get("Target", "")
            if "tables/" in tgt.replace("\\", "/"):
                norm = tgt.replace("\\", "/").lstrip("/")
                if not norm.startswith("xl/"):
                    norm = "xl/" + norm.replace("../", "")
                out.add(os.path.normpath(os.path.join(work_dir, norm)))
    except Exception:
        return set()
    return out


def resolve_target(work_dir: str, sheet_arg: str | None):
    """Pick the worksheet(s) to mutate. Returns (ordered_paths, names)."""
    ordered, names = _load_sheet_map(work_dir)
    if not ordered:
        return ordered, names
    listing = ", ".join(f"{names[fp]} ({i + 1})" for i, fp in enumerate(ordered))
    if sheet_arg is None:
        if len(ordered) == 1:
            return ordered, names
        print(f"ERROR: workbook has multiple worksheets; pass --sheet NAME|INDEX. "
              f"Sheets: {listing}")
        sys.exit(1)
    if sheet_arg.isdigit():
        idx = int(sheet_arg) - 1
        if 0 <= idx < len(ordered):
            return [ordered[idx]], names
        print(f"ERROR: --sheet index {sheet_arg} out of range (1..{len(ordered)}). "
              f"Sheets: {listing}")
        sys.exit(1)
    matched = [fp for fp in ordered if names[fp] == sheet_arg]
    if matched:
        return matched, names
    print(f"ERROR: no worksheet named '{sheet_arg}'. Sheets: {listing}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]):
    sheet_arg = None
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--sheet":
            if i + 1 >= len(argv):
                print("ERROR: --sheet requires a NAME or INDEX argument")
                sys.exit(1)
            sheet_arg = argv[i + 1]
            i += 2
            continue
        if a.startswith("--sheet="):
            sheet_arg = a.split("=", 1)[1]
            i += 1
            continue
        positional.append(a)
        i += 1
    return positional, sheet_arg


def main() -> None:
    positional, sheet_arg = _parse_args(sys.argv[1:])
    if len(positional) < 4:
        print(__doc__)
        sys.exit(1)

    work_dir = positional[0]
    operation = positional[1].lower()
    at = int(positional[2])
    count = int(positional[3])

    if operation not in ("insert", "delete"):
        print(f"ERROR: operation must be 'insert' or 'delete', got '{operation}'")
        sys.exit(1)

    # Guard the argument space that produces duplicate/invalid <row r="N">.
    if at < 1:
        print(f"ERROR: row number must be >= 1, got {at}")
        sys.exit(1)
    if count < 1:
        print(f"ERROR: count must be >= 1, got {count}")
        sys.exit(1)

    if operation == "insert":
        delta = count
        shift_at = at
        delete_from: int | None = None
        delete_count = 0
    else:
        delta = -count
        # Rows inside [at, at+count) are deleted; only rows at or below the
        # deleted block shift up. Relabeling rows >= at would renumber the
        # deleted row itself onto the row above it and duplicate content.
        shift_at = at + count
        delete_from = at
        delete_count = count

    if not os.path.isdir(work_dir):
        print(f"ERROR: Directory not found: {work_dir}")
        sys.exit(1)

    ws_dir = os.path.join(work_dir, "xl", "worksheets")
    if not os.path.isdir(ws_dir):
        print(f"ERROR: no worksheets under {ws_dir}")
        sys.exit(1)

    target_paths, names = resolve_target(work_dir, sheet_arg)
    multi = len(_load_sheet_map(work_dir)[0]) > 1
    target_names = {names[fp] for fp in target_paths}
    filter_sheet = None  # only used to narrow charts/pivots on multi-sheet books

    print(f"Operation : {operation} {count} row(s) at row {at} (delta={delta:+d})")
    print(f"Work dir  : {work_dir}")
    print(f"Sheet(s)  : {', '.join(sorted(target_names)) if not multi else names[target_paths[0]]}")
    print()

    total_changes = 0

    # Process the targeted worksheet(s) only.
    for fpath in target_paths:
        n = process_worksheet(fpath, shift_at, delta, delete_from, delete_count)
        if n:
            print(f"  Updated {n:3d} references in xl/worksheets/{os.path.basename(fpath)}")
            total_changes += n

    # Charts: on a multi-sheet workbook only shift ranges pointing at the
    # target sheet; on a single-sheet workbook keep the original global pass.
    charts_dir = os.path.join(work_dir, "xl", "charts")
    if os.path.isdir(charts_dir):
        chart_filter = next(iter(target_names)) if (multi and len(target_names) == 1) else None
        for fname in sorted(os.listdir(charts_dir)):
            if fname.endswith(".xml"):
                fpath = os.path.join(charts_dir, fname)
                n = process_chart(fpath, shift_at, delta,
                                  delete_from, delete_count, chart_filter)
                if n:
                    print(f"  Updated chart ranges in xl/charts/{fname}")
                    total_changes += n

    # Tables: on a multi-sheet workbook only shift tables owned by the target
    # sheet (resolved via the worksheet's tableParts rels).
    tables_dir = os.path.join(work_dir, "xl", "tables")
    if os.path.isdir(tables_dir):
        if multi:
            owned = set()
            for tp in target_paths:
                owned |= _sheet_table_files(work_dir, tp)
        else:
            owned = None  # single sheet: all tables belong to it
        for fname in sorted(os.listdir(tables_dir)):
            if fname.endswith(".xml"):
                fpath = os.path.join(tables_dir, fname)
                if owned is not None and os.path.normpath(fpath) not in owned:
                    continue
                n = process_table(fpath, shift_at, delta, delete_from, delete_count)
                if n:
                    print(f"  Updated table ref in xl/tables/{fname}")
                    total_changes += n

    # Pivot cache definitions
    cache_dir = os.path.join(work_dir, "xl", "pivotCaches")
    if os.path.isdir(cache_dir):
        pivot_filter = next(iter(target_names)) if (multi and len(target_names) == 1) else None
        for fname in sorted(os.listdir(cache_dir)):
            if "Definition" in fname and fname.endswith(".xml"):
                fpath = os.path.join(cache_dir, fname)
                n = process_pivot_cache(fpath, shift_at, delta,
                                        delete_from, delete_count, pivot_filter)
                if n:
                    print(f"  Updated pivot source range in xl/pivotCaches/{fname}")
                    total_changes += n

    print()
    print(f"Total changes: {total_changes}")
    print()
    print("IMPORTANT: Review named ranges in xl/workbook.xml <definedNames> manually.")
    print("           Structured table references (Table[@Col]) are NOT updated.")
    print()
    print("Next steps:")
    print("  1. Review the changes above")
    print(f"  2. python3 xlsx_pack.py {work_dir} output.xlsx")
    print("  3. python3 formula_check.py output.xlsx")


if __name__ == "__main__":
    main()
