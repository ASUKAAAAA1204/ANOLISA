#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for xlsx_shift_rows.py insert/delete semantics.

Regression tests for delete mode, which used to relabel the deleted row
onto the row above it (producing duplicate <row r="N"> elements and
keeping the doomed content) instead of dropping its content and shifting
only the rows below the deleted block.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = ("http://schemas.openxmlformats.org/package/2006/relationships")

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(SCRIPTS_DIR, "xlsx_shift_rows.py")

WORKSHEET_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{NS_MAIN}">
  <dimension ref="A1:A9"/>
  <sheetData>
    <row r="7"><c r="A7"><v>7</v></c></row>
    <row r="8"><c r="A8"><v>8</v></c></row>
    <row r="9"><c r="A9"><v>9</v></c><c r="B9"><f>A9*2</f></c></row>
  </sheetData>
</worksheet>
"""

MERGE_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{NS_MAIN}">
  <dimension ref="A1:B7"/>
  <sheetData>
    <row r="5"><c r="A5"><v>5</v></c><c r="B5"><v>5</v></c></row>
    <row r="6"><c r="A6"><v>6</v></c><c r="B6"><v>6</v></c></row>
    <row r="7"><c r="A7"><v>7</v></c><c r="B7"><v>7</v></c></row>
  </sheetData>
  <mergeCells count="2">
    <mergeCell ref="A6:B6"/>
    <mergeCell ref="A7:B7"/>
  </mergeCells>
</worksheet>
"""


def _tag(local: str) -> str:
    return f"{{{NS_MAIN}}}{local}"


def _rows_of(ws_path):
    root = ET.parse(ws_path).getroot()
    sheet_data = root.find(_tag("sheetData"))
    return [int(row.get("r")) for row in sheet_data] if sheet_data is not None else []


class TestXlsxShiftRows(unittest.TestCase):
    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix="xlsx_shift_rows_test_")
        ws_dir = os.path.join(self.work_dir, "xl", "worksheets")
        os.makedirs(ws_dir)
        self.ws_path = os.path.join(ws_dir, "sheet1.xml")
        with open(self.ws_path, "w", encoding="utf-8") as fh:
            fh.write(WORKSHEET_XML)

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def run_script(self, *args, expect_ok=True):
        result = subprocess.run(
            [sys.executable, SCRIPT, self.work_dir, *args],
            capture_output=True,
            text=True,
        )
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def read_rows(self):
        root = ET.parse(self.ws_path).getroot()
        sheet_data = root.find(_tag("sheetData"))
        return [
            (int(row.get("r")),
             [c.get("r") for c in row],
             [c.find(_tag("f")).text
              if c.find(_tag("f")) is not None else None
              for c in row],
             [c.find(_tag("v")).text
              if c.find(_tag("v")) is not None else None
              for c in row])
            for row in sheet_data
        ]

    def test_delete_row_drops_content_without_duplicate(self):
        self.run_script("delete", "8", "1")
        rows = self.read_rows()
        numbers = [r[0] for r in rows]
        # No duplicate r attributes and the deleted content is gone.
        self.assertEqual(numbers, [7, 8])
        self.assertEqual([c for c, *_ in rows if c == 7], [7])
        # Old row 9 shifted up to 8 with its cell address, formula and value;
        # the deleted row's value ("8") no longer appears anywhere.
        self.assertEqual(rows[1][1], ["A8", "B8"])
        self.assertEqual(rows[1][2], [None, "A8*2"])
        self.assertEqual(rows[0][3], ["7"])
        self.assertEqual(rows[1][3], ["9", None])

    def test_delete_multiple_rows(self):
        self.run_script("delete", "7", "2")
        rows = self.read_rows()
        numbers = [r[0] for r in rows]
        self.assertEqual(numbers, [7])
        self.assertEqual(rows[0][1], ["A7", "B7"])
        self.assertEqual(rows[0][2], [None, "A7*2"])
        self.assertEqual(rows[0][3], ["9", None])

    def test_insert_mode_unchanged(self):
        self.run_script("insert", "8", "1")
        rows = self.read_rows()
        numbers = [r[0] for r in rows]
        self.assertEqual(numbers, [7, 9, 10])
        self.assertEqual(rows[2][1], ["A10", "B10"])
        self.assertEqual(rows[2][2], [None, "A10*2"])

    def test_row_order_stays_ascending(self):
        self.run_script("delete", "8", "1")
        with open(self.ws_path, encoding="utf-8") as fh:
            raw = fh.read()
        numbers = [int(m) for m in re.findall(r'<row r="(\d+)"', raw)]
        self.assertEqual(numbers, sorted(numbers))

    def test_rejects_bad_row_and_count(self):
        # at < 1 or count < 1 previously produced duplicate r="1" / no-op runs.
        self.assertEqual(self.run_script("delete", "0", "1", expect_ok=False).returncode, 1)
        self.assertEqual(self.run_script("delete", "3", "-1", expect_ok=False).returncode, 1)


class TestMergeClamp(unittest.TestCase):
    """Blocking #1: merge/dimension endpoints inside the deleted block."""

    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix="xlsx_merge_test_")
        ws_dir = os.path.join(self.work_dir, "xl", "worksheets")
        os.makedirs(ws_dir)
        self.ws_path = os.path.join(ws_dir, "sheet1.xml")
        with open(self.ws_path, "w", encoding="utf-8") as fh:
            fh.write(MERGE_XML)

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def run_script(self, *args):
        result = subprocess.run(
            [sys.executable, SCRIPT, self.work_dir, *args],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_merge_clamped_collapsed_and_counted(self):
        # delete row 6: A6:B6 collapses (removed), A7:B7 -> A6:B6, count 2 -> 1.
        self.run_script("delete", "6", "1")
        root = ET.parse(self.ws_path).getroot()
        mc_parent = root.find(_tag("mergeCells"))
        refs = [mc.get("ref") for mc in mc_parent]
        self.assertEqual(refs, ["A6:B6"], "collapsed merge dropped, survivor shifted")
        self.assertEqual(mc_parent.get("count"), "1", "mergeCells count decremented")
        # no merge may overlap another after the operation
        self.assertEqual(len(refs), len(set(refs)))

    def test_dimension_recomputed_to_true_extent(self):
        # data rows 5,6,7; delete row 7 -> surviving 5,6; the dimension is
        # recomputed from the true extent of the surviving sheetData.
        self.run_script("delete", "7", "1")
        root = ET.parse(self.ws_path).getroot()
        dim = root.find(_tag("dimension")).get("ref")
        self.assertEqual(dim, "A5:B6")


class TestSheetTargeting(unittest.TestCase):
    """Blocking #2: delete must not destroy rows on every worksheet."""

    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix="xlsx_multi_test_")
        ws_dir = os.path.join(self.work_dir, "xl", "worksheets")
        os.makedirs(ws_dir)
        os.makedirs(os.path.join(self.work_dir, "xl", "_rels"))

        def sheet_xml(prefix):
            rows = "".join(
                f'<row r="{n}"><c r="A{n}"><v>{prefix}-{n}</v></c></row>'
                for n in range(1, 6)
            )
            return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                    f'<worksheet xmlns="{NS_MAIN}"><dimension ref="A1:A5"/>'
                    f'<sheetData>{rows}</sheetData></worksheet>\n')

        for i, pfx in enumerate(("S1", "S2"), start=1):
            with open(os.path.join(ws_dir, f"sheet{i}.xml"), "w", encoding="utf-8") as fh:
                fh.write(sheet_xml(pfx))

        wb = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
              f'<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_REL}"><sheets>'
              f'<sheet name="One" sheetId="1" r:id="rId1"/>'
              f'<sheet name="Two" sheetId="2" r:id="rId2"/>'
              f'</sheets></workbook>\n')
        with open(os.path.join(self.work_dir, "xl", "workbook.xml"), "w", encoding="utf-8") as fh:
            fh.write(wb)
        rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                f'<Relationships xmlns="{NS_PKG_REL}">'
                f'<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/>'
                f'<Relationship Id="rId2" Type="x" Target="worksheets/sheet2.xml"/>'
                f'</Relationships>\n')
        with open(os.path.join(self.work_dir, "xl", "_rels", "workbook.xml.rels"), "w", encoding="utf-8") as fh:
            fh.write(rels)

        self.s1 = os.path.join(ws_dir, "sheet1.xml")
        self.s2 = os.path.join(ws_dir, "sheet2.xml")

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, SCRIPT, self.work_dir, *args],
            capture_output=True, text=True,
        )

    def test_refuses_multi_sheet_without_target(self):
        r = self.run_script("delete", "3", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("multiple worksheets", r.stdout + r.stderr)
        # nothing mutated
        self.assertEqual(_rows_of(self.s1), [1, 2, 3, 4, 5])
        self.assertEqual(_rows_of(self.s2), [1, 2, 3, 4, 5])

    def test_targets_only_named_sheet(self):
        r = self.run_script("delete", "3", "1", "--sheet", "Two")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # rows 4,5 on sheet Two shift up; sheet One is untouched.
        self.assertEqual(_rows_of(self.s2), [1, 2, 3, 4])
        self.assertEqual(_rows_of(self.s1), [1, 2, 3, 4, 5])

    def test_targets_by_index(self):
        r = self.run_script("delete", "3", "1", "--sheet", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_rows_of(self.s1), [1, 2, 3, 4])
        self.assertEqual(_rows_of(self.s2), [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
