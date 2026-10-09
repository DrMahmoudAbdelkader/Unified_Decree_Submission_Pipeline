# -*- coding: utf-8 -*-
"""
signature_script.py - rebuilt from the pasted Signature_script.txt (indentation / comment markers
had been lost in the paste). Detection, placement, sizes and ink processing are UNCHANGED.
Only change: temp PNGs go to tempfile.gettempdir() instead of C:\\temp (works on the GitHub runner).
"""
import sys
import os
import io
import argparse
import glob
import tempfile
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:
    try:
        import fitz  # older PyMuPDF
    except ImportError:
        print("PyMuPDF is required. Install with: pip install PyMuPDF")
        sys.exit(1)

# ═══════════════════════════════════════════════
# DEFAULT CONFIGURATION
# ═══════════════════════════════════════════════
DEFAULT_SIG1 = r"C:\Users\drmah\Template_Signatures\1a.png"   # المراجع (Reviewer)
DEFAULT_SIG2 = r"C:\Users\drmah\Template_Signatures\2a.png"   # رئيس حسابات المرضى
DEFAULT_SIG3 = r"C:\Users\drmah\Template_Signatures\3a.png"   # الموظف المختص
DEFAULT_SIG4 = r"C:\Users\drmah\Template_Signatures\4.png"    # signature behind stamp
DEFAULT_STAMP = r"C:\Users\drmah\Template_Signatures\stamp.png"  # مدير الشؤون المالية

DEFAULT_PDF_FOLDER = r"C:\Users\drmah\pdfs"
DEFAULT_OUTPUT_FOLDER = r"C:\Users\drmah\pdfs\output"

SIG_VERTICAL_OFFSET = 35
STAMP_VERTICAL_OFFSET = 52

SIG_WIDTH_SIG1 = 200
SIG_HEIGHT_SIG1 = 105
SIG_WIDTH_SIG2 = 190
SIG_HEIGHT_SIG2 = 105
SIG_WIDTH_SIG3 = 210
SIG_HEIGHT_SIG3 = 105
SIG_WIDTH_SIG4 = 180
SIG_HEIGHT_SIG4 = 90
STAMP_SIZE = 150

# no image may draw with its bottom edge closer than this to the physical page bottom
MIN_BOTTOM_MARGIN_PT = 8

SIG_HORIZONTAL_SPACING = 8

BLUE_R, BLUE_G, BLUE_B = 30, 80, 200

SIG_STROKE_INTENSITY = 2.5
SIG_STROKE_GAMMA = 0.85
SIG_STROKE_DILATION = 1
SIGNATURE_OPACITY = 0.85

STAMP_STROKE_INTENSITY = 7.0
STAMP_STROKE_GAMMA = 0.30
STAMP_STROKE_DILATION = 3
STAMP_OPACITY = 1.0

# Detection settings (permissive)
DETECT_DPI = 150
BLUE_DOMINANCE_THRESH = 30
MIN_BLUE_VALUE = 70
DARK_THRESH = 160
MIN_CLUSTER_SPAN = 150
SEARCH_START_FRACTION = 0.45

TEXT_ROW_SEARCH_STRIDE = 3
MIN_COLUMN_GROUPS = 3

_TMP_DIR = tempfile.gettempdir()


# ════════════════════════════════════════════
# IMAGE PROCESSING - SEPARATE FOR SIGNATURES AND STAMP
# ════════════════════════════════════════════
def to_blue_transparent_signature(img_path: str) -> str:
    """Convert signature to NATURAL-looking blue ink."""
    img = Image.open(img_path).convert("RGB")
    img = ImageEnhance.Contrast(img).enhance(1.2)

    arr = np.array(img, dtype=np.float32)
    gray = arr.mean(axis=2)

    threshold = 30
    alpha_raw = ((gray - threshold) / (255 - threshold)).clip(0, 1)
    alpha_gamma = np.power(alpha_raw, SIG_STROKE_GAMMA)
    alpha_final = (alpha_gamma * SIG_STROKE_INTENSITY).clip(0, SIGNATURE_OPACITY)

    rgba = np.zeros((*arr.shape[:2], 4), dtype=np.uint8)
    rgba[:, :, 0] = BLUE_R
    rgba[:, :, 1] = BLUE_G
    rgba[:, :, 2] = BLUE_B
    rgba[:, :, 3] = (alpha_final * 255).astype(np.uint8)

    if SIG_STROKE_DILATION > 1:
        alpha_img = Image.fromarray(rgba[:, :, 3])
        for _ in range(SIG_STROKE_DILATION - 1):
            alpha_img = alpha_img.filter(ImageFilter.MaxFilter(3))
        rgba[:, :, 3] = np.array(alpha_img)

    tmp = os.path.join(_TMP_DIR, f"{os.path.basename(img_path)}__signature_tmp.png")
    Image.fromarray(rgba, "RGBA").save(tmp)
    return tmp


def to_blue_transparent_stamp(img_path: str) -> str:
    """Convert stamp to the ORIGINAL bold blue ink."""
    img = Image.open(img_path).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    gray = arr.mean(axis=2)

    threshold = 20
    alpha_raw = ((gray - threshold) / (255 - threshold)).clip(0, 1)
    alpha_gamma = np.power(alpha_raw, STAMP_STROKE_GAMMA)
    alpha_final = (alpha_gamma * STAMP_STROKE_INTENSITY).clip(0, STAMP_OPACITY)

    rgba = np.zeros((*arr.shape[:2], 4), dtype=np.uint8)
    rgba[:, :, 0] = 0
    rgba[:, :, 1] = 0
    rgba[:, :, 2] = 180
    rgba[:, :, 3] = (alpha_final * 255).astype(np.uint8)

    alpha_img = Image.fromarray(rgba[:, :, 3])
    for _ in range(STAMP_STROKE_DILATION - 1):
        alpha_img = alpha_img.filter(ImageFilter.MaxFilter(3))
    rgba[:, :, 3] = np.array(alpha_img)

    tmp = os.path.join(_TMP_DIR, f"{os.path.basename(img_path)}__stamp_tmp.png")
    Image.fromarray(rgba, "RGBA").save(tmp)
    return tmp


def cleanup_tmp(tmp: str, original: str):
    if tmp != original and ("__signature_tmp" in tmp or "__stamp_tmp" in tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


# ════════════════════════════════════════════
# LABEL ROW DETECTION (PyMuPDF)
# ════════════════════════════════════════════
def group_cols(cols, gap_pts, scale_x):
    """Cluster pixel column indices into groups separated by gap_pts."""
    groups = []
    cs = cp = int(cols[0])
    for c in cols[1:]:
        c = int(c)
        if (c - cp) / scale_x > gap_pts:
            groups.append((cs / scale_x, cp / scale_x))
            cs = c
        cp = c
    groups.append((cs / scale_x, cp / scale_x))
    return groups


def merge_to_n(groups, n=4):
    """Merge the closest pair of groups until exactly n remain."""
    while len(groups) > n:
        gaps = sorted((groups[i + 1][0] - groups[i][1], i) for i in range(len(groups) - 1))
        i = gaps[0][1]
        groups[i] = (groups[i][0], groups[i + 1][1])
        del groups[i + 1]
    return groups


def _try_detect(arr, start_px, h_px, w_px, pdf_w, pdf_h, scale_x, scale_y):
    """Blue-text then dark-text detection inside the band [start_px, h_px).
    Returns (top_y_pt, bot_y_pt, col_groups, method_name) or None."""
    detection = None

    # ── Method 1: blue-text labels (digital PDFs) ──
    r = arr[:, :, 0].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)
    blue_mask = ((b - r) > BLUE_DOMINANCE_THRESH) & (b > MIN_BLUE_VALUE)
    blue_lower = np.where(blue_mask[start_px:, :].any(axis=1))[0] + start_px

    if len(blue_lower) >= 3:
        clusters = []
        s = p = int(blue_lower[0])
        for row in blue_lower[1:]:
            row = int(row)
            if row - p > 15:
                clusters.append((s, p))
                s = row
            p = row
        clusters.append((s, p))

        top_px, bot_px = clusters[-1]
        row_slice = blue_mask[top_px: bot_px + 1, :]
        blue_cols = np.where(row_slice.any(axis=0))[0]

        if len(blue_cols) >= 3:
            col_groups = group_cols(blue_cols, gap_pts=6, scale_x=scale_x)
            merged = []
            cl, cr = col_groups[0]
            for l, r2 in col_groups[1:]:
                if l - cr < 25:
                    cr = r2
                else:
                    merged.append((cl, cr))
                    cl, cr = l, r2
            merged.append((cl, cr))

            if len(merged) >= 3:
                # 3 columns detected -> duplicate the middle one to make 4
                while len(merged) < 4:
                    mid_idx = len(merged) // 2
                    merged.insert(mid_idx, merged[mid_idx])

                # more than 4 -> keep the most evenly spaced window of 4
                if len(merged) > 4:
                    best_4, best_score = None, float("inf")
                    for i in range(len(merged) - 3):
                        subset = merged[i:i + 4]
                        spaces = [subset[j + 1][0] - subset[j][1] for j in range(3)]
                        variance = sum((s_ - sum(spaces) / len(spaces)) ** 2 for s_ in spaces)
                        if variance < best_score:
                            best_score, best_4 = variance, subset
                    merged = best_4 if best_4 else merged[:4]

                detection = (top_px / scale_y, bot_px / scale_y, merged, "blue-text")

    # ── Method 2: dark-text labels (scanned PDFs / black print) ──
    if detection is None:
        gray_img = arr.mean(axis=2)
        best_row, best_score = None, -1

        for y_px in range(start_px, h_px, TEXT_ROW_SEARCH_STRIDE):
            row = gray_img[y_px, :]
            dark_cols = np.where(row < DARK_THRESH)[0]
            if len(dark_cols) < 5:
                continue
            groups = group_cols(dark_cols, gap_pts=15, scale_x=scale_x)
            groups = [(l, r2) for (l, r2) in groups if r2 < pdf_w - 20]
            if len(groups) < MIN_COLUMN_GROUPS:
                continue
            span = groups[-1][1] - groups[0][0]
            score = span * min(len(groups), 4)
            if score > best_score:
                best_score = score
                best_row = (y_px, groups)

        if best_row is not None:
            y_px, groups = best_row
            band_dark = gray_img[max(0, y_px - 20): y_px + 20, :] < DARK_THRESH
            rows_with_dark = np.where(band_dark.any(axis=1))[0]
            top_px = (y_px - 20 + rows_with_dark[0]) if len(rows_with_dark) else y_px
            bot_px = (y_px - 20 + rows_with_dark[-1]) if len(rows_with_dark) else y_px + 15
            top_px = max(0, top_px)

            if len(groups) >= 4:
                groups = groups[:4]
            elif len(groups) >= 3:
                groups = list(groups)
                groups.insert(len(groups) // 2, groups[len(groups) // 2])
            else:
                first_x = groups[0][0] if groups else 0
                last_x = groups[-1][1] if groups else pdf_w
                group_width = (last_x - first_x) / 4
                groups = [(first_x + i * group_width, first_x + (i + 1) * group_width - 2)
                          for i in range(4)]

            detection = (top_px / scale_y, bot_px / scale_y, groups, "dark-text")

    return detection


def detect_label_row(pdf_path: str, page_index: int = 0) -> dict:
    """Pass 1: bottom part of the page. Pass 2 (if nothing found): the whole page
    (on multi-page PDFs the label row can land near the TOP of the last page)."""
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    pdf_w, pdf_h = page.rect.width, page.rect.height

    zoom = DETECT_DPI / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()

    arr = np.array(img)
    h_px, w_px = arr.shape[:2]
    scale_x, scale_y = w_px / pdf_w, h_px / pdf_h

    start_px = int(SEARCH_START_FRACTION * h_px)
    result = _try_detect(arr, start_px, h_px, w_px, pdf_w, pdf_h, scale_x, scale_y)

    if result is not None:
        print(f"  [detection] Used {result[3]} method (bottom-half pass).")
    else:
        print("  [detection] Bottom-half search failed - retrying full page scan.")
        result = _try_detect(arr, 0, h_px, w_px, pdf_w, pdf_h, scale_x, scale_y)
        if result is not None:
            print(f"  [detection] Used {result[3]} method (full-page pass).")
        else:
            raise RuntimeError(f"Could not detect label row in {pdf_path}")

    label_top_y, label_bot_y, merged, _ = result

    role_order = ["stamp", "sig2", "sig1", "sig3"]
    columns = {}
    for idx, (x_left, x_right) in enumerate(merged):
        role = role_order[idx] if idx < len(role_order) else f"unknown_{idx}"
        columns[role] = {"x_center": (x_left + x_right) / 2, "x_left": x_left, "x_right": x_right}

    return dict(label_top_y=label_top_y, label_bot_y=label_bot_y,
                pdf_w=pdf_w, pdf_h=pdf_h, columns=columns)


# ════════════════════════════════════════════
# OVERLAY BUILDER
# ════════════════════════════════════════════
def build_overlay(detection: dict, sig1_path: str, sig2_path: str, sig3_path: str,
                  sig4_path: str, stamp_path: str) -> bytes:
    pdf_w, pdf_h = detection["pdf_w"], detection["pdf_h"]
    cols = detection["columns"]
    label_bot_bo = pdf_h - detection["label_bot_y"]

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(pdf_w, pdf_h))
    tmp_paths = []

    w1 = SIG_WIDTH_SIG1 - SIG_HORIZONTAL_SPACING
    w2 = SIG_WIDTH_SIG2 - SIG_HORIZONTAL_SPACING
    w3 = SIG_WIDTH_SIG3 - SIG_HORIZONTAL_SPACING
    w4 = SIG_WIDTH_SIG4 - SIG_HORIZONTAL_SPACING

    # 4th signature FIRST (behind the stamp)
    if "stamp" in cols:
        print("  [Processing] 4th signature (behind stamp)")
        ready_sig4 = to_blue_transparent_signature(sig4_path)
        tmp_paths.append((ready_sig4, sig4_path))
        cx = cols["stamp"]["x_center"]
        img_y = max(label_bot_bo - SIG_VERTICAL_OFFSET - SIG_HEIGHT_SIG4 / 2, MIN_BOTTOM_MARGIN_PT)
        c.drawImage(ready_sig4, cx - w4 / 2, img_y, width=w4, height=SIG_HEIGHT_SIG4,
                    preserveAspectRatio=True, mask="auto")
        print(f"  [OK] sig4 (behind stamp) x_center={cx:.1f}")

    # STAMP
    if "stamp" in cols:
        ready = to_blue_transparent_stamp(stamp_path)
        tmp_paths.append((ready, stamp_path))
        cx = cols["stamp"]["x_center"]
        img_y = max(label_bot_bo - STAMP_VERTICAL_OFFSET - STAMP_SIZE / 2, MIN_BOTTOM_MARGIN_PT)
        c.drawImage(ready, cx - STAMP_SIZE / 2, img_y, width=STAMP_SIZE, height=STAMP_SIZE,
                    preserveAspectRatio=True, mask="auto")
        print(f"  [OK] stamp x_center={cx:.1f}")

    role_config = [
        ("sig2", sig2_path, w2, SIG_HEIGHT_SIG2, SIG_VERTICAL_OFFSET),
        ("sig1", sig1_path, w1, SIG_HEIGHT_SIG1, SIG_VERTICAL_OFFSET),
        ("sig3", sig3_path, w3, SIG_HEIGHT_SIG3, SIG_VERTICAL_OFFSET),
    ]
    for role, img_path, w_pts, h_pts, v_off in role_config:
        if role not in cols:
            print(f"  [WARN] Role '{role}' not detected - skipping.")
            continue
        ready = to_blue_transparent_signature(img_path)
        tmp_paths.append((ready, img_path))
        cx = cols[role]["x_center"]
        img_y = max(label_bot_bo - v_off - h_pts / 2, MIN_BOTTOM_MARGIN_PT)
        c.drawImage(ready, cx - w_pts / 2, img_y, width=w_pts, height=h_pts,
                    preserveAspectRatio=True, mask="auto")
        print(f"  [OK] {role:6s} x_center={cx:.1f}")

    c.save()
    buf.seek(0)
    for tmp, orig in tmp_paths:
        cleanup_tmp(tmp, orig)
    return buf.read()


# ════════════════════════════════════════════
# MERGE ONTO ORIGINAL PDF
# ════════════════════════════════════════════
def apply_overlay(input_path: str, overlay_bytes: bytes, output_path: str, page_index: int = 0):
    reader = PdfReader(input_path)
    writer = PdfWriter()
    overlay = PdfReader(io.BytesIO(overlay_bytes)).pages[0]
    for i, page in enumerate(reader.pages):
        if i == page_index:
            page.merge_page(overlay)
        writer.add_page(page)
    with open(output_path, "wb") as f:
        writer.write(f)
    print(f"  Saved -> {output_path}")


# ════════════════════════════════════════════
# SIGN SINGLE PDF
# ════════════════════════════════════════════
def _find_last_content_page(pdf_path: str, total_pages: int) -> int:
    """Last page with visible text (skips a blank trailing page from print rounding)."""
    doc = fitz.open(pdf_path)
    try:
        for i in range(total_pages - 1, -1, -1):
            if len(doc[i].get_text().strip()) > 0:
                return i
        return total_pages - 1
    finally:
        doc.close()


def sign_pdf(input_pdf: str, output_pdf: str, sig1: str, sig2: str, sig3: str,
             sig4: str, stamp: str, page_index: int = 0):
    """detect labels -> overlay signatures + stamp -> merge.
    Multi-page PDFs: the last content-bearing page is used automatically."""
    print(f"\n{'=' * 62}\n  Signing: {os.path.basename(input_pdf)}\n{'=' * 62}")

    total_pages = len(PdfReader(input_pdf).pages)
    if total_pages > 1:
        target_page = _find_last_content_page(input_pdf, total_pages)
        print(f"  Multi-page PDF ({total_pages} pages) -> signing page index {target_page}")
    else:
        target_page = page_index
        print(f"  Single-page PDF -> signing page index {target_page}")

    print("\n[1/3] Detecting label row ...")
    det = detect_label_row(input_pdf, page_index=target_page)
    print(f"  Label row y: {det['label_top_y']:.1f} - {det['label_bot_y']:.1f} pt")
    for role, info in det["columns"].items():
        print(f"    {role:8s}: cx={info['x_center']:.1f} ({info['x_left']:.0f}-{info['x_right']:.0f})")

    print("\n[2/3] Building overlay ...")
    overlay_bytes = build_overlay(det, sig1, sig2, sig3, sig4, stamp)

    print("\n[3/3] Merging ...")
    apply_overlay(input_pdf, overlay_bytes, output_pdf, target_page)
    print("\nDone.\n")


# ════════════════════════════════════════════
# BATCH + MAIN (unchanged behaviour)
# ════════════════════════════════════════════
def batch_sign_pdfs(pdf_folder, output_folder, sig1, sig2, sig3, sig4, stamp, recursive=False):
    Path(output_folder).mkdir(parents=True, exist_ok=True)
    pattern = os.path.join(pdf_folder, "**", "*.pdf") if recursive else os.path.join(pdf_folder, "*.pdf")
    pdf_files = glob.glob(pattern, recursive=recursive)
    if not pdf_files:
        print(f"No PDF files found in {pdf_folder}")
        return
    ok = bad = 0
    for pdf_file in pdf_files:
        try:
            out = os.path.join(output_folder, os.path.relpath(pdf_file, pdf_folder))
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            sign_pdf(pdf_file, out, sig1, sig2, sig3, sig4, stamp)
            ok += 1
        except Exception as e:
            print(f"Failed to sign {os.path.basename(pdf_file)}: {e}")
            bad += 1
    print(f"BATCH SIGNING COMPLETE  ok={ok} failed={bad}")


def main():
    ap = argparse.ArgumentParser(description="Batch sign PDF files - natural signatures + stamp")
    ap.add_argument("--pdf-folder", default=DEFAULT_PDF_FOLDER)
    ap.add_argument("--output-folder", default=DEFAULT_OUTPUT_FOLDER)
    ap.add_argument("--sig1", default=DEFAULT_SIG1)
    ap.add_argument("--sig2", default=DEFAULT_SIG2)
    ap.add_argument("--sig3", default=DEFAULT_SIG3)
    ap.add_argument("--sig4", default=DEFAULT_SIG4)
    ap.add_argument("--stamp", default=DEFAULT_STAMP)
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--page", type=int, default=0)
    a = ap.parse_args()
    missing = [f"{n}: {p}" for n, p in [("sig1", a.sig1), ("sig2", a.sig2), ("sig3", a.sig3),
                                       ("sig4", a.sig4), ("stamp", a.stamp)] if not os.path.exists(p)]
    if missing:
        print("Missing signature files:\n  " + "\n  ".join(missing))
        sys.exit(1)
    if not os.path.exists(a.pdf_folder):
        print(f"PDF folder '{a.pdf_folder}' does not exist.")
        sys.exit(1)
    batch_sign_pdfs(a.pdf_folder, a.output_folder, a.sig1, a.sig2, a.sig3, a.sig4, a.stamp, a.recursive)


if __name__ == "__main__":
    main()
