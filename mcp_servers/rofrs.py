"""
EA Risk of Flooding from Rivers and Sea (RoFRS) — point lookup.

Replaces the old hardcoded postcode → flood zone table. That table keyed on
outward code, which cannot represent flood risk: TW1 3DY (Eel Pie Island,
in the Thames) and TW1 3NP 150m away differ sharply, yet shared one entry.

This reads the EA's published polygons instead and answers per point.

Data:   data/RoFRS_London/RoFRS_London.shp  (ESRI Shapefile, EPSG:27700)
Field:  PROB_4BAND — High | Medium | Low | Very Low
Extent: Greater London only. Outside it the answer is "unknown", never "low".

Pure standard library: the shapefile format is simple enough that pulling in
geopandas/GDAL for one point-in-polygon test is not worth the dependency.
"""

import os
import struct
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_PATH = Path(
    os.environ.get(
        "ROFRS_SHAPEFILE",
        Path(__file__).resolve().parent.parent / "data" / "RoFRS_London" / "RoFRS_London",
    )
)

# Bands ordered worst-first, so a point inside overlapping polygons takes the
# highest risk present rather than whichever the file happens to list first.
BAND_SEVERITY = {"High": 4, "Medium": 3, "Low": 2, "Very Low": 1}

# Index cell size in metres. 1km keeps the candidate list per lookup small
# while holding the index itself to a few thousand cells for London.
_CELL = 1000

_polygons: list | None = None   # [(bbox, rings, band)]
_grid: dict | None = None       # (cx, cy) -> [polygon index]
_extent: tuple | None = None    # bbox of the whole dataset


def _dbf_bands(path: Path) -> list[str]:
    """PROB_4BAND for every record, in file order."""
    with open(path.with_suffix(".dbf"), "rb") as f:
        header = f.read(32)
        nrec, hlen, rlen = struct.unpack("<I H H", header[4:12])
        fields = []
        for _ in range((hlen - 33) // 32):
            fd = f.read(32)
            fields.append((fd[:11].split(b"\x00")[0].decode(), fd[16]))
        f.seek(hlen)

        bands = []
        for _ in range(nrec):
            rec = f.read(rlen)
            if not rec or rec[:1] == b"\x1a":
                break
            offset, value = 1, ""
            for name, flen in fields:
                if name == "PROB_4BAND":
                    value = rec[offset:offset + flen].decode("latin-1").strip()
                offset += flen
            bands.append(value)
        return bands


def _load() -> None:
    """Read the shapefile into memory and build the grid index. Idempotent."""
    global _polygons, _grid, _extent
    if _polygons is not None:
        return

    shp = DATA_PATH.with_suffix(".shp")
    if not shp.exists():
        logger.warning(f"RoFRS shapefile not found at {shp} — flood band lookup disabled")
        _polygons, _grid, _extent = [], {}, None
        return

    bands = _dbf_bands(DATA_PATH)
    polygons: list = []

    with open(shp, "rb") as f:
        header = f.read(100)
        _extent = struct.unpack("<4d", header[36:68])

        record = 0
        while True:
            head = f.read(8)
            if len(head) < 8:
                break
            _, content_len = struct.unpack(">ii", head)
            body = f.read(content_len * 2)
            record += 1

            if struct.unpack("<i", body[:4])[0] != 5:   # 5 = polygon
                continue

            bbox = struct.unpack("<4d", body[4:36])
            nparts, npoints = struct.unpack("<ii", body[36:44])
            parts = struct.unpack(f"<{nparts}i", body[44:44 + 4 * nparts])
            pbase = 44 + 4 * nparts
            coords = struct.unpack(
                f"<{npoints * 2}d", body[pbase:pbase + 16 * npoints]
            )
            points = list(zip(coords[0::2], coords[1::2]))
            bounds = list(parts) + [npoints]
            rings = [points[bounds[i]:bounds[i + 1]] for i in range(nparts)]

            polygons.append((bbox, rings, bands[record - 1]))

    grid: dict = {}
    for i, (bbox, _, _) in enumerate(polygons):
        xmin, ymin, xmax, ymax = bbox
        for cx in range(int(xmin // _CELL), int(xmax // _CELL) + 1):
            for cy in range(int(ymin // _CELL), int(ymax // _CELL) + 1):
                grid.setdefault((cx, cy), []).append(i)

    _polygons, _grid = polygons, grid
    logger.info(
        f"RoFRS loaded: {len(polygons)} polygons, {len(grid)} index cells "
        f"from {shp.name}"
    )


def _in_ring(x: float, y: float, ring: list) -> bool:
    """Ray casting test for one ring."""
    inside = False
    count = len(ring)
    for i in range(count):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % count]
        if (y1 > y) != (y2 > y):
            if x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def in_coverage(easting: float, northing: float) -> bool:
    """Whether the point falls inside the dataset's extent at all."""
    _load()
    if not _extent:
        return False
    xmin, ymin, xmax, ymax = _extent
    return xmin <= easting <= xmax and ymin <= northing <= ymax


def band_at(easting: float, northing: float) -> str | None:
    """
    RoFRS probability band for a British National Grid point.

    Returns High / Medium / Low / Very Low, or None when the point sits in no
    polygon. None means negligible mapped risk IF the point is within
    coverage — callers must check in_coverage() first, because outside the
    extent None only means "not surveyed here".
    """
    _load()
    if not _polygons:
        return None

    cell = (int(easting // _CELL), int(northing // _CELL))
    found = None

    for i in _grid.get(cell, ()):
        (xmin, ymin, xmax, ymax), rings, band = _polygons[i]
        if not (xmin <= easting <= xmax and ymin <= northing <= ymax):
            continue
        hit = False
        for ring in rings:
            if _in_ring(easting, northing, ring):
                hit = not hit          # a hole flips the result back out
        if hit and (
            found is None
            or BAND_SEVERITY.get(band, 0) > BAND_SEVERITY.get(found, 0)
        ):
            found = band

    return found
