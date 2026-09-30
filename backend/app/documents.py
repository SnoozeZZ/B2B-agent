from __future__ import annotations

import os
from functools import partial
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    KeepTogether,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .domain.redaction import find_sensitive_paths
from .models import Quote

LABELS = {
    "brand": "品牌",
    "sku": "SKU/货号",
    "currency": "币种",
    "unit_price": "报价单价",
    "moq": "MOQ",
    "trade_term": "贸易条款",
    "payment_terms": "付款条款",
    "lead_time": "发货周期",
    "stock_type": "库存类型",
}

_CID_FONT = "STSong-Light"
_EMBEDDED_FONT = "QuoteCJK"
_NAVY = colors.HexColor("#17263A")
_BLUE = colors.HexColor("#176B87")
_PALE_BLUE = colors.HexColor("#EAF3F6")
_SLATE = colors.HexColor("#344256")
_MUTED = colors.HexColor("#6E7B8B")
_BORDER = colors.HexColor("#D9E1E8")
_ROW_FILL = colors.HexColor("#F7F9FB")


def downstream_quote_filename(quote: Quote, extension: str = "pdf") -> str:
    """Return a stable filename that never includes customer or supplier data."""

    normalized_extension = extension.lower().lstrip(".")
    if normalized_extension not in {"pdf", "xlsx"}:
        raise ValueError("不支持的报价文件格式")
    return f"quote-{quote.id}-v{quote.version}.{normalized_extension}"


def _assert_downstream_safe(data: Any) -> None:
    leaked = find_sensitive_paths(data)
    if leaked:
        raise ValueError(f"报价快照包含敏感字段：{', '.join(leaked)}")


def _register_pdf_fonts() -> str:
    configured_font = os.getenv("QUOTE_PDF_FONT_PATH")
    font_candidates = [
        configured_font,
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    try:
        pdfmetrics.getFont(_EMBEDDED_FONT)
        return _EMBEDDED_FONT
    except KeyError:
        pass

    for candidate in font_candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont(_EMBEDDED_FONT, candidate, subfontIndex=0))
            return _EMBEDDED_FONT
        except (OSError, TTFError):
            continue

    try:
        pdfmetrics.getFont(_CID_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(_CID_FONT))
    return _CID_FONT


def _paragraph(value: Any, style: ParagraphStyle) -> Paragraph:
    if value is None or value == "":
        text = "—"
    else:
        text = escape(str(value)).replace("\n", "<br/>")
    return Paragraph(text, style)


def _quote_number(quote: Quote) -> str:
    return f"Q-{quote.id:06d}-V{quote.version}"


def _draw_pdf_page(
    canvas_: canvas.Canvas,
    document: SimpleDocTemplate,
    *,
    font_name: str,
) -> None:
    canvas_.saveState()
    canvas_.setTitle("B2B Quote")
    canvas_.setAuthor("")
    canvas_.setSubject("Commercial quotation")
    canvas_.setKeywords("")

    page_width, page_height = A4
    canvas_.setFillColor(_NAVY)
    canvas_.rect(0, page_height - 12 * mm, page_width, 12 * mm, stroke=0, fill=1)
    canvas_.setFillColor(colors.white)
    canvas_.setFont(font_name, 8.5)
    canvas_.drawString(18 * mm, page_height - 7.6 * mm, "B2B 商务报价")

    canvas_.setStrokeColor(_BORDER)
    canvas_.line(18 * mm, 14 * mm, page_width - 18 * mm, 14 * mm)
    canvas_.setFillColor(_MUTED)
    canvas_.setFont(font_name, 7.5)
    canvas_.drawString(18 * mm, 9 * mm, "本报价以最终确认的合同条款为准")
    canvas_.drawRightString(page_width - 18 * mm, 9 * mm, f"第 {document.page} 页")
    canvas_.restoreState()


def build_downstream_quote_pdf(quote: Quote) -> bytes:
    """Build a customer-facing PDF from the allowlisted downstream snapshot."""

    data = quote.downstream_snapshot or {}
    _assert_downstream_safe(data)
    font_name = _register_pdf_fonts()

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=22 * mm,
        bottomMargin=20 * mm,
        title="B2B Quote",
        author="",
        subject="Commercial quotation",
        pageCompression=1,
        invariant=1,
    )

    sample = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "QuoteTitle",
        parent=sample["Title"],
        fontName=font_name,
        fontSize=23,
        leading=30,
        textColor=_NAVY,
        alignment=TA_LEFT,
        spaceAfter=2 * mm,
    )
    subtitle_style = ParagraphStyle(
        "QuoteSubtitle",
        parent=sample["Normal"],
        fontName=font_name,
        fontSize=9,
        leading=13,
        textColor=_MUTED,
        alignment=TA_LEFT,
    )
    badge_style = ParagraphStyle(
        "QuoteBadge",
        parent=sample["Normal"],
        fontName=font_name,
        fontSize=9,
        leading=13,
        textColor=colors.white,
        alignment=TA_CENTER,
    )
    label_style = ParagraphStyle(
        "QuoteLabel",
        parent=sample["Normal"],
        fontName=font_name,
        fontSize=9,
        leading=14,
        textColor=_SLATE,
        alignment=TA_LEFT,
    )
    table_header_style = ParagraphStyle(
        "QuoteTableHeader",
        parent=label_style,
        textColor=colors.white,
    )
    value_style = ParagraphStyle(
        "QuoteValue",
        parent=sample["Normal"],
        fontName=font_name,
        fontSize=10,
        leading=15,
        textColor=_NAVY,
        alignment=TA_LEFT,
        wordWrap="CJK",
    )
    price_style = ParagraphStyle(
        "QuotePrice",
        parent=value_style,
        fontSize=17,
        leading=22,
        textColor=_BLUE,
        alignment=TA_RIGHT,
    )
    note_style = ParagraphStyle(
        "QuoteNote",
        parent=sample["Normal"],
        fontName=font_name,
        fontSize=8,
        leading=12,
        textColor=_MUTED,
    )

    created_at = getattr(quote, "created_at", None)
    issued_on = created_at.strftime("%Y-%m-%d") if created_at else "—"
    heading = Table(
        [
            [
                [
                    Paragraph("商务报价单", title_style),
                    Paragraph("COMMERCIAL QUOTATION", subtitle_style),
                ],
                Paragraph(f"报价编号<br/><b>{_quote_number(quote)}</b>", badge_style),
            ]
        ],
        colWidths=[119 * mm, 38 * mm],
    )
    heading.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (1, 0), (1, 0), _BLUE),
                ("BOX", (1, 0), (1, 0), 0.4, _BLUE),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), 8 * mm),
                ("TOPPADDING", (1, 0), (1, 0), 4 * mm),
                ("BOTTOMPADDING", (1, 0), (1, 0), 4 * mm),
            ]
        )
    )

    currency = data.get("currency") or ""
    unit_price = data.get("unit_price")
    price_text = "—" if unit_price in (None, "") else f"{currency} {unit_price}".strip()
    summary = Table(
        [
            [
                _paragraph(f"签发日期\n{issued_on}", label_style),
                _paragraph("报价版本\nv" + str(quote.version), label_style),
                Paragraph(escape(price_text), price_style),
            ]
        ],
        colWidths=[48 * mm, 40 * mm, 69 * mm],
    )
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _PALE_BLUE),
                ("BOX", (0, 0), (-1, -1), 0.5, _BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
            ]
        )
    )

    rows = [
        [
            Paragraph("项目", table_header_style),
            Paragraph("报价内容", table_header_style),
        ],
    ]
    for key, label in LABELS.items():
        # Unit price is highlighted in the summary and repeated here for a printable record.
        rows.append([Paragraph(label, label_style), _paragraph(data.get(key), value_style)])

    details = LongTable(
        rows,
        colWidths=[42 * mm, 115 * mm],
        repeatRows=1,
        splitByRow=1,
        splitInRow=1,
        hAlign="LEFT",
    )
    details.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BACKGROUND", (0, 1), (0, -1), _ROW_FILL),
                ("BOX", (0, 0), (-1, -1), 0.5, _BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, _BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )

    story = [
        heading,
        Spacer(1, 8 * mm),
        summary,
        Spacer(1, 8 * mm),
        details,
        Spacer(1, 6 * mm),
        KeepTogether(
            [
                Paragraph("说明", label_style),
                Spacer(1, 1.5 * mm),
                Paragraph(
                    "价格、库存与交期以双方最终确认结果为准。本文件仅包含对外报价信息。",
                    note_style,
                ),
            ]
        ),
    ]
    draw_page = partial(_draw_pdf_page, font_name=font_name)
    document.build(
        story,
        onFirstPage=draw_page,
        onLaterPages=draw_page,
        canvasmaker=partial(canvas.Canvas, invariant=1),
    )
    return output.getvalue()


def build_downstream_quote_xlsx(quote: Quote) -> bytes:
    data = quote.downstream_snapshot
    _assert_downstream_safe(data)

    workbook = Workbook()
    workbook.properties.creator = ""
    workbook.properties.lastModifiedBy = ""
    workbook.properties.title = "B2B 报价单"
    workbook.properties.subject = ""
    workbook.properties.keywords = ""
    workbook.properties.description = ""
    sheet = workbook.active
    sheet.title = "报价单"
    sheet.sheet_view.showGridLines = False
    sheet.merge_cells("A1:B1")
    title = sheet["A1"]
    title.value = "B2B 报价单"
    title.font = Font(name="Arial", size=18, bold=True, color="FFFFFF")
    title.fill = PatternFill("solid", fgColor="17263A")
    title.alignment = Alignment(horizontal="center", vertical="center")
    sheet.row_dimensions[1].height = 34

    thin = Side(style="thin", color="D9DEE5")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for row_index, key in enumerate(LABELS, start=3):
        label_cell = sheet.cell(row=row_index, column=1, value=LABELS[key])
        value_cell = sheet.cell(row=row_index, column=2, value=data.get(key) or "—")
        label_cell.font = Font(bold=True, color="344256")
        label_cell.fill = PatternFill("solid", fgColor="EDF1F5")
        for cell in (label_cell, value_cell):
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.row_dimensions[row_index].height = 28

    footer_row = len(LABELS) + 5
    sheet.merge_cells(start_row=footer_row, start_column=1, end_row=footer_row, end_column=2)
    footer = sheet.cell(row=footer_row, column=1)
    footer.value = f"报价版本 v{quote.version} · 报价编号 {quote.id}"
    footer.font = Font(size=9, color="7C8795")
    footer.alignment = Alignment(horizontal="right")
    widths = {1: 20, 2: 44}
    for column, width in widths.items():
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.freeze_panes = "A3"
    sheet.protection.sheet = True
    sheet.protection.enable()

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
