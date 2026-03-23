from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pyembroidery
import tempfile
import os
import math

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
        # ── Read with pyembroidery ──────────────
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
                    "t": "c",  # color change
                    "c": color_index
                })
            elif cmd == pyembroidery.JUMP or cmd == pyembroidery.TRIM:
                stitches_out.append({
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "t": "j",  # jump
                    "c": color_index
                })
            elif cmd == pyembroidery.STITCH:
                stitches_out.append({
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "t": "s",  # stitch
                    "c": color_index
                })

        # ── Calculate stats ────────────────────
        normal_stitches = [s for s in stitches_out if s["t"] == "s"]
        jump_stitches   = [s for s in stitches_out if s["t"] == "j"]

        stitch_count  = len(stitches_out) - len([s for s in stitches_out if s["t"] == "c"])
        jump_count    = len(jump_stitches)
        color_count   = color_index + 1

        # Bounds from actual stitch coordinates
        if normal_stitches:
            xs = [s["x"] for s in normal_stitches]
            ys = [s["y"] for s in normal_stitches]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            # pyembroidery uses 0.1mm units
            width_mm  = round((max_x - min_x) / 10, 1)
            height_mm = round((max_y - min_y) / 10, 1)
        else:
            width_mm = height_mm = 0

        # ── Per-needle stitch analysis ─────────
        needle_stats = []
        current_needle_stitches = []
        current_color = 0

        for s in stitches_out:
            if s["t"] == "c":
                # Save current needle data
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
            "stitchCount": stitch_count,
            "jumpCount": jump_count,
            "colorCount": color_count,
            "widthMM": width_mm,
            "heightMM": height_mm,
            "areaCM2": round((width_mm * height_mm) / 100, 1),
            "needleStats": needle_stats,
            "stitches": stitches_out,  # full stitch data for rendering
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
        dx = (stitches[i]["x"] - stitches[i-1]["x"]) / 10  # to mm
        dy = (stitches[i]["y"] - stitches[i-1]["y"]) / 10  # to mm
        total_length_mm += math.sqrt(dx*dx + dy*dy)

    return {
        "needle": color_idx + 1,
        "stitchCount": stitch_count,       # for bead/sequin counting
        "pathLengthMM": round(total_length_mm, 1),  # for thread metering
    }