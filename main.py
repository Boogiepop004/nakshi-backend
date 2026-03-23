from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pyembroidery
import tempfile
import os
import math
import re

app = FastAPI(title="NAKSHI Embroidery API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "NAKSHI API running", "version": "1.0"}

@app.post("/parse")
async def parse_dst(file: UploadFile = File(...)):
    # Validate extension
    filename = file.filename.lower()
    allowed = ['.dst', '.pes', '.jef', '.exp', '.vp3', '.hus']
    if not any(filename.endswith(ext) for ext in allowed):
        raise HTTPException(400, "Unsupported file format")

    # Save to temp file — pyembroidery needs a file path
    suffix = os.path.splitext(filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # ── Read DST header FIRST for EmCAD's exact counts ──
        # EmCAD writes its own stitch count into the DST header
        # This is the AUTHORITATIVE count — matches what EmCAD displays
        raw_bytes = open(tmp_path, 'rb').read(512)
        header = raw_bytes.decode('ascii', errors='ignore')

        # Extract ST (stitch count) from header — written by EmCAD
        st_match  = re.search(r'ST\s+(\d+)', header)
        co_match  = re.search(r'CO\s+(\d+)', header)
        px_match  = re.search(r'\+X(\d+)', header)
        mx_match  = re.search(r'\-X(\d+)', header)
        py_match  = re.search(r'\+Y(\d+)', header)
        my_match  = re.search(r'\-Y(\d+)', header)

        header_st = int(st_match.group(1)) if st_match else 0
        header_co = int(co_match.group(1)) if co_match else 0
        header_px = int(px_match.group(1)) if px_match else 0
        header_mx = int(mx_match.group(1)) if mx_match else 0
        header_py = int(py_match.group(1)) if py_match else 0
        header_my = int(my_match.group(1)) if my_match else 0

        # Header dimensions (0.1mm units → mm)
        header_width_mm  = (header_px + header_mx) / 10.0
        header_height_mm = (header_py + header_my) / 10.0

        # ── Read with pyembroidery for stitch coordinates ──
        pattern = pyembroidery.read(tmp_path)
        if pattern is None:
            raise HTTPException(400, "Could not parse embroidery file")

        # ── Extract stitches ───────────────────
        stitches_out = []
        color_index = 0

        for stitch in pattern.stitches:
            x, y, cmd = stitch[0], stitch[1], stitch[2]

            if cmd == pyembroidery.END:
                break
            elif cmd == pyembroidery.COLOR_CHANGE or cmd == pyembroidery.NEEDLE_SET:
                color_index += 1
                stitches_out.append({
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "t": "c",
                    "c": color_index
                })
            elif cmd == pyembroidery.JUMP or cmd == pyembroidery.TRIM:
                stitches_out.append({
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "t": "j",
                    "c": color_index
                })
            elif cmd == pyembroidery.STITCH:
                stitches_out.append({
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "t": "s",
                    "c": color_index
                })

        # ── Calculate stats ────────────────────
        normal_stitches = [s for s in stitches_out if s["t"] == "s"]
        jump_stitches   = [s for s in stitches_out if s["t"] == "j"]
        color_changes   = [s for s in stitches_out if s["t"] == "c"]

        # ── STITCH COUNT: Use EmCAD's header value (authoritative) ──
        # Falls back to pyembroidery count if header is missing
        pyembroidery_count = len(normal_stitches) + len(jump_stitches)
        stitch_count = header_st if header_st > 0 else pyembroidery_count

        # ── JUMP COUNT: from pyembroidery (header doesn't store this) ──
        jump_count = len(jump_stitches)

        # ── COLOR COUNT: header CO + 1, fallback to detected ──
        color_count = (header_co + 1) if header_co > 0 else (color_index + 1)

        # ── DIMENSIONS: header values, fallback to computed ──
        if header_width_mm > 0 and header_height_mm > 0:
            width_mm  = round(header_width_mm, 1)
            height_mm = round(header_height_mm, 1)
        elif normal_stitches:
            xs = [s["x"] for s in normal_stitches]
            ys = [s["y"] for s in normal_stitches]
            width_mm  = round((max(xs) - min(xs)) / 10, 1)
            height_mm = round((max(ys) - min(ys)) / 10, 1)
        else:
            width_mm = height_mm = 0

        # ── Per-needle stitch analysis ─────────
        needle_stats = []
        current_needle_stitches = []
        current_color = 0

        for s in stitches_out:
            if s["t"] == "c":
                needle_stats.append(_analyze_needle(
                    current_color, current_needle_stitches
                ))
                current_needle_stitches = []
                current_color = s["c"]
            elif s["t"] == "s":
                current_needle_stitches.append(s)

        # Last needle
        if current_needle_stitches:
            needle_stats.append(_analyze_needle(
                current_color, current_needle_stitches
            ))

        return {
            "success": True,
            "fileName": file.filename,
            "stitchCount": stitch_count,        # EmCAD's exact count from header
            "jumpCount": jump_count,
            "colorCount": color_count,
            "widthMM": width_mm,
            "heightMM": height_mm,
            "areaCM2": round((width_mm * height_mm) / 100, 1),
            "needleStats": needle_stats,
            "stitches": stitches_out,
            # Debug info — remove later
            "debug": {
                "headerST": header_st,
                "pyembroideryCount": pyembroidery_count,
                "headerCO": header_co,
            }
        }

    finally:
        os.unlink(tmp_path)


def _analyze_needle(color_idx: int, stitches: list) -> dict:
    """Calculate exact material consumption per needle"""
    if not stitches:
        return {"needle": color_idx + 1, "stitchCount": 0, "pathLengthMM": 0}

    stitch_count = len(stitches)

    # Calculate total path length using Pythagoras
    total_length_mm = 0.0
    for i in range(1, len(stitches)):
        dx = (stitches[i]["x"] - stitches[i-1]["x"]) / 10
        dy = (stitches[i]["y"] - stitches[i-1]["y"]) / 10
        total_length_mm += math.sqrt(dx*dx + dy*dy)

    return {
        "needle": color_idx + 1,
        "stitchCount": stitch_count,
        "pathLengthMM": round(total_length_mm, 1),
    }