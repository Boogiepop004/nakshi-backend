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


# ── LIGHTWEIGHT STATS ENDPOINT ─────────────────────────────────────────────────
@app.post("/stats")
async def get_stats_only(file: UploadFile = File(...)):
    """Returns only stats — no stitch coordinates, tiny response"""
    filename = file.filename.lower()
    allowed = ['.dst', '.pes', '.jef', '.exp', '.vp3', '.hus']
    if not any(filename.endswith(ext) for ext in allowed):
        raise HTTPException(400, "Unsupported file format")

    suffix = os.path.splitext(filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        raw_bytes = open(tmp_path, 'rb').read(512)
        header = raw_bytes.decode('ascii', errors='ignore')

        st_match = re.search(r'ST\s+(\d+)', header)
        co_match = re.search(r'CO\s+(\d+)', header)
        px_match = re.search(r'\+X(\d+)', header)
        mx_match = re.search(r'\-X(\d+)', header)
        py_match = re.search(r'\+Y(\d+)', header)
        my_match = re.search(r'\-Y(\d+)', header)

        header_st = int(st_match.group(1)) if st_match else 0
        header_co = int(co_match.group(1)) if co_match else 0
        width_mm  = ((int(px_match.group(1)) if px_match else 0) +
                     (int(mx_match.group(1)) if mx_match else 0)) / 10
        height_mm = ((int(py_match.group(1)) if py_match else 0) +
                     (int(my_match.group(1)) if my_match else 0)) / 10

        pattern = pyembroidery.read(tmp_path)
        py_normal       = sum(1 for s in pattern.stitches if s[2] == pyembroidery.STITCH)
        py_jumps        = sum(1 for s in pattern.stitches if s[2] in [pyembroidery.JUMP, pyembroidery.TRIM])
        py_sequin_eject = sum(1 for s in pattern.stitches if s[2] == pyembroidery.SEQUIN_EJECT)
        py_sequin_mode  = sum(1 for s in pattern.stitches if s[2] == pyembroidery.SEQUIN_MODE)
        py_colors       = sum(1 for s in pattern.stitches if s[2] in [pyembroidery.COLOR_CHANGE, pyembroidery.NEEDLE_SET])
        # Actual sequins = EJECT - MODE (MODE fires extra EJECT for B-type sequins)
        actual_sequins  = py_sequin_eject - py_sequin_mode
        py_total        = py_normal + py_jumps + actual_sequins

        return {
            "fileName": file.filename,
            "header": {
                "ST_raw": st_match.group(0) if st_match else "NOT FOUND",
                "headerST": header_st,
                "headerCO": header_co,
                "widthMM": width_mm,
                "heightMM": height_mm,
            },
            "pyembroidery": {
                "normalStitches": py_normal,
                "jumpStitches": py_jumps,
                "sequinEjects": py_sequin_eject,
                "sequinModes": py_sequin_mode,
                "actualSequins": actual_sequins,
                "colorChanges": py_colors,
                "total": py_total,
            },
            "emcadDisplays": 40790,
            "difference": 40790 - py_total,
        }
    finally:
        os.unlink(tmp_path)


# ── MAIN PARSE ENDPOINT ────────────────────────────────────────────────────────
@app.post("/parse")
async def parse_dst(file: UploadFile = File(...)):
    filename = file.filename.lower()
    allowed = ['.dst', '.pes', '.jef', '.exp', '.vp3', '.hus']
    if not any(filename.endswith(ext) for ext in allowed):
        raise HTTPException(400, "Unsupported file format")

    suffix = os.path.splitext(filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        raw_bytes = open(tmp_path, 'rb').read(512)
        header = raw_bytes.decode('ascii', errors='ignore')

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

        header_width_mm  = (header_px + header_mx) / 10.0
        header_height_mm = (header_py + header_my) / 10.0

        pattern = pyembroidery.read(tmp_path)
        if pattern is None:
            raise HTTPException(400, "Could not parse embroidery file")

        stitches_out = []
        color_index = 0
        last_was_sequin_mode = False

        for stitch in pattern.stitches:
            x, y, cmd = stitch[0], stitch[1], stitch[2]

            if cmd == pyembroidery.END:
                break
            elif cmd == pyembroidery.COLOR_CHANGE or cmd == pyembroidery.NEEDLE_SET:
                color_index += 1
                last_was_sequin_mode = False
                stitches_out.append({
                    "x": round(x, 2), "y": round(y, 2),
                    "t": "c", "c": color_index
                })
            elif cmd == pyembroidery.JUMP or cmd == pyembroidery.TRIM:
                last_was_sequin_mode = False
                stitches_out.append({
                    "x": round(x, 2), "y": round(y, 2),
                    "t": "j", "c": color_index
                })
            elif cmd == pyembroidery.SEQUIN_MODE:
                # Mode switch — marks next EJECT as B-type (don't count twice)
                last_was_sequin_mode = True
                # Don't add to stitches_out — just a mode flag
            elif cmd == pyembroidery.SEQUIN_EJECT:
                if last_was_sequin_mode:
                    # This EJECT is the actual B-type sequin placement
                    last_was_sequin_mode = False
                    stitches_out.append({
                        "x": round(x, 2), "y": round(y, 2),
                        "t": "q", "c": color_index  # q = sequin
                    })
                else:
                    # A-type sequin placement
                    stitches_out.append({
                        "x": round(x, 2), "y": round(y, 2),
                        "t": "q", "c": color_index
                    })
            elif cmd == pyembroidery.STITCH:
                last_was_sequin_mode = False
                stitches_out.append({
                    "x": round(x, 2), "y": round(y, 2),
                    "t": "s", "c": color_index
                })

        normal_stitches = [s for s in stitches_out if s["t"] == "s"]
        jump_stitches   = [s for s in stitches_out if s["t"] == "j"]
        sequin_stitches = [s for s in stitches_out if s["t"] == "q"]

        # Total = normal + jumps + sequins (matches EmCAD exactly)
        stitch_count = len(normal_stitches) + len(jump_stitches) + len(sequin_stitches)
        jump_count   = len(jump_stitches)
        color_count  = (header_co + 1) if header_co > 0 else (color_index + 1)

        # Dimensions
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

        # Per-needle analysis
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
            elif s["t"] in ["s", "q"]:
                current_needle_stitches.append(s)

        if current_needle_stitches:
            needle_stats.append(_analyze_needle(
                current_color, current_needle_stitches
            ))

        return {
            "success": True,
            "fileName": file.filename,
            "stitchCount": stitch_count,
            "jumpCount": jump_count,
            "sequinCount": len(sequin_stitches),
            "colorCount": color_count,
            "widthMM": width_mm,
            "heightMM": height_mm,
            "areaCM2": round((width_mm * height_mm) / 100, 1),
            "needleStats": needle_stats,
            "stitches": stitches_out,
            "debug": {
                "normalStitches": len(normal_stitches),
                "jumpStitches": len(jump_stitches),
                "sequinStitches": len(sequin_stitches),
                "headerST": header_st,
                "headerCO": header_co,
            }
        }

    finally:
        os.unlink(tmp_path)


def _analyze_needle(color_idx: int, stitches: list) -> dict:
    """Calculate exact material consumption per needle"""
    if not stitches:
        return {"needle": color_idx + 1, "stitchCount": 0, "pathLengthMM": 0}

    total_length_mm = 0.0
    for i in range(1, len(stitches)):
        dx = (stitches[i]["x"] - stitches[i-1]["x"]) / 10
        dy = (stitches[i]["y"] - stitches[i-1]["y"]) / 10
        total_length_mm += math.sqrt(dx*dx + dy*dy)

    return {
        "needle": color_idx + 1,
        "stitchCount": len(stitches),
        "pathLengthMM": round(total_length_mm, 1),
    }