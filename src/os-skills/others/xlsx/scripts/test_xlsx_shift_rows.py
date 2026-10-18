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
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

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


def _tag(local: str) -> str:
    return f"{{{NS_MAIN}}}{local}"


class TestXlsxShiftRows(unittest.TestCase):
    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix="xlsx_shift_rows_test_")
        ws_dir = os.path.join(self.work_dir, "xl", "worksheets")
        os.makedirs(ws_dir)
        self.ws_path = os.path.join(ws_dir, "sheet1.xml")
        with open(self.ws_path, "w", encoding="utf-8") as fh:
            fh.write(WORKSHEET_XML)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def run_script(self, *args):
        result = subprocess.run(
            [sys.executable, SCRIPT, self.work_dir, *args],
            capture_output=True,
            text=True,
        )
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
              for c in row])
            for row in sheet_data
        ]

    def test_delete_row_drops_content_without_duplicate(self):
        self.run_script("delete", "8", "1")
        rows = self.read_rows()
        numbers = [r[0] for r in rows]
        # No duplicate r attributes and the deleted content is gone.
        self.assertEqual(numbers, [7, 8])
        self.assertEqual([c for c, _, _ in rows if c == 7], [7])
        # Old row 9 shifted up to 8 with its cell address and formula.
        self.assertEqual(rows[1][1], ["A8", "B8"])
        self.assertEqual(rows[1][2], [None, "A8*2"])

    def test_delete_multiple_rows(self):
        self.run_script("delete", "7", "2")
        rows = self.read_rows()
        numbers = [r[0] for r in rows]
        self.assertEqual(numbers, [7])
        self.assertEqual(rows[0][1], ["A7", "B7"])
        self.assertEqual(rows[0][2], [None, "A7*2"])

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


if __name__ == "__main__":
    unittest.main()
