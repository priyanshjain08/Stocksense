"""Excel report builder (openpyxl). All text cells pass through the formula-injection guard."""
from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from app.domain.analytics import safe_csv_cell


def _cell(v):
    if isinstance(v, (int, float)) or v is None:
        return v
    return safe_csv_cell(v)  # strings starting with = + - @ are neutralised


def build_workbook(sheets: dict[str, tuple[list[str], list[list]]]) -> bytes:
    """sheets: {title: (headers, rows)} -> .xlsx bytes. Sheet titles are truncated to Excel's 31-char limit."""
    wb = Workbook()
    wb.remove(wb.active)
    for title, (headers, rows) in sheets.items():
        ws = wb.create_sheet(title[:31])
        ws.append([_cell(h) for h in headers])
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append([_cell(v) for v in r])
        for i, h in enumerate(headers, start=1):
            width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:200] if i - 1 < len(r)])
            ws.column_dimensions[get_column_letter(i)].width = min(width + 2, 50)
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
