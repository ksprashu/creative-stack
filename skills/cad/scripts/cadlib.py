#!/usr/bin/env python3
# Copyright 2026 Creative Stack Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared mesh, printability, and host helpers for the /cad scripts.

Pure-Python STL parsing and topology/overhang math live here with zero third-party
imports at module load, so `tests/test_pure.py` exercises every printability rule
on a bare stdlib interpreter.
"""

import argparse
import math
import os
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import hostenv  # noqa: E402

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
GRID_SCALE = 10_000  # 1e-4 mm vertex-welding grid


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def parse_bed(spec):
    """Parse '256x256x256' into (256.0, 256.0, 256.0) mm."""
    parts = str(spec).lower().replace(",", "x").split("x")
    if len(parts) != 3:
        die(f"expected --bed WxDxH in mm (e.g. 256x256x256), got: {spec!r}")
    try:
        dims = tuple(float(p) for p in parts)
    except ValueError:
        die(f"non-numeric bed dimensions: {spec!r}")
    if any(d <= 0 for d in dims):
        die(f"bed dimensions must be positive: {spec!r}")
    return dims


def _cross(u, v):
    return (
        u[1] * v[2] - u[2] * v[1],
        u[2] * v[0] - u[0] * v[2],
        u[0] * v[1] - u[1] * v[0],
    )


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _tri_normal_and_area(a, b, c):
    ab = _sub(b, a)
    ac = _sub(c, a)
    cr = _cross(ab, ac)
    mag = math.sqrt(_dot(cr, cr))
    if mag <= 1e-15:
        return (0.0, 0.0, 0.0), 0.0
    return (cr[0] / mag, cr[1] / mag, cr[2] / mag), 0.5 * mag


def write_binary_stl_bytes(triangles, header=b"creative-stack /cad binary stl"):
    """Serialize [(a, b, c), ...] triangles to standard 80+4+50*N binary STL."""
    hdr = header[:80].ljust(80, b"\x00")
    out = bytearray(hdr)
    out += struct.pack("<I", len(triangles))
    for a, b, c in triangles:
        n, _ = _tri_normal_and_area(a, b, c)
        out += struct.pack(
            "<12fH",
            n[0], n[1], n[2],
            a[0], a[1], a[2],
            b[0], b[1], b[2],
            c[0], c[1], c[2],
            0,
        )
    return bytes(out)


def _looks_binary_stl(data):
    if len(data) < 84:
        return False
    count = struct.unpack_from("<I", data, 80)[0]
    if 84 + count * 50 != len(data):
        return False
    if data[:5].lower() == b"solid":
        # Guard against the rare ASCII file whose byte length happens to match 84+50*N
        sample = data[:512].decode("ascii", errors="ignore")
        if "facet" in sample and "vertex" in sample:
            return False
    return True


def parse_stl(data):
    """Parse binary or ASCII STL bytes into [((x,y,z), (x,y,z), (x,y,z)), ...]."""
    if _looks_binary_stl(data):
        count = struct.unpack_from("<I", data, 80)[0]
        tris = []
        offset = 84
        for _ in range(count):
            vals = struct.unpack_from("<12fH", data, offset)
            offset += 50
            a = (vals[3], vals[4], vals[5])
            b = (vals[6], vals[7], vals[8])
            c = (vals[9], vals[10], vals[11])
            tris.append((a, b, c))
        return tris

    text = data.decode("utf-8", errors="replace")
    tris, verts = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("vertex"):
            parts = line.split()
            if len(parts) >= 4:
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
                if len(verts) == 3:
                    tris.append((verts[0], verts[1], verts[2]))
                    verts = []
    return tris


def _quantize(pt):
    return (
        int(round(pt[0] * GRID_SCALE)),
        int(round(pt[1] * GRID_SCALE)),
        int(round(pt[2] * GRID_SCALE)),
    )


def analyze_mesh(triangles, bed_size=(256.0, 256.0, 256.0), max_overhang_deg=45.0,
                 bed_tol=0.05):
    """Compute manifoldness, connected components, mm bounds, volume, bed contact & overhangs."""
    if not triangles:
        return {
            "triangles": 0,
            "watertight": False,
            "boundary_edges": 0,
            "non_manifold_edges": 0,
            "components": 0,
            "inverted_normals": False,
            "bbox_mm": {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0], "size": [0.0, 0.0, 0.0]},
            "fits_bed": False,
            "unit_warning": "mesh has zero triangles",
            "volume_cm3": 0.0,
            "area_mm2": 0.0,
            "bed_contact_mm2": 0.0,
            "overhang": {"max_deg": max_overhang_deg, "area_mm2": 0.0, "fraction": 0.0},
        }

    xs = [p[0] for tri in triangles for p in tri]
    ys = [p[1] for tri in triangles for p in tri]
    zs = [p[2] for tri in triangles for p in tri]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)
    size = (max_x - min_x, max_y - min_y, max_z - min_z)

    undirected_counts = {}
    directed_counts = {}
    edge_to_tris = {}

    parent = list(range(len(triangles)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    signed_vol6 = 0.0
    total_area = 0.0
    bed_area = 0.0
    overhang_area = 0.0
    degenerate = 0

    for idx, (a, b, c) in enumerate(triangles):
        qa, qb, qc = _quantize(a), _quantize(b), _quantize(c)
        if qa == qb or qb == qc or qa == qc:
            degenerate += 1
            continue

        for u, v in ((qa, qb), (qb, qc), (qc, qa)):
            directed_counts[(u, v)] = directed_counts.get((u, v), 0) + 1
            ue = (u, v) if u < v else (v, u)
            undirected_counts[ue] = undirected_counts.get(ue, 0) + 1
            prev = edge_to_tris.get(ue)
            if prev is None:
                edge_to_tris[ue] = idx
            else:
                union(prev, idx)

        signed_vol6 += _dot(a, _cross(b, c))
        n, area = _tri_normal_and_area(a, b, c)
        total_area += area

        if (n[2] < -0.98
                and abs(a[2] - min_z) <= bed_tol
                and abs(b[2] - min_z) <= bed_tol
                and abs(c[2] - min_z) <= bed_tol):
            bed_area += area
        elif n[2] < -1e-6:
            centroid_z = (a[2] + b[2] + c[2]) / 3.0
            if centroid_z > min_z + bed_tol:
                horiz = math.hypot(n[0], n[1])
                overhang_deg = math.degrees(math.atan2(-n[2], horiz))
                if overhang_deg > max_overhang_deg:
                    overhang_area += area

    boundary_edges = sum(1 for c in undirected_counts.values() if c == 1)
    non_manifold_edges = sum(
        1 for ue, c in undirected_counts.items()
        if c > 2 or directed_counts.get(ue, 0) > 1 or directed_counts.get((ue[1], ue[0]), 0) > 1
    )

    active_roots = {
        find(i) for i, (a, b, c) in enumerate(triangles)
        if len({_quantize(a), _quantize(b), _quantize(c)}) == 3
    }
    components = len(active_roots)

    vol_mm3 = signed_vol6 / 6.0
    inverted = vol_mm3 < -1e-6
    max_dim = max(size)
    unit_warn = None
    if max_dim < 1.0:
        unit_warn = (f"largest dimension is {max_dim:.3f} mm (< 1 mm) — "
                     "possible metre/inch scale bug (slicers interpret STL units as mm)")
    elif max_dim > 1000.0:
        unit_warn = f"largest dimension is {max_dim:.1f} mm (> 1000 mm) — check units"

    fits_bed = (size[0] <= bed_size[0] + 1e-6
                and size[1] <= bed_size[1] + 1e-6
                and size[2] <= bed_size[2] + 1e-6)

    return {
        "triangles": len(triangles),
        "degenerate_triangles": degenerate,
        "watertight": (len(active_roots) > 0 and boundary_edges == 0 and non_manifold_edges == 0),
        "boundary_edges": boundary_edges,
        "non_manifold_edges": non_manifold_edges,
        "components": components,
        "inverted_normals": inverted,
        "bbox_mm": {
            "min": [round(min_x, 4), round(min_y, 4), round(min_z, 4)],
            "max": [round(max_x, 4), round(max_y, 4), round(max_z, 4)],
            "size": [round(size[0], 4), round(size[1], 4), round(size[2], 4)],
        },
        "fits_bed": fits_bed,
        "bed_mm": list(bed_size),
        "unit_warning": unit_warn,
        "volume_cm3": round(abs(vol_mm3) / 1000.0, 4),
        "area_mm2": round(total_area, 2),
        "bed_contact_mm2": round(bed_area, 2),
        "overhang": {
            "max_deg": max_overhang_deg,
            "area_mm2": round(overhang_area, 2),
            "fraction": round(overhang_area / total_area, 4) if total_area > 0 else 0.0,
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stl", help="inspect an STL file and print JSON metrics")
    args = ap.parse_args()
    if args.stl:
        import json
        tris = parse_stl(Path(args.stl).read_bytes())
        print(json.dumps(analyze_mesh(tris), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
