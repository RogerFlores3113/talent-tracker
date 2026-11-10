"""A small .xlsx writer that needs nothing but the standard library (zipfile and XML text).

It makes what a recruiter's spreadsheet app needs and nothing more: one or more sheets, a bold shaded header row that
stays in view, a filter on the header, sensible column widths, wrapped long text, real numbers and dates, and
clickable web links. Every piece of text is written as an inline string, so a cell that starts with = + - or @ is
only ever text and a spreadsheet app never runs it as a formula. The file carries no author name.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence
from xml.sax.saxutils import escape

MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_CELL = 32000          # Excel's limit is 32767 characters in a cell
MAX_LINK = 2000           # and about 2079 in a link
MAX_ROWS = 1_000_000
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_SHEET_BAD = re.compile(r"[\[\]:*?/\\\x00-\x1f]")
_WEB = re.compile(r"^(?:https?://|mailto:)[^\s<>\"]+$", re.I)
_EPOCH = date(1899, 12, 30)


@dataclass
class Link:
    """A cell that shows `text` and opens `url` (http, https or mailto only; anything else is just text)."""
    url: str
    text: str = ""


@dataclass
class Sheet:
    name: str
    headers: Sequence[str]
    rows: Sequence[Sequence[Any]]
    preamble: Sequence[str] = ()          # plain lines above the header row (a title or a note)
    widths: Sequence[float] | None = None
    freeze: bool = True                   # keep the header row in view
    autofilter: bool = True               # filter buttons on the header row


def clean_text(value: Any) -> str:
    """Text safe for XML 1.0 and for one cell: control characters gone, line breaks as \\n, not too long."""
    s = "" if value is None else str(value)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _BAD_XML.sub("", s)
    return s[:MAX_CELL]


def col_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def safe_sheet_name(name: str, taken: set[str]) -> str:
    base = " ".join(_SHEET_BAD.sub(" ", clean_text(name)).split()).strip("'") or "Sheet"
    base = base[:31].rstrip()
    cand, n = base, 2
    while cand.lower() in taken:
        suffix = f" ({n})"
        cand = base[:31 - len(suffix)].rstrip() + suffix
        n += 1
    taken.add(cand.lower())
    return cand


def _serial(d: date | datetime) -> float:
    if isinstance(d, datetime):
        if d.tzinfo is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        delta = d - datetime(1899, 12, 30)
        return delta.days + delta.seconds / 86400
    return (d - _EPOCH).days


# Cell formats (the cellXfs list in styles.xml). Keep these numbers in step with _STYLES.
S_PLAIN, S_HEAD, S_WRAP, S_DATE, S_LINK, S_NOTE, S_TEXTFIRST, S_NUM = 0, 1, 2, 3, 4, 5, 6, 7

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/></numFmts>'
    '<fonts count="4">'
    '<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>'
    '<font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/><family val="2"/></font>'
    '<font><i/><sz val="11"/><color rgb="FF595959"/><name val="Calibri"/><family val="2"/></font>'
    '</fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF2F4F6F"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="8">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">'
    '<alignment vertical="center" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1">'
    '<alignment horizontal="left" vertical="top"/></xf>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1">'
    '<alignment vertical="top" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" quotePrefix="1" applyAlignment="1">'
    '<alignment vertical="top" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


def _text_cell(ref: str, text: str, style: int) -> str:
    if text == "":
        return ""
    return (f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>')


def _cell(ref: str, value: Any, links: list[tuple[str, str]]) -> str:
    """One body cell as XML ("" for an empty one). Links are collected as (cell, url)."""
    if value is None or value == "":
        return ""
    if isinstance(value, Link):
        text = clean_text(value.text or value.url)
        url = clean_text(value.url).strip()
        if _WEB.match(url) and len(url) <= MAX_LINK:
            links.append((ref, url))
            return _text_cell(ref, text, S_LINK)
        return _text_cell(ref, text, S_WRAP)
    if isinstance(value, bool):
        return _text_cell(ref, "Yes" if value else "No", S_WRAP)
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        return f'<c r="{ref}" s="{S_NUM}"><v>{value!r}</v></c>'
    if isinstance(value, (date, datetime)):
        return f'<c r="{ref}" s="{S_DATE}"><v>{_serial(value)!r}</v></c>'
    text = clean_text(value)
    return _text_cell(ref, text, S_TEXTFIRST if text[:1] in ("=", "+", "-", "@", "\t") else S_WRAP)


def _widths(sheet: Sheet) -> list[float]:
    if sheet.widths:
        return list(sheet.widths)
    out = []
    for i, h in enumerate(sheet.headers):
        longest = len(clean_text(h)) + 2
        for r in list(sheet.rows)[:300]:
            if i < len(r) and r[i] is not None:
                v = r[i].text or r[i].url if isinstance(r[i], Link) else r[i]
                if isinstance(v, (date, datetime)):
                    n = 10
                else:
                    n = max((len(p) for p in clean_text(v).split("\n")), default=0)
                longest = max(longest, n)
        out.append(float(min(60, max(8, longest + 2))))
    return out


def _sheet_xml(sheet: Sheet) -> tuple[str, list[str]]:
    """(worksheet XML, link urls in the order of the relationship ids rId1, rId2 ...)."""
    ncols = max(1, len(sheet.headers))
    rows_xml: list[str] = []
    links: list[tuple[str, str]] = []
    r = 0
    for line in sheet.preamble:
        r += 1
        rows_xml.append(f'<row r="{r}">{_text_cell(f"A{r}", clean_text(line), S_NOTE)}</row>')
    r += 1
    head_row = r
    cells = "".join(_text_cell(f"{col_letter(i + 1)}{r}", clean_text(h), S_HEAD) for i, h in enumerate(sheet.headers))
    rows_xml.append(f'<row r="{r}" ht="22" customHeight="1">{cells}</row>')
    body = list(sheet.rows)[:MAX_ROWS - r]
    for row in body:
        r += 1
        cells = "".join(_cell(f"{col_letter(i + 1)}{r}", v, links) for i, v in enumerate(list(row)[:ncols]))
        rows_xml.append(f'<row r="{r}">{cells}</row>')
    last = col_letter(ncols)
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w:.1f}" customWidth="1"/>'
                   for i, w in enumerate(_widths(sheet)))
    pane = ""
    if sheet.freeze:
        pane = (f'<pane ySplit="{head_row}" topLeftCell="A{head_row + 1}" activePane="bottomLeft" state="frozen"/>'
                f'<selection pane="bottomLeft" activeCell="A{head_row + 1}" sqref="A{head_row + 1}"/>')
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
           f'<dimension ref="A1:{last}{max(r, 1)}"/>'
           f'<sheetViews><sheetView workbookViewId="0">{pane}</sheetView></sheetViews>'
           '<sheetFormatPr defaultRowHeight="15"/>'
           f'<cols>{cols}</cols><sheetData>{"".join(rows_xml)}</sheetData>')
    if sheet.autofilter and sheet.headers:
        xml += f'<autoFilter ref="A{head_row}:{last}{max(r, head_row)}"/>'
    if links:
        xml += "<hyperlinks>" + "".join(f'<hyperlink ref="{ref}" r:id="rId{i + 1}"/>'
                                        for i, (ref, _) in enumerate(links)) + "</hyperlinks>"
    xml += '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>'
    return xml, [u for _, u in links]


def _rels(urls: list[str]) -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                      f'relationships/hyperlink" Target="{escape(u, {chr(34): "&quot;"})}" TargetMode="External"/>'
                      for i, u in enumerate(urls)) + "</Relationships>")


def build_workbook(sheets: Sequence[Sheet], *, title: str = "", created: datetime | None = None) -> bytes:
    """The bytes of an .xlsx file with these sheets. At least one sheet is needed."""
    if not sheets:
        raise ValueError("A workbook needs at least one sheet.")
    created = created or datetime.now(timezone.utc)
    stamp = created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    taken: set[str] = set()
    names = [safe_sheet_name(s.name, taken) for s in sheets]
    ct = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'spreadsheetml.sheet.main+xml"/>',
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'spreadsheetml.styles+xml"/>',
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.'
          'core-properties+xml"/>',
          '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'extended-properties+xml"/>']
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        wb_sheets, wb_rels = [], []
        for i, (sheet, name) in enumerate(zip(sheets, names), start=1):
            xml, urls = _sheet_xml(sheet)
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
            if urls:
                z.writestr(f"xl/worksheets/_rels/sheet{i}.xml.rels", _rels(urls))
            ct.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-'
                      'officedocument.spreadsheetml.worksheet+xml"/>')
            wb_sheets.append(f'<sheet name="{escape(name, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>')
            wb_rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                           f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
        n = len(sheets)
        wb_rels.append(f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                       'relationships/styles" Target="styles.xml"/>')
        defined = []
        for i, (sheet, name) in enumerate(zip(sheets, names)):
            if sheet.autofilter and sheet.headers:  # Excel wants the filter named too, or it may drop the buttons
                head_row = len(sheet.preamble) + 1
                defined.append(f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
                               f"{escape(chr(39) + name.replace(chr(39), chr(39) * 2) + chr(39))}!$A${head_row}:"
                               f"${col_letter(len(sheet.headers))}${head_row + len(sheet.rows)}</definedName>")
        filters = f"<definedNames>{''.join(defined)}</definedNames>" if defined else ""
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   + "".join(ct) + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                   'officeDocument" Target="xl/workbook.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/'
                   'metadata/core-properties" Target="docProps/core.xml"/>'
                   '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                   'extended-properties" Target="docProps/app.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<bookViews><workbookView activeTab="0"/></bookViews>'
                   f'<sheets>{"".join(wb_sheets)}</sheets>{filters}</workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(wb_rels) + "</Relationships>")
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("docProps/core.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
                   'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
                   'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                   f'<dc:title>{escape(clean_text(title))}</dc:title><dc:creator>Talent Search</dc:creator>'
                   f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
                   f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified></cp:coreProperties>')
        z.writestr("docProps/app.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
                   '<Application>Talent Search</Application></Properties>')
    return buf.getvalue()
