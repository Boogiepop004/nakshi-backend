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


# ─────────────────────────────────────────────────────────────────────────────
# SEQUIN COUNTING — WHY THIS WORKS
#
# pyembroidery decodes Dahao sequin commands as follows:
#   A-type head: emits 1 × SEQUIN_EJECT per physical sequin drop
#   B-type head: emits 2 × SEQUIN_EJECT per physical sequin drop
#               (1st = positioning move, 2nd = actual drop)
#   SEQUIN_MODE: toggles the active head between A ↔ B
#
# Proof from screenshots:
#   SEQUIN_EJECT (pyembroidery) = 8,165
#   B-type (EmCAD)              = 2,955
#   8,165 − 2,955               = 5,210  ← EmCAD exact sequin total ✓
#   A-type (EmCAD)              = 2,255
#   B-type (EmCAD)              = 2,955
#   A + B                       = 5,210 ✓
#
# So: A-mode EJECTs = 2,255 (1:1)
#     B-mode EJECTs = 2,955 × 2 = 5,910 (2:1)
#     Total EJECTs  = 2,255 + 5,910 = 8,165 ✓
# ─────────────────────────────────────────────────────────────────────────────

def _count_sequins_accurately(stitches):
    """
    Walk pyembroidery stitch list and return exact A-type and B-type counts
    matching EmCAD's sequin accounting.
    """
    in_b_mode      = False   # machine starts in A-mode
    b_pending      = False   # True when we've seen the 1st (positioning) EJECT in B-mode
    a_count        = 0
    b_count        = 0

    for s in stitches:
        cmd = s[2]

        if cmd == pyembroidery.END:
            break

        elif cmd == pyembroidery.SEQUIN_MODE:
            # Each occurrence toggles A ↔ B head
            in_b_mode  = not in_b_mode
            b_pending  = False   # reset pairing on mode switch

        elif cmd == pyembroidery.SEQUIN_EJECT:
            if in_b_mode:
                if b_pending:
                    # 2nd EJECT in the pair = actual drop
                    b_count  += 1
                    b_pending = False
                else:
                    # 1st EJECT in the pair = positioning move, skip
                    b_pending = True
            else:
                # A-mode: every EJECT is a real sequin
                a_count += 1

        elif cmd in (pyembroidery.COLOR_CHANGE, pyembroidery.NEEDLE_SET,
                     pyembroidery.JUMP, pyembroidery.TRIM, pyembroidery.STITCH):
            # Any non-sequin command resets the B-mode pairing state
            # (but does NOT reset in_b_mode — head stays as-is until SEQUIN_MODE)
            b_pending = False

    return a_count, b_count


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
        header    = raw_bytes.decode('ascii', errors='ignore')

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
        py_jumps        = sum(1 for s in pattern.stitches
                              if s[2] in [pyembroidery.JUMP, pyembroidery.TRIM])
        py_sequin_eject = sum(1 for s in pattern.stitches
                              if s[2] == pyembroidery.SEQUIN_EJECT)
        py_sequin_mode  = sum(1 for s in pattern.stitches
                              if s[2] == pyembroidery.SEQUIN_MODE)
        py_colors       = sum(1 for s in pattern.stitches
                              if s[2] in [pyembroidery.COLOR_CHANGE, pyembroidery.NEEDLE_SET])

        a_sequins, b_sequins = _count_sequins_accurately(pattern.stitches)
        actual_sequins = a_sequins + b_sequins
        py_total       = py_normal + py_jumps + actual_sequins

        return {
            "fileName": file.filename,
            "header": {
                "ST_raw":    st_match.group(0) if st_match else "NOT FOUND",
                "headerST":  header_st,
                "headerCO":  header_co,
                "widthMM":   width_mm,
                "heightMM":  height_mm,
            },
            "pyembroidery": {
                "normalStitches":  py_normal,
                "jumpStitches":    py_jumps,
                "sequinEjects":    py_sequin_eject,
                "sequinModes":     py_sequin_mode,
                "aTypeSequins":    a_sequins,
                "bTypeSequins":    b_sequins,
                "actualSequins":   actual_sequins,
                "colorChanges":    py_colors,
                "total":           py_total,
            },
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
        header    = raw_bytes.decode('ascii', errors='ignore')

        st_match = re.search(r'ST\s+(\d+)', header)
        co_match = re.search(r'CO\s+(\d+)', header)
        px_match = re.search(r'\+X(\d+)', header)
        mx_match = re.search(r'\-X(\d+)', header)
        py_match = re.search(r'\+Y(\d+)', header)
        my_match = re.search(r'\-Y(\d+)', header)

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
        color_index  = 0

        # ── Sequin state machine ───────────────────────────────────────────
        in_b_mode     = False   # toggles on each SEQUIN_MODE
        b_pending     = False   # True after 1st (positioning) EJECT in B-mode
        b_pending_pos = (0, 0)  # position of 1st EJECT (discarded)

        for stitch in pattern.stitches:
            x, y, cmd = stitch[0], stitch[1], stitch[2]

            if cmd == pyembroidery.END:
                break

            elif cmd in (pyembroidery.COLOR_CHANGE, pyembroidery.NEEDLE_SET):
                color_index += 1
                b_pending    = False
                stitches_out.append({
                    "x": round(x, 2), "y": round(y, 2),
                    "t": "c", "c": color_index
                })

            elif cmd in (pyembroidery.JUMP, pyembroidery.TRIM):
                b_pending = False
                stitches_out.append({
                    "x": round(x, 2), "y": round(y, 2),
                    "t": "j", "c": color_index
                })

            elif cmd == pyembroidery.SEQUIN_MODE:
                # Toggle A ↔ B head — do NOT emit a stitch record
                in_b_mode = not in_b_mode
                b_pending  = False

            elif cmd == pyembroidery.SEQUIN_EJECT:
                if in_b_mode:
                    if b_pending:
                        # 2nd EJECT = actual B-type sequin drop — use THIS position
                        stitches_out.append({
                            "x": round(x, 2), "y": round(y, 2),
                            "t": "q", "c": color_index
                        })
                        b_pending = False
                    else:
                        # 1st EJECT = positioning move — remember pos, skip record
                        b_pending_pos = (x, y)
                        b_pending     = True
                else:
                    # A-mode: every EJECT is a real sequin drop
                    b_pending = False
                    stitches_out.append({
                        "x": round(x, 2), "y": round(y, 2),
                        "t": "q", "c": color_index
                    })

            elif cmd == pyembroidery.STITCH:
                b_pending = False
                stitches_out.append({
                    "x": round(x, 2), "y": round(y, 2),
                    "t": "s", "c": color_index
                })

        # ── Totals ─────────────────────────────────────────────────────────
        normal_stitches = [s for s in stitches_out if s["t"] == "s"]
        jump_stitches   = [s for s in stitches_out if s["t"] == "j"]
        sequin_stitches = [s for s in stitches_out if s["t"] == "q"]

        # Matches EmCAD: normal + jumps + correctly-counted sequins
        stitch_count = (len(normal_stitches)
                        + len(jump_stitches)
                        + len(sequin_stitches))
        jump_count   = len(jump_stitches)
        color_count  = (header_co + 1) if header_co > 0 else (color_index + 1)

        # ── Dimensions ─────────────────────────────────────────────────────
        if header_width_mm > 0 and header_height_mm > 0:
            width_mm  = round(header_width_mm, 1)
            height_mm = round(header_height_mm, 1)
        elif normal_stitches:
            xs        = [s["x"] for s in normal_stitches]
            ys        = [s["y"] for s in normal_stitches]
            width_mm  = round((max(xs) - min(xs)) / 10, 1)
            height_mm = round((max(ys) - min(ys)) / 10, 1)
        else:
            width_mm = height_mm = 0

        # ── Per-needle analysis ─────────────────────────────────────────────
        needle_stats          = []
        current_needle_sts    = []
        current_color         = 0

        for s in stitches_out:
            if s["t"] == "c":
                needle_stats.append(
                    _analyze_needle(current_color, current_needle_sts)
                )
                current_needle_sts = []
                current_color      = s["c"]
            elif s["t"] in ("s", "q"):
                current_needle_sts.append(s)

        if current_needle_sts:
            needle_stats.append(
                _analyze_needle(current_color, current_needle_sts)
            )

        return {
            "success":     True,
            "fileName":    file.filename,
            "stitchCount": stitch_count,
            "jumpCount":   jump_count,
            "sequinCount": len(sequin_stitches),
            "colorCount":  color_count,
            "widthMM":     width_mm,
            "heightMM":    height_mm,
            "areaCM2":     round((width_mm * height_mm) / 100, 1),
            "needleStats": needle_stats,
            "stitches":    stitches_out,
            "debug": {
                "normalStitches":  len(normal_stitches),
                "jumpStitches":    len(jump_stitches),
                "sequinStitches":  len(sequin_stitches),
                "headerST":        header_st,
                "headerCO":        header_co,
            }
        }

    finally:
        os.unlink(tmp_path)


def _analyze_needle(color_idx: int, stitches: list) -> dict:
    """Calculate material consumption per needle (normal + sequin stitches)"""
    if not stitches:
        return {"needle": color_idx + 1, "stitchCount": 0, "pathLengthMM": 0}

    total_length_mm = 0.0
    for i in range(1, len(stitches)):
        dx = (stitches[i]["x"] - stitches[i - 1]["x"]) / 10
        dy = (stitches[i]["y"] - stitches[i - 1]["y"]) / 10
        total_length_mm += math.sqrt(dx * dx + dy * dy)

    return {
        "needle":        color_idx + 1,
        "stitchCount":   len(stitches),
        "pathLengthMM":  round(total_length_mm, 1),
    }