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
# SEQUIN COUNTING — DEFINITIVE RULE (proven from /debug-sequins output)
#
# pyembroidery emits SEQUIN_EJECT records as follows:
#   B-type sequin: the IDENTICAL stitch record is emitted TWICE back-to-back
#                  (same x, same y, consecutive). Pair = 1 physical sequin.
#   A-type sequin: a unique stitch record emitted ONCE. Single = 1 sequin.
#   SEQUIN_MODE:   machine head mode indicator — irrelevant to counting.
#
# Proof from file 30771=BUTO.DST:
#   Paired EJECTs (B-type): 2,955 pairs = 5,910 EJECT records
#   Single EJECTs (A-type): 2,255 singles
#   Total EJECTs:           8,165  ✓  (matches pyembroidery raw count)
#   Total sequins:          5,210  ✓  (matches EmCAD exactly)
#
# Algorithm: walk EJECT list, if next EJECT has identical (x,y) → pair, skip 2.
# ─────────────────────────────────────────────────────────────────────────────

def _parse_sequins(all_stitches):
    """
    Returns (sequin_positions, a_count, b_count) where:
      sequin_positions = list of (x, y, color_index) for every real sequin drop
      a_count          = number of A-type (single EJECT) sequins
      b_count          = number of B-type (paired EJECT) sequins
    """
    # First collect only SEQUIN_EJECT records with their color context
    ejects = []
    color_index = 0
    for s in all_stitches:
        cmd = s[2]
        if cmd == pyembroidery.END:
            break
        if cmd in (pyembroidery.COLOR_CHANGE, pyembroidery.NEEDLE_SET):
            color_index += 1
        elif cmd == pyembroidery.SEQUIN_EJECT:
            ejects.append((round(s[0], 2), round(s[1], 2), color_index))

    # Walk ejects: identical consecutive pair → 1 B-type sequin, else A-type
    sequin_positions = []
    a_count = 0
    b_count = 0
    i = 0
    while i < len(ejects):
        x, y, c = ejects[i]
        # Check if next EJECT is at the identical position (B-type pair)
        if (i + 1 < len(ejects)
                and ejects[i + 1][0] == x
                and ejects[i + 1][1] == y):
            # B-type: pair → 1 sequin, use this position
            sequin_positions.append((x, y, c))
            b_count += 1
            i += 2          # skip both records
        else:
            # A-type: single → 1 sequin
            sequin_positions.append((x, y, c))
            a_count += 1
            i += 1

    return sequin_positions, a_count, b_count


# ── LIGHTWEIGHT STATS ENDPOINT ─────────────────────────────────────────────────
@app.post("/stats")
async def get_stats_only(file: UploadFile = File(...)):
    """Returns only stats — no stitch coordinates, tiny response"""
    filename = file.filename.lower()
    allowed  = ['.dst', '.pes', '.jef', '.exp', '.vp3', '.hus']
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
                              if s[2] in [pyembroidery.COLOR_CHANGE,
                                          pyembroidery.NEEDLE_SET])

        _, a_sequins, b_sequins = _parse_sequins(pattern.stitches)
        actual_sequins = a_sequins + b_sequins
        total          = py_normal + py_jumps + actual_sequins

        return {
            "fileName": file.filename,
            "header": {
                "ST_raw":   st_match.group(0) if st_match else "NOT FOUND",
                "headerST": header_st,
                "headerCO": header_co,
                "widthMM":  width_mm,
                "heightMM": height_mm,
            },
            "pyembroidery": {
                "normalStitches": py_normal,
                "jumpStitches":   py_jumps,
                "sequinEjects":   py_sequin_eject,
                "sequinModes":    py_sequin_mode,
                "aTypeSequins":   a_sequins,
                "bTypeSequins":   b_sequins,
                "actualSequins":  actual_sequins,
                "colorChanges":   py_colors,
                "total":          total,
            },
        }
    finally:
        os.unlink(tmp_path)


# ── MAIN PARSE ENDPOINT ────────────────────────────────────────────────────────
@app.post("/parse")
async def parse_dst(file: UploadFile = File(...)):
    filename = file.filename.lower()
    allowed  = ['.dst', '.pes', '.jef', '.exp', '.vp3', '.hus']
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

        # ── Parse sequins first (deduplicated positions + counts) ──────────
        sequin_positions, a_sequins, b_sequins = _parse_sequins(pattern.stitches)
        # Build a fast lookup set: (x, y) positions that are real sequin drops
        # We'll match them in order during the stitch walk below.
        sequin_iter  = iter(sequin_positions)
        next_sequin  = next(sequin_iter, None)

        # ── Build stitches_out with correct sequin deduplication ───────────
        stitches_out = []
        color_index  = 0

        # We need to walk the raw stitch list and emit "q" only for real drops.
        # Strategy: maintain a parallel pointer into sequin_positions.
        # When we encounter a SEQUIN_EJECT, advance the sequin pointer and
        # emit "q" only when a real sequin matches the current position.
        # B-type pairs are consumed 2 EJECTs at a time by _parse_sequins,
        # so we just emit from the pre-computed deduplicated list in order.

        # Simpler: rebuild stitches_out from the deduplicated sequin list
        # alongside the non-sequin stitches in correct order.

        sequin_idx   = 0   # pointer into sequin_positions list
        eject_count  = 0   # raw EJECT counter
        # We precompute the cumulative real-sequin index per raw EJECT
        # by replaying _parse_sequins logic here during the walk.

        pending_pair = None   # holds (x,y,c) of first EJECT in a B-type pair

        for stitch in pattern.stitches:
            x, y, cmd = stitch[0], stitch[1], stitch[2]

            if cmd == pyembroidery.END:
                break

            elif cmd in (pyembroidery.COLOR_CHANGE, pyembroidery.NEEDLE_SET):
                color_index  += 1
                pending_pair  = None
                stitches_out.append({
                    "x": round(x, 2), "y": round(y, 2),
                    "t": "c", "c": color_index
                })

            elif cmd in (pyembroidery.JUMP, pyembroidery.TRIM):
                pending_pair = None
                stitches_out.append({
                    "x": round(x, 2), "y": round(y, 2),
                    "t": "j", "c": color_index
                })

            elif cmd == pyembroidery.SEQUIN_MODE:
                # Mode indicator — no stitch record emitted
                pass

            elif cmd == pyembroidery.SEQUIN_EJECT:
                rx, ry = round(x, 2), round(y, 2)
                if pending_pair is not None:
                    px, py_ = pending_pair
                    if rx == px and ry == py_:
                        # 2nd of a B-type pair → emit 1 sequin at this position
                        stitches_out.append({
                            "x": rx, "y": ry,
                            "t": "q", "c": color_index
                        })
                        pending_pair = None
                    else:
                        # Previous pending was actually a lone A-type → emit it,
                        # then start a new pending for current
                        stitches_out.append({
                            "x": px, "y": py_,
                            "t": "q", "c": color_index
                        })
                        pending_pair = (rx, ry)
                else:
                    # Could be A-type or first of B-type pair — defer
                    pending_pair = (rx, ry)

            elif cmd == pyembroidery.STITCH:
                # Flush any orphaned pending before next stitch segment
                if pending_pair is not None:
                    stitches_out.append({
                        "x": pending_pair[0], "y": pending_pair[1],
                        "t": "q", "c": color_index
                    })
                    pending_pair = None
                stitches_out.append({
                    "x": round(x, 2), "y": round(y, 2),
                    "t": "s", "c": color_index
                })

        # Flush final pending if file ends mid-sequin-run
        if pending_pair is not None:
            stitches_out.append({
                "x": pending_pair[0], "y": pending_pair[1],
                "t": "q", "c": color_index
            })

        # ── Totals ─────────────────────────────────────────────────────────
        normal_stitches = [s for s in stitches_out if s["t"] == "s"]
        jump_stitches   = [s for s in stitches_out if s["t"] == "j"]
        sequin_stitches = [s for s in stitches_out if s["t"] == "q"]

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
        needle_stats       = []
        current_needle_sts = []
        current_color      = 0

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
                "normalStitches": len(normal_stitches),
                "jumpStitches":   len(jump_stitches),
                "sequinStitches": len(sequin_stitches),
                "aTypeSequins":   a_sequins,
                "bTypeSequins":   b_sequins,
                "headerST":       header_st,
                "headerCO":       header_co,
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
        "needle":       color_idx + 1,
        "stitchCount":  len(stitches),
        "pathLengthMM": round(total_length_mm, 1),
    }