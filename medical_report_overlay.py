#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
medical_report_overlay.py
==========================================================================
Builds the per-patient medical report PDF by drawing text directly on top
of a pre-exported PDF version of your Word template (no Word/COM needed
at runtime).

ONE-TIME SETUP (do this once, on your machine, before running the
pipeline):
  1. Open your existing medical_report_template.docx in Word.
  2. File -> Export -> Create PDF/XPS, save it as medical_report_template.pdf
  3. Point MEDICAL_REPORT_TEMPLATE_PDF below at that file.
  4. Point ARABIC_FONT_PATH / ARABIC_FONT_PATH_BOLD at a TTF font that has
     BOTH Arabic and Latin glyphs (Tahoma.ttf / Tahomabd.ttf are what the
     template itself uses and are the safest choice - they ship with
     Windows at C:\\Windows\\Fonts\\tahoma.ttf and tahomabd.ttf).
     IMPORTANT: if you ever swap in a font that lacks Latin glyphs, any
     English portion of a mixed sentence will silently disappear rather
     than raising an error - always eyeball the first row's PDF.

WHAT THIS DRAWS (coordinates below were measured directly off your real
template PDF with PyMuPDF, not guessed):
  - Name / National ID, next to the existing "Name:" / "National ID:"
    labels near the top of the page.
  - The clinical statement box (the box that, in the original template,
    permanently contains the breast-cancer sentence). For EVERY tumor
    type - including breast - this box is first painted over with a
    white rectangle, then the correct sentence for that row is drawn
    fresh. This is the fix for the bug where non-breast reports used to
    show the breast sentence pasted in ahead of the real one: masking
    must happen unconditionally, not only for non-breast rows, because
    we are now drawing the complete statement ourselves in every case
    rather than appending to whatever the template already has baked in.

SENTENCE FORMAT PER TUMOR TYPE (tumor_cfg comes from
Unified_Decree_Submission_Pipeline.TUMOR_TYPE_CONFIG):
  - If tumor_cfg["opening_statement"] is a non-empty string (currently:
    Breast Cancer's real template wording, and Blood Type Tumor's
    supplied clinical phrase): draw that Arabic phrase verbatim,
    right-to-left, right-aligned, then append the treatment plan after
    it - exactly as the template's own baked-in breast sentence reads.
    This is the "unique Arabic style" that stays as-is.
  - Otherwise (every other/generic tumor type, opening_statement is
    None): draw an ENGLISH sentence instead of the Arabic diagnosis
    name:
        "A patient of {English label} for {plan}"
    e.g. "A patient of Pancreatic Cancer for FOLFIRINOX x6 cycles".
    tumor_cfg["label"] is already the plain-English name (e.g.
    "Pancreatic Cancer", "Bone Cancer") - lower-cased here so it reads
    naturally inside the sentence ("a patient of pancreatic cancer for
    ..."). The plan text (Column B, whatever the user typed - English,
    Arabic, or mixed) is inserted VERBATIM after "for", exactly as
    supplied, with no translation or reformatting. Because the sentence
    is now English-first, it is drawn left-to-right, left-aligned in
    the box - unlike the Arabic breast/blood-tumor case above. Any
    Arabic text that happens to be inside the pasted-in plan is still
    correctly reshaped/connected via arabic_reshaper + python-bidi, it
    just sits inside an overall left-to-right paragraph.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Dict, Optional

import fitz  # PyMuPDF
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PyPDF2 import PdfReader, PdfWriter

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    _BIDI_AVAILABLE = True
except ImportError:
    _BIDI_AVAILABLE = False

# =====================================================================
# ONE-TIME SETUP - edit these three paths for your machine
# =====================================================================

MEDICAL_REPORT_TEMPLATE_PDF = Path(r"D:\MDT_Medical_Report_Template\medical_report_template.pdf")

# Tahoma ships with Windows and has full Arabic + Latin coverage, which
# is required since the generic sentence now mixes an English scaffold
# with a plan that may itself contain Arabic text.
ARABIC_FONT_PATH = r"C:\Windows\Fonts\tahoma.ttf"
ARABIC_FONT_PATH_BOLD = r"C:\Windows\Fonts\tahomabd.ttf"

_FONT_NAME = "ReportArabic"
_FONT_NAME_BOLD = "ReportArabicBold"

# =====================================================================
# Coordinates measured directly off the real template PDF (PyMuPDF
# coordinate system: origin top-left, y increases downward, points).
# =====================================================================

# "Name:" / "National ID:" label boxes - values are drawn just to the
# right of each label, same baseline.
_NAME_LABEL_BBOX = (77.64, 92.91, 106.52, 104.42)
_NATIONAL_ID_LABEL_BBOX = (77.64, 102.15, 124.41, 113.66)
_ID_FIELD_FONT_SIZE = 8.04

# The clinical-statement box. The visible bordered rectangle around the
# whole statement area (outer box the template draws) plus a little
# inner padding - this is what gets masked (painted white) before the
# real sentence for this row is drawn, and is also the wrap width used
# for that sentence.
_STATEMENT_BOX = (72.5, 505.7, 540.0, 596.9)   # x0, y0, x1, y1
_STATEMENT_FONT_SIZE = 8.04
_STATEMENT_LINE_HEIGHT = 11.5
_STATEMENT_PAD_X = 8.0
_STATEMENT_PAD_TOP = 5.0

_fonts_registered = False
_TEMPLATE_BYTES_CACHE: Optional[bytes] = None
_TEMPLATE_PAGE_SIZE_CACHE = None  # (width, height) in points


def _register_fonts():
    global _fonts_registered
    if _fonts_registered:
        return
    pdfmetrics.registerFont(TTFont(_FONT_NAME, ARABIC_FONT_PATH))
    pdfmetrics.registerFont(TTFont(_FONT_NAME_BOLD, ARABIC_FONT_PATH_BOLD))
    _fonts_registered = True


def _shape_arabic(text: str) -> str:
    """Reshapes+reorders text for correct rendering when the paragraph's
    base direction is RIGHT-TO-LEFT (i.e. the breast/blood-tumor literal
    Arabic phrases). Any embedded Latin runs are left as-is by
    arabic_reshaper/bidi other than being placed correctly in the
    overall RTL line."""
    if not _BIDI_AVAILABLE:
        return text
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


def _shape_mixed_ltr(text: str) -> str:
    """Reshapes+reorders text for correct rendering when the paragraph's
    base direction is LEFT-TO-RIGHT (the new generic English-scaffold
    sentence, which may still contain an Arabic plan pasted in after
    'for'). python-bidi's get_display() with base_dir='L' keeps the
    overall paragraph LTR while still correctly reshaping/reordering any
    embedded Arabic run in place."""
    if not _BIDI_AVAILABLE:
        return text
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped, base_dir="L")


def _get_template_bytes() -> bytes:
    global _TEMPLATE_BYTES_CACHE
    if _TEMPLATE_BYTES_CACHE is None:
        _TEMPLATE_BYTES_CACHE = Path(MEDICAL_REPORT_TEMPLATE_PDF).read_bytes()
    return _TEMPLATE_BYTES_CACHE


def _get_template_page_size():
    global _TEMPLATE_PAGE_SIZE_CACHE
    if _TEMPLATE_PAGE_SIZE_CACHE is None:
        doc = fitz.open(stream=_get_template_bytes(), filetype="pdf")
        page = doc[0]
        _TEMPLATE_PAGE_SIZE_CACHE = (page.rect.width, page.rect.height)
        doc.close()
    return _TEMPLATE_PAGE_SIZE_CACHE


def _wrap_text(text: str, font_name: str, font_size: float, max_width: float,
                is_rtl_source: bool) -> list:
    """Word-wraps `text` (BEFORE shaping) to fit max_width, measuring with
    the plain (unshaped) string - reportlab's stringWidth is shape-
    agnostic for our purposes since Arabic presentation-form glyphs in
    Tahoma are essentially the same advance width family as the base
    letters. Splits on whitespace, which works for both Arabic and
    English since both use spaces between words."""
    words = text.split(" ")
    lines = []
    current = []
    for w in words:
        candidate = (" ".join(current + [w])).strip()
        if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width or not current:
            current.append(w)
        else:
            lines.append(" ".join(current))
            current = [w]
    if current:
        lines.append(" ".join(current))
    return lines


def _draw_overlay(page_width: float, page_height: float, full_name: str,
                   national_id: str, tumor_cfg: Dict, plan_text: str) -> bytes:
    """Draws the dynamic overlay (name/ID + statement) as its own PDF page
    the same size as the template, ready to be merged on top of it."""
    _register_fonts()

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width, page_height))

    def to_reportlab_y(pymupdf_y: float) -> float:
        # PyMuPDF: y grows downward from top. reportlab: y grows upward
        # from bottom. Both share the same page height.
        return page_height - pymupdf_y

    # ---- Name / National ID -------------------------------------------------
    # FIXED BUG: full_name is a real Arabic name and was previously drawn
    # via c.drawString(..., full_name) completely RAW - i.e. the plain
    # logical-order Unicode string, with no shaping and no bidi reorder.
    # ReportLab (like any non-Arabic-aware renderer) then draws each
    # Arabic codepoint using its isolated glyph form and left-to-right
    # position, instead of the correct joined/contextual letterforms in
    # right-to-left order - which is exactly what produces "fractionated"
    # (disconnected) Arabic letters instead of a normal connected Arabic
    # name. The fix is the same shaping step already used everywhere else
    # in this file for Arabic text: reshape (arabic_reshaper) so each
    # letter gets its correct initial/medial/final/isolated glyph form,
    # then bidi-reorder (get_display) into left-to-right *visual* order so
    # a plain drawString renders it correctly. National ID is plain
    # digits, so shaping is a no-op for it, but it's run through the same
    # helper for consistency/safety in case a non-numeric ID format is
    # ever supplied.
    c.setFont(_FONT_NAME_BOLD, _ID_FIELD_FONT_SIZE)
    name_x = _NAME_LABEL_BBOX[2] + 4
    name_y = to_reportlab_y(_NAME_LABEL_BBOX[3]) + 1
    c.drawString(name_x, name_y, _shape_arabic(full_name))

    id_x = _NATIONAL_ID_LABEL_BBOX[2] + 4
    id_y = to_reportlab_y(_NATIONAL_ID_LABEL_BBOX[3]) + 1
    c.drawString(id_x, id_y, _shape_arabic(national_id))

    # ---- Statement box: mask THEN draw the correct sentence for every
    # tumor type (this unconditional masking is the fix - previously
    # only non-breast rows were masked, so breast rows kept showing the
    # template's own baked-in phrase underneath whatever was appended). -
    box_x0, box_y0, box_x1, box_y1 = _STATEMENT_BOX
    rl_box_y0 = to_reportlab_y(box_y1)   # lower edge in reportlab coords
    rl_box_y1 = to_reportlab_y(box_y0)   # upper edge in reportlab coords
    c.setFillColorRGB(1, 1, 1)
    c.rect(box_x0, rl_box_y0, box_x1 - box_x0, rl_box_y1 - rl_box_y0,
           stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)

    max_text_width = (box_x1 - box_x0) - 2 * _STATEMENT_PAD_X
    c.setFont(_FONT_NAME_BOLD, _STATEMENT_FONT_SIZE)

    opening_statement = tumor_cfg.get("opening_statement")
    if opening_statement:
        # --- Explicit Arabic clinical phrase (breast / blood tumor) ---
        # Kept exactly as the template's own style: right-to-left,
        # right-aligned, plan appended after the opening phrase.
        full_sentence = f"{opening_statement} {plan_text}".strip()
        lines = _wrap_text(full_sentence, _FONT_NAME_BOLD, _STATEMENT_FONT_SIZE,
                            max_text_width, is_rtl_source=True)
        text_y = to_reportlab_y(box_y0 + _STATEMENT_PAD_TOP + _STATEMENT_FONT_SIZE)
        for line in lines:
            shaped = _shape_arabic(line)
            c.drawRightString(box_x1 - _STATEMENT_PAD_X, text_y, shaped)
            text_y -= _STATEMENT_LINE_HEIGHT
    else:
        # --- Generic case: English cancer-type name, not Arabic. -------
        # "A patient of {English label} for {plan}" - plan inserted
        # verbatim (whatever the user put in Column B, any language).
        english_label = tumor_cfg.get("label", "").lower()
        full_sentence = f"A patient of {english_label} for {plan_text}".strip()
        lines = _wrap_text(full_sentence, _FONT_NAME_BOLD, _STATEMENT_FONT_SIZE,
                            max_text_width, is_rtl_source=False)
        text_y = to_reportlab_y(box_y0 + _STATEMENT_PAD_TOP + _STATEMENT_FONT_SIZE)
        for line in lines:
            shaped = _shape_mixed_ltr(line)
            c.drawString(box_x0 + _STATEMENT_PAD_X, text_y, shaped)
            text_y -= _STATEMENT_LINE_HEIGHT

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


def build_medical_report_pdf(full_name: str, national_id: str, plan_text: str,
                              tumor_cfg: Dict) -> bytes:
    """Returns the finished, single-page medical report PDF as bytes:
    the template page with the dynamic name/ID/statement overlay merged
    on top of it.

    Args:
        full_name:    patient's full name, drawn next to "Name:".
        national_id:  patient's national ID, drawn next to "National ID:".
        plan_text:    Column B's treatment-plan text, verbatim.
        tumor_cfg:    one entry from TUMOR_TYPE_CONFIG (must contain at
                      least "label" and "opening_statement").
    """
    page_width, page_height = _get_template_page_size()
    overlay_bytes = _draw_overlay(page_width, page_height, full_name,
                                   national_id, tumor_cfg, plan_text)

    template_reader = PdfReader(io.BytesIO(_get_template_bytes()))
    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))

    writer = PdfWriter()
    template_page = template_reader.pages[0]
    template_page.merge_page(overlay_reader.pages[0])
    writer.add_page(template_page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()
