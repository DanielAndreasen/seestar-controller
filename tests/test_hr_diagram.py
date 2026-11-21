"""Tests for seestar_controller.hr_diagram — the pure CMD / H-R core
(hr-diagram ticket 01). Synthetic cross-match tables with an injected
cluster main sequence + field stars, CSV round-trip, the no-gi subprocess
boundary, and a read-only integration with the store's crossmatch table.
No network, no gi in the package (subprocess-enforced)."""

from __future__ import annotations

import csv
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

import seestar_controller.hr_diagram as hr
from seestar_controller.hr_diagram import (
    CSV_HEADER,
    DEFAULT_SIGMA_CLIP,
    MIN_MEMBERSHIP_SAMPLE,
    ExportError,
    HrError,
    HrPoint,
    Membership,
    absolute_g,
    cmd_points,
    export_table_csv,
    hr_points,
    membership_filter,
)

#: Directory of the hr_diagram package (source-scan audit).
HR_PACKAGE_DIR = Path(hr.__file__).resolve().parent

#: The PURE hr_diagram modules — no ``gi``, no subprocess, no store import
#: (subprocess-enforced).
PURE_HR_MODULES = ("__init__", "core", "export", "isochrone", "plot")

#: The STORE-BOUNDARY modules — no ``gi``, no subprocess, but they DO import
#: :mod:`seestar_controller.store` (the same role as gaia.overlay).
STORE_HR_MODULES = ("controller",)

#: The WIDGET modules — ``gi`` at module top, before any ``from gi.repository``
#: import; exported lazily by the package ``__init__`` (PEP 562).
WIDGET_HR_MODULES = ("view",)


# --- synthetic cross-match table: injected cluster main sequence + field stars ----------------

CLUSTER_IDS = tuple(1000 + i for i in range(25))
FIELD_IDS = tuple(2000 + i for i in range(8))
#: Field rows probing the parallax SNR cut: SNR 5.025 (kept), 5.0 (dropped), 2.0 (dropped).
SNR_IDS = (2997, 2998, 2999)
INCOMPLETE_ID = 3000  # matched but bp=None -> no point, still in the CSV


def _split(g: float, color: float) -> tuple[float, float]:
    """Split an apparent G and a BP-RP color into (bp, rp): bp - rp = color."""
    return g + color / 2.0, g - color / 2.0


def _row(
    source_id: int,
    x: float,
    y: float,
    g: float,
    color: float,
    parallax: float,
    parallax_error: float,
    pmra: float,
    pmdec: float,
    matched: bool = True,
    bp: float | None = None,
    rp: float | None = None,
) -> dict:
    if bp is None and rp is None:
        bp, rp = _split(g, color)
    return {
        "frame_id": 1,
        "x": x,
        "y": y,
        "ra_deg": 83.633 + x * 1e-4,
        "dec_deg": 22.014 + y * 1e-4,
        "matched": matched,
        "source_id": source_id if matched else None,
        "g": g if matched else None,
        "bp": bp if matched else None,
        "rp": rp if matched else None,
        "parallax": parallax if matched else None,
        "parallax_error": parallax_error if matched else None,
        "pmra": pmra if matched else None,
        "pmra_error": 0.05 if matched else None,
        "pmdec": pmdec if matched else None,
        "pmdec_error": 0.05 if matched else None,
        "separation_arcsec": 0.3 if matched else None,
    }


def cluster_table() -> list[dict]:
    """The synthetic table: 25 cluster main-sequence stars (the majority, so
    the median lands on the cluster), 8 field stars, 3 SNR-probe field rows,
    1 incomplete matched row, 2 unmatched detections."""
    rows: list[dict] = []
    for i, sid in enumerate(CLUSTER_IDS):
        g = 12.0 + 0.25 * i
        color = 0.5 + 0.1 * i
        rows.append(
            _row(
                sid,
                x=10.0 + i,
                y=200.0 + i,
                g=g,
                color=color,
                parallax=2.0 + 0.004 * i,
                parallax_error=0.02,
                pmra=5.0 + 0.02 * i,
                pmdec=-2.0 - 0.02 * i,
            )
        )
    for i, sid in enumerate(FIELD_IDS):
        rows.append(
            _row(
                sid,
                x=100.0 + i,
                y=300.0 + i,
                g=15.0 + 0.5 * i,
                color=0.3 + 0.4 * i,
                parallax=0.5 + 0.1 * i,
                parallax_error=0.05,
                pmra=-8.0 + 2.0 * i,
                pmdec=8.0 - 2.0 * i,
            )
        )
    for sid, err in ((2997, 0.199), (2998, 0.2), (2999, 0.5)):
        rows.append(
            _row(
                sid,
                x=400.0 + sid,
                y=500.0,
                g=16.0,
                color=1.0,
                parallax=1.0,
                parallax_error=err,
                pmra=0.0,
                pmdec=0.0,
            )
        )
    # matched but incomplete photometry (bp missing) -> no diagram point
    rows.append(
        _row(
            INCOMPLETE_ID,
            x=600.0,
            y=600.0,
            g=17.0,
            color=1.0,
            parallax=2.0,
            parallax_error=0.02,
            pmra=5.0,
            pmdec=-2.0,
            bp=None,
            rp=16.5,
        )
    )
    # two unmatched detections
    rows.append(_row(0, x=700.0, y=700.0, g=0.0, color=0.0, parallax=0.0,
                     parallax_error=0.0, pmra=0.0, pmdec=0.0, matched=False))
    rows.append(_row(0, x=701.0, y=701.0, g=0.0, color=0.0, parallax=0.0,
                     parallax_error=0.0, pmra=0.0, pmdec=0.0, matched=False))
    return rows


@dataclass(frozen=True)
class _AttrRow:
    """An attribute-style row (like store's CrossmatchRecord) for duck-typing."""

    x: float
    y: float
    ra_deg: float
    dec_deg: float
    matched: bool
    source_id: int | None = None
    g: float | None = None
    bp: float | None = None
    rp: float | None = None
    parallax: float | None = None
    parallax_error: float | None = None
    pmra: float | None = None
    pmdec: float | None = None


# --- absolute-G math ---------------------------------------------------------------------------


def test_absolute_g_matches_the_distance_modulus_formula():
    # M = m + 5 + 5*log10(parallax_arcsec); parallax 2 mas = 0.002 arcsec
    expected = 12.0 + 5.0 + 5.0 * math.log10(0.002)
    assert absolute_g(12.0, 2.0) == pytest.approx(expected, abs=1e-12)
    assert absolute_g(12.0, 2.0) == pytest.approx(12.0 + 5.0 - 5.0 * math.log10(500.0), abs=1e-12)
    # 1000 mas = 1 arcsec -> M = m + 5; 100 mas = 0.1 arcsec -> M = m
    assert absolute_g(15.0, 1000.0) == pytest.approx(20.0, abs=1e-12)
    assert absolute_g(15.0, 100.0) == pytest.approx(15.0, abs=1e-12)
    # no positive parallax -> None
    assert absolute_g(15.0, None) is None
    assert absolute_g(15.0, 0.0) is None
    assert absolute_g(15.0, -1.0) is None
    # non-finite parallax is not positive either -> None (no NaN leak)
    assert absolute_g(15.0, float("nan")) is None
    assert absolute_g(15.0, float("inf")) is None
    assert absolute_g(15.0, float("-inf")) is None


def test_hr_points_abs_g_within_tolerance():
    table = cluster_table()
    points = {p.source_id: p for p in hr_points(table)}
    for i, sid in enumerate(CLUSTER_IDS):
        g = 12.0 + 0.25 * i
        parallax = 2.0 + 0.004 * i
        expected = g + 5.0 - 5.0 * math.log10(1000.0 / parallax)
        assert points[sid].abs_g == pytest.approx(expected, abs=1e-12)
        assert points[sid].g == pytest.approx(g)


# --- membership filter -------------------------------------------------------------------------


def test_membership_filter_keeps_the_cluster_and_drops_the_field():
    table = cluster_table()
    membership = membership_filter(table)
    assert isinstance(membership, Membership)
    assert membership.sigma_clip == DEFAULT_SIGMA_CLIP
    assert membership.sample_size == 25 + 8 + 3 + 1  # all matched rows with astrometry
    # the median lands on the cluster (the majority)
    assert membership.parallax_median == pytest.approx(2.0, abs=0.1)
    assert membership.pmra_median == pytest.approx(5.0, abs=0.5)
    assert membership.pmdec_median == pytest.approx(-2.0, abs=0.5)
    # every cluster star is a member; every field star is dropped
    assert set(CLUSTER_IDS) <= membership.member_ids
    assert INCOMPLETE_ID in membership.member_ids
    assert not (set(FIELD_IDS) & membership.member_ids)
    assert not (set(SNR_IDS) & membership.member_ids)
    # the member flag on the points agrees
    points = {p.source_id: p for p in cmd_points(table, membership=membership)}
    assert all(points[sid].member for sid in CLUSTER_IDS)
    assert not any(points[sid].member for sid in FIELD_IDS + SNR_IDS)


def test_membership_filter_custom_sigma_clip_and_small_sample_fallback():
    table = cluster_table()
    # a huge sigma clip keeps everything in the sample
    wide = membership_filter(table, sigma_clip=1000.0)
    assert set(CLUSTER_IDS) | set(FIELD_IDS) | set(SNR_IDS) | {INCOMPLETE_ID} <= wide.member_ids
    # fewer than MIN_MEMBERSHIP_SAMPLE rows -> everything matched is a member
    tiny = [_row(1, 1.0, 1.0, 14.0, 1.0, 2.0, 0.02, 5.0, -2.0),
            _row(2, 2.0, 2.0, 15.0, 1.5, 0.5, 0.05, -8.0, 8.0)]
    fallback = membership_filter(tiny)
    assert fallback.sample_size == 2 < MIN_MEMBERSHIP_SAMPLE
    assert fallback.member_ids == frozenset({1, 2})
    assert fallback.parallax_median is None
    # empty table -> empty membership, not an error
    empty = membership_filter([])
    assert empty.member_ids == frozenset() and empty.sample_size == 0


def test_membership_filter_rejects_bad_arguments():
    with pytest.raises(TypeError):
        membership_filter("not-a-table")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        membership_filter([], sigma_clip="3")  # type: ignore[arg-type]
    with pytest.raises(HrError):
        membership_filter([], sigma_clip=0.0)
    with pytest.raises(HrError):
        membership_filter([], sigma_clip=float("nan"))


# --- cmd_points / hr_points --------------------------------------------------------------------


def test_cmd_points_returns_apparent_cmd_points_with_colors_and_flags():
    table = cluster_table()
    points = cmd_points(table)
    assert all(isinstance(p, HrPoint) for p in points)
    by_id = {p.source_id: p for p in points}
    # matched rows with complete photometry only: 25 cluster + 8 field + 3 SNR probes
    assert set(by_id) == set(CLUSTER_IDS) | set(FIELD_IDS) | set(SNR_IDS)
    assert INCOMPLETE_ID not in by_id  # bp missing -> skipped
    # bp_rp = bp - rp and g round-trip
    i = 3
    g = 12.0 + 0.25 * i
    color = 0.5 + 0.1 * i
    bp, rp = _split(g, color)
    point = by_id[CLUSTER_IDS[i]]
    assert point.g == pytest.approx(g)
    assert point.bp_rp == pytest.approx(bp - rp)
    assert point.bp_rp == pytest.approx(color)
    # abs_g computed from the parallax (positive here)
    assert point.abs_g == pytest.approx(absolute_g(g, 2.0 + 0.004 * i))
    # member flags: cluster in, field out
    assert point.member is True
    assert by_id[FIELD_IDS[0]].member is False
    # pixel position carried through
    assert point.x == pytest.approx(10.0 + i) and point.y == pytest.approx(200.0 + i)


def test_hr_points_applies_the_parallax_snr_cut():
    table = cluster_table()
    points = hr_points(table)  # default parallax_snr=5.0
    by_id = {p.source_id: p for p in points}
    # cluster (SNR ~100) + field (SNR 10..24) + the SNR 5.025 probe are kept
    assert set(CLUSTER_IDS) | set(FIELD_IDS) | {2997} == set(by_id)
    # SNR exactly 5.0 and SNR 2.0 are dropped (strictly greater than the cut)
    assert 2998 not in by_id and 2999 not in by_id
    # every H-R point has a finite abs_g
    assert all(p.abs_g is not None and math.isfinite(p.abs_g) for p in points)
    # a custom cut drops the low-SNR field stars too
    strict = hr_points(table, parallax_snr=30.0)
    assert {p.source_id for p in strict} == set(CLUSTER_IDS)
    # bad arguments
    with pytest.raises(TypeError):
        hr_points(table, parallax_snr="5")  # type: ignore[arg-type]
    with pytest.raises(HrError):
        hr_points(table, parallax_snr=-1.0)
    with pytest.raises(TypeError):
        hr_points("nope")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cmd_points(table, membership="nope")  # type: ignore[arg-type]


def test_points_accept_attribute_rows_and_one_shot_iterables():
    table = cluster_table()
    attr_rows = [
        _AttrRow(
            x=r["x"], y=r["y"], ra_deg=r["ra_deg"], dec_deg=r["dec_deg"],
            matched=r["matched"], source_id=r["source_id"], g=r["g"], bp=r["bp"],
            rp=r["rp"], parallax=r["parallax"], parallax_error=r["parallax_error"],
            pmra=r["pmra"], pmdec=r["pmdec"],
        )
        for r in table
    ]
    from_dicts = cmd_points(table)
    from_attrs = cmd_points(attr_rows)
    assert from_dicts == from_attrs
    # a generator is consumed exactly once (materialized internally)
    gen = cmd_points(r for r in table)
    assert gen == from_dicts
    gen_hr = hr_points(r for r in table)
    assert gen_hr == hr_points(table)
    # empty table -> empty points
    assert cmd_points([]) == () and hr_points([]) == ()


# --- CSV export + round-trip -------------------------------------------------------------------


def test_export_table_csv_round_trips(tmp_path):
    table = cluster_table()
    dest = export_table_csv(table, tmp_path / "crossmatch.csv")
    assert dest == tmp_path / "crossmatch.csv"
    with dest.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert tuple(rows[0]) == CSV_HEADER
    assert len(rows) == 1 + len(table)  # header + every row, matched and unmatched
    by_source = {row[0]: row for row in rows[1:] if row[0]}
    # a cluster row: floats round-trip exactly (repr), computed columns present
    i = 0
    g = 12.0
    color = 0.5
    bp, rp = _split(g, color)
    row = by_source[str(CLUSTER_IDS[i])]
    assert float(row[6]) == g  # g
    assert float(row[7]) == bp  # bp
    assert float(row[8]) == rp  # rp
    assert float(row[9]) == pytest.approx(bp - rp)  # bp_rp
    assert float(row[9]) == float(repr(bp - rp))  # exact repr round-trip
    assert float(row[17]) == pytest.approx(absolute_g(g, 2.0))  # abs_g
    assert row[5] == "1" and row[18] == "1"  # matched, member
    # a field row: not a member
    frow = by_source[str(FIELD_IDS[0])]
    assert frow[5] == "1" and frow[18] == "0"
    # the incomplete row: bp empty, bp_rp empty, still exported
    irow = by_source[str(INCOMPLETE_ID)]
    assert irow[7] == "" and irow[9] == "" and irow[8] != ""
    # an unmatched row: matched=0, all Gaia fields empty, member=0
    unmatched = [row for row in rows[1:] if row[5] == "0"]
    assert len(unmatched) == 2
    assert all(row[0] == "" and row[6] == "" and row[10] == "" and row[18] == "0"
               for row in unmatched)
    # exact float round-trip for every float column of every row
    float_cols = (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17)
    for src, row in zip(table, rows[1:]):
        for col in float_cols:
            raw = row[col]
            if raw == "":
                continue
            assert float(raw) == float(raw)  # parses
            assert repr(float(raw)) == raw  # repr round-trip


def test_export_table_csv_explicit_membership_and_errors(tmp_path):
    table = cluster_table()
    membership = membership_filter(table, sigma_clip=1000.0)  # everyone a member
    dest = export_table_csv(table, tmp_path / "all.csv", membership=membership)
    with dest.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert all(row[18] == "1" for row in rows[1:] if row[0])
    # a row missing a required field -> ExportError, nothing written
    bad = [{"x": 1.0, "y": 2.0, "matched": True}]  # no ra_deg/dec_deg
    with pytest.raises(ExportError):
        export_table_csv(bad, tmp_path / "bad.csv")
    assert not (tmp_path / "bad.csv").exists()
    # bad matched type -> ExportError
    with pytest.raises(ExportError):
        export_table_csv([{**bad[0], "ra_deg": 1.0, "dec_deg": 2.0, "matched": 1}],
                         tmp_path / "bad2.csv")
    # argument type errors
    with pytest.raises(TypeError):
        export_table_csv(table, 123)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        export_table_csv("nope", tmp_path / "x.csv")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        export_table_csv(table, tmp_path / "x.csv", membership=42)  # type: ignore[arg-type]
    # ExportError is an HrError is a ValueError
    assert issubclass(ExportError, HrError) and issubclass(HrError, ValueError)
    # atomic write: no *.tmp left behind
    assert not list(tmp_path.glob("*.tmp"))


# --- package boundary (subprocess + source scan) ------------------------------------------------


def test_hr_diagram_package_imports_no_gi_and_computes_offline():
    code = """
import socket

class _BlockedSocket:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("network access blocked in test")

def _blocked(*args, **kwargs):
    raise RuntimeError("network access blocked in test")

socket.socket = _BlockedSocket
socket.create_connection = _blocked
socket.getaddrinfo = _blocked

import sys
import tempfile
from pathlib import Path

import seestar_controller.hr_diagram as hr
import seestar_controller.hr_diagram.core
import seestar_controller.hr_diagram.export
import seestar_controller.hr_diagram.plot
import seestar_controller.hr_diagram.controller

assert "gi" not in sys.modules, "GTK imported"
assert "seestar_controller.link" not in sys.modules, "link imported"
assert "seestar_controller.ui" not in sys.modules, "ui imported"
assert "seestar_controller.hr_diagram.view" not in sys.modules, "widget module imported eagerly"

def row(sid, g, color, plx, plx_err, pmra, pmdec):
    bp, rp = g + color / 2.0, g - color / 2.0
    return {"x": 1.0, "y": 2.0, "ra_deg": 83.6, "dec_deg": 22.0, "matched": True,
            "source_id": sid, "g": g, "bp": bp, "rp": rp, "parallax": plx,
            "parallax_error": plx_err, "pmra": pmra, "pmdec": pmdec}

table = [row(1000 + i, 12.0 + 0.25 * i, 0.5 + 0.1 * i, 2.0 + 0.004 * i, 0.02,
             5.0 + 0.02 * i, -2.0 - 0.02 * i)
         for i in range(10)]
table += [row(2000 + i, 15.0, 1.0, 0.8 + 0.02 * i, 0.05, -5.0 + 0.1 * i, 5.0 - 0.1 * i)
          for i in range(4)]

m = hr.membership_filter(table)
assert {1000 + i for i in range(10)} <= m.member_ids
assert not ({2000 + i for i in range(4)} & m.member_ids)
cmd = hr.cmd_points(table)
assert len(cmd) == 14 and all(p.bp_rp is not None for p in cmd)
h = hr.hr_points(table)
assert len(h) == 14 and all(p.abs_g is not None for p in h)
with tempfile.TemporaryDirectory() as td:
    dest = hr.export_table_csv(table, Path(td) / "t.csv")
    assert dest.read_bytes().splitlines()[0].decode() == ",".join(hr.CSV_HEADER)
print("clean")
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_every_hr_diagram_module_is_pure_and_the_public_api_is_exported():
    modules = {path.stem for path in HR_PACKAGE_DIR.glob("*.py")}
    assert modules == {"__init__", *PURE_HR_MODULES, *STORE_HR_MODULES, *WIDGET_HR_MODULES}
    for name in PURE_HR_MODULES:
        source = (HR_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"hr_diagram module {name} imports gi"
        )
        assert not re.search(
            r"^\s*(import subprocess|from subprocess)\b", source, re.MULTILINE
        ), f"hr_diagram module {name} shells out"
        # the pure modules import NOTHING from seestar_controller (duck-typed table)
        for match in re.finditer(
            r"^\s*(?:from|import)\s+(seestar_controller[\w.]*)", source, re.MULTILINE
        ):
            assert match.group(1).startswith("seestar_controller.hr_diagram"), (
                f"hr_diagram module {name} imports {match.group(1)}"
            )
    for name in STORE_HR_MODULES:
        source = (HR_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"hr_diagram module {name} imports gi"
        )
        assert not re.search(
            r"^\s*(import subprocess|from subprocess)\b", source, re.MULTILINE
        ), f"hr_diagram module {name} shells out"
        # the store-boundary module may import the store + its siblings only
        for match in re.finditer(
            r"^\s*(?:from|import)\s+(seestar_controller[\w.]*)", source, re.MULTILINE
        ):
            assert match.group(1).startswith(
                ("seestar_controller.hr_diagram", "seestar_controller.store")
            ), f"hr_diagram module {name} imports {match.group(1)}"
    for name in WIDGET_HR_MODULES:
        source = (HR_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        gi_line = re.search(r"^\s*import gi\b", source, re.MULTILINE)
        assert gi_line is not None, f"hr_diagram widget module {name} must import gi at top"
        first_repo = re.search(r"^\s*from gi\.repository", source, re.MULTILINE)
        assert first_repo is not None, f"hr_diagram widget module {name} must use gi.repository"
        assert gi_line.start() < first_repo.start(), (
            f"hr_diagram widget module {name}: 'import gi' must come before 'from gi.repository'"
        )
    for name in hr.__all__:
        if name in hr._WIDGET_EXPORTS:
            continue  # lazy widget export: importing it needs gi (headless here)
        assert hasattr(hr, name), f"hr_diagram.__all__ name {name} missing"
    assert hr._WIDGET_EXPORTS == {"HrDiagramView": "view"}
    assert set(hr.__all__) == {
        "CMD", "CSV_HEADER", "DB_CALL_TIMEOUT_S", "DEFAULT_PARALLAX_SNR",
        "DEFAULT_SIGMA_CLIP", "HR", "KINDS", "MAD_TO_SIGMA",
        "MIN_MEMBERSHIP_SAMPLE", "PARSEC_ISOCHRONE_URL", "DiagramGeometry",
        "DiagramModel", "DiagramWindow", "ExportError", "HrDiagramController",
        "HrDiagramListener", "HrDiagramSnapshot", "HrDiagramView", "HrError",
        "HrPoint", "IsochroneError", "IsochroneFetchError", "IsochronePoint",
        "Membership", "absolute_g", "abs_g_error", "cmd_points",
        "data_to_screen", "export_table_csv", "fallback_isochrone",
        "fetch_isochrone_json", "full_window", "hr_points", "isochrone",
        "load_stack_image", "membership_filter", "nearest_point_by_pixel",
        "pan_window", "point_at", "point_xy", "render_diagram_png",
        "render_stack_png", "screen_to_data", "stack_image_scale",
        "tonemap_gray", "zoom_window_at",
    }
    assert hr.DEFAULT_PARALLAX_SNR == 5.0


# --- read-only integration with the store's crossmatch table -------------------------------------


def _fits_bytes(date_obs: str) -> bytes:
    import io

    import numpy as np
    from astropy.io import fits as pyfits

    hdu = pyfits.PrimaryHDU(data=np.zeros((8, 8), dtype=np.float32))
    hdu.header["DATE-OBS"] = date_obs
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def test_store_crossmatch_table_feeds_the_core_read_only(tmp_path):
    """The merged gaia-crossmatch API (store.crossmatch, schema v6) is the
    data source: save a table, load it back, and run the core on the loaded
    CrossmatchRecord rows — no schema knowledge, no writes from hr_diagram."""
    from seestar_controller import store

    project = store.create_project("M 42", root=tmp_path / "projects")
    try:
        session_id = store.start_session(
            project, datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
        )
        frame = store.store_frame(
            project, session_id, store.KIND_STACK, _fits_bytes("2026-11-20T22:00:00.000")
        )
        table = cluster_table()
        saved = store.save_crossmatch(project, frame.id, table)
        assert len(saved) == len(table)
        loaded = store.load_crossmatch(project, frame.id)
        assert len(loaded) == len(table)
        stacks = store.list_crossmatched_stacks(project)
        assert len(stacks) == 1
        assert stacks[0].frame_id == frame.id
        assert stacks[0].row_count == len(table)
        assert stacks[0].matched_count == len(table) - 2
        # the core consumes the loaded records directly
        membership = membership_filter(loaded)
        assert set(CLUSTER_IDS) <= membership.member_ids
        points = cmd_points(loaded, membership=membership)
        assert {p.source_id for p in points} == set(CLUSTER_IDS) | set(FIELD_IDS) | set(SNR_IDS)
        hpoints = hr_points(loaded, membership=membership)
        assert {p.source_id for p in hpoints} == set(CLUSTER_IDS) | set(FIELD_IDS) | {2997}
        dest = export_table_csv(loaded, tmp_path / "out" / "table.csv", membership=membership)
        with dest.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        assert tuple(rows[0]) == CSV_HEADER and len(rows) == 1 + len(table)
    finally:
        project.close()
