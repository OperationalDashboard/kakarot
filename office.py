"""Office files: Excel (.xlsx), Word (.docx) and PDF - create and read."""
import csv
import json
import os

from tools import S, _cut, safe_path, tool


def _rows(data):
    """Accept a JSON string or list. Rows are lists; or a list of dicts (keys become the header)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:  # the model sometimes writes Python-style quotes
            import ast
            data = ast.literal_eval(data)
    fixed = []
    for r in data or []:  # repair cells the model glued together with mixed quotes, e.g. "Total','=SUM(B2:B4)"
        if isinstance(r, list):
            r = [part for c in r for part in (str(c).split("','") if isinstance(c, str) and "','" in c else [c])]
        fixed.append(r)
    data = fixed
    if data and isinstance(data[0], dict):
        head = list(dict.fromkeys(k for r in data for k in r))
        return [head] + [[r.get(k, "") for k in head] for r in data]
    return data


def _num(v):
    if isinstance(v, str):
        t = v.replace(",", "").strip()
        try:
            return int(t) if t.lstrip("-").isdigit() else float(t)
        except ValueError:
            return v
    return v


@tool("create_excel", "Create an Excel file (.xlsx). Give the rows as JSON: a list of rows (first row = header), "
                      "or a list of objects. Cells starting with '=' become formulas, e.g. '=SUM(B2:B10)'. "
                      "For several sheets pass sheets as JSON {\"Sheet name\": rows, ...}.",
      {"path": S("File path ending in .xlsx"),
       "rows": S("JSON rows for one sheet, e.g. [[\"Item\",\"Price\"],[\"Tea\",20]]. Leave empty if using sheets."),
       "sheets": S("Optional JSON object of sheet name -> rows.")}, ["path"])
def create_excel(path, rows="", sheets=""):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    p = safe_path(path if path.lower().endswith(".xlsx") else path + ".xlsx")
    book = json.loads(sheets) if sheets else {"Sheet1": rows}
    wb = Workbook()
    wb.remove(wb.active)
    for name, data in book.items():
        ws = wb.create_sheet(str(name)[:31])
        data = _rows(data) or []
        for r in data:
            ws.append([_num(v) for v in r])
        if data:
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="1F7A5A")
            ws.freeze_panes = "A2"
            for i, col in enumerate(zip(*[[str(v) for v in r] for r in data]), 1):
                ws.column_dimensions[get_column_letter(i)].width = min(max(len(v) for v in col) + 2, 60)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    wb.save(p)
    return f"Excel file saved: {p} ({', '.join(f'{k}: {len(_rows(v) or [])} rows' for k, v in book.items())})"


@tool("read_excel", "Read an Excel (.xlsx) or CSV file and return its rows as text (first sheets, up to 200 rows each).",
      {"path": S("File path.")})
def read_excel(path):
    p = safe_path(path)
    if p.lower().endswith(".csv"):
        with open(p, encoding="utf-8-sig", errors="replace") as f:
            rows = list(csv.reader(f))[:200]
        return _cut("\n".join(" | ".join(r) for r in rows), 12000)
    from openpyxl import load_workbook
    wb = load_workbook(p, data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets[:5]:
        out.append(f"### Sheet: {ws.title}")
        for i, r in enumerate(ws.iter_rows(values_only=True)):
            if i >= 200:
                out.append("...(more rows)")
                break
            out.append(" | ".join("" if v is None else str(v) for v in r))
    return _cut("\n".join(out), 12000)


def _blocks(content):
    """Simple markdown: '# ' headings, '- ' bullets, '| a | b |' tables, blank line = new paragraph."""
    lines, i, out = content.splitlines(), 0, []
    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("|"):
            table = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set("-: ") for c in cells):
                    table.append(cells)
                i += 1
            out.append(("table", table))
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            out.append(("h", min(level, 3), line.lstrip("#").strip()))
        elif line.lstrip().startswith(("- ", "* ")):
            out.append(("li", line.lstrip()[2:]))
        elif line.strip():
            out.append(("p", line.strip()))
        i += 1
    return out


@tool("create_word", "Create a Word document (.docx). Write content in simple markdown: '# Heading', '## Sub', "
                     "'- bullet', tables as '| a | b |' lines, plain lines become paragraphs.",
      {"path": S("File path ending in .docx"), "title": S("Document title."), "content": S("Body in simple markdown.")})
def create_word(path, title, content):
    import docx
    p = safe_path(path if path.lower().endswith(".docx") else path + ".docx")
    d = docx.Document()
    if title:
        d.add_heading(title, 0)
    for b in _blocks(content):
        if b[0] == "h":
            d.add_heading(b[2], b[1])
        elif b[0] == "li":
            d.add_paragraph(b[1], style="List Bullet")
        elif b[0] == "table" and b[1]:
            t = d.add_table(rows=len(b[1]), cols=max(len(r) for r in b[1]))
            t.style = "Light Grid Accent 1"
            for r, row in enumerate(b[1]):
                for c, val in enumerate(row):
                    t.cell(r, c).text = val
        else:
            d.add_paragraph(b[1])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    d.save(p)
    return f"Word document saved: {p}"


@tool("create_pdf", "Create a PDF document. Same simple markdown as create_word (# headings, - bullets, | tables |).",
      {"path": S("File path ending in .pdf"), "title": S("Document title."), "content": S("Body in simple markdown.")})
def create_pdf(path, title, content):
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    p = safe_path(path if path.lower().endswith(".pdf") else path + ".pdf")
    st = getSampleStyleSheet()
    flow = [Paragraph(escape(title), st["Title"]), Spacer(1, 8)] if title else []
    bullets = []

    def flush():
        if bullets:
            flow.append(ListFlowable([ListItem(Paragraph(escape(x), st["BodyText"])) for x in bullets],
                                     bulletType="bullet"))
            bullets.clear()

    for b in _blocks(content):
        if b[0] == "li":
            bullets.append(b[1])
            continue
        flush()
        if b[0] == "h":
            flow.append(Paragraph(escape(b[2]), st[f"Heading{b[1]}"]))
        elif b[0] == "table" and b[1]:
            t = Table(b[1], repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F7A5A")),
                                   ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                                   ("FONTSIZE", (0, 0), (-1, -1), 9)]))
            flow += [t, Spacer(1, 8)]
        else:
            flow.append(Paragraph(escape(b[1]), st["BodyText"]))
    flush()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    SimpleDocTemplate(p, pagesize=A4, title=title or "").build(flow)
    return f"PDF saved: {p}"
