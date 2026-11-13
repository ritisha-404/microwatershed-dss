"""Unit tests for the NumPy D8 hydrology engine.

Correctness is checked two ways:
1. Against hand-derivable answers on structured synthetic DEMs (planes,
   valleys) where the exact pointer/accumulation/catchment is known.
2. Against a deliberately naive brute-force implementation (cell-by-cell
   loops, path walking) on a random smooth DEM. The naive code is slow but
   transparently correct, giving an independent check of the vectorised
   engine.
"""

import math

import numpy as np
import pytest

from conftest import DIAMOND_CATCHMENT_CELLS, DIAMOND_OUTLET, diamond_valley
from microwatershed.hydrology import flow


# --------------------------------------------------------------------------
# Naive reference implementations (transparent, loop-based).
# --------------------------------------------------------------------------

OFFSETS = {1: (-1, 1), 2: (0, 1), 4: (1, 1), 8: (1, 0), 16: (1, -1), 32: (0, -1), 64: (-1, -1), 128: (-1, 0)}


def naive_pointer(z):
    nrows, ncols = z.shape
    out = np.zeros(z.shape, dtype="uint8")
    for r in range(nrows):
        for c in range(ncols):
            if not np.isfinite(z[r, c]):
                continue
            best, best_code = 0.0, 0
            for code, (dr, dc) in OFFSETS.items():
                nr, nc = r + dr, c + dc
                if 0 <= nr < nrows and 0 <= nc < ncols and np.isfinite(z[nr, nc]):
                    dist = math.sqrt(2) if dr != 0 and dc != 0 else 1.0
                    grad = (z[r, c] - z[nr, nc]) / dist
                    if grad > best:
                        best, best_code = grad, code
            out[r, c] = best_code
    return out


def naive_path(pointer, valid, r, c):
    """Cells visited flowing downstream from (r, c), inclusive."""
    nrows, ncols = pointer.shape
    path = []
    while True:
        path.append((r, c))
        code = pointer[r, c]
        if code == 0:
            return path
        dr, dc = OFFSETS[int(code)]
        r, c = r + dr, c + dc
        if not (0 <= r < nrows and 0 <= c < ncols) or not valid[r, c]:
            return path


def naive_accumulation(pointer, valid):
    accum = np.zeros(pointer.shape)
    for r, c in zip(*np.nonzero(valid)):
        for pr, pc in naive_path(pointer, valid, r, c):
            accum[pr, pc] += 1
    return accum


def naive_watershed(pointer, valid, outlet):
    mask = np.zeros(pointer.shape, dtype=bool)
    for r, c in zip(*np.nonzero(valid)):
        if outlet in naive_path(pointer, valid, r, c):
            mask[r, c] = True
    return mask


# --------------------------------------------------------------------------
# Synthetic DEMs.
# --------------------------------------------------------------------------


def east_draining_plane(nrows=20, ncols=30, res=30.0, drop=1.0):
    """z decreases eastward: every cell flows due east."""
    return np.tile(np.arange(ncols, 0.0, -1.0) * drop, (nrows, 1))


def smooth_random_dem(nrows=40, ncols=40, seed=42):
    rng = np.random.default_rng(seed)
    z = rng.normal(0, 1, (nrows, ncols))
    # Smooth with a few box-blur passes, then add a regional slope.
    for _ in range(3):
        z = (np.roll(z, 1, 0) + np.roll(z, -1, 0) + np.roll(z, 1, 1) + np.roll(z, -1, 1) + z) / 5.0
    z = z * 20.0 + np.linspace(50, 0, ncols)[None, :] + 500.0
    return z


# --------------------------------------------------------------------------
# Pointer and accumulation on structured DEMs.
# --------------------------------------------------------------------------


class TestPointer:
    def test_plane_flows_east(self):
        z = east_draining_plane()
        pointer = flow.d8_pointer(z)
        assert (pointer[:, :-1] == 2).all()
        assert (pointer[:, -1] == 0).all()  # grid outlet: no in-grid lower cell

    def test_nodata_cells_have_no_pointer(self):
        z = east_draining_plane()
        z[5:8, 5:8] = np.nan
        pointer = flow.d8_pointer(z)
        assert (pointer[5:8, 5:8] == 0).all()

    def test_pointer_never_points_uphill(self):
        z = smooth_random_dem()
        filled = flow.fill_depressions(z)
        pointer = flow.d8_pointer(filled)
        valid = np.isfinite(filled)
        ds = flow.downstream_index(pointer, valid)
        src = np.flatnonzero(ds >= 0)
        assert (filled.ravel()[ds[src]] < filled.ravel()[src]).all()


class TestAccumulation:
    def test_plane_accumulation_counts_row(self):
        z = east_draining_plane()
        pointer = flow.d8_pointer(z)
        valid = np.isfinite(z)
        ds = flow.downstream_index(pointer, valid)
        accum = flow.flow_accumulation(ds, valid)
        expected = np.tile(np.arange(1.0, z.shape[1] + 1.0), (z.shape[0], 1))
        np.testing.assert_array_equal(accum, expected)

    def test_mass_balance(self):
        z = flow.fill_depressions(smooth_random_dem())
        valid = np.isfinite(z)
        pointer = flow.d8_pointer(z)
        ds = flow.downstream_index(pointer, valid)
        accum = flow.flow_accumulation(ds, valid)
        terminals = flow.terminal_mask(ds, valid)
        assert accum[terminals].sum() == valid.sum()

    def test_cycle_detection(self):
        valid = np.ones((1, 2), dtype=bool)
        ds = np.array([1, 0], dtype="int64")  # two cells pointing at each other
        with pytest.raises(flow.FlowError, match="cycle"):
            flow.flow_accumulation(ds, valid)


# --------------------------------------------------------------------------
# Depression filling.
# --------------------------------------------------------------------------


def dig_pit(z):
    """Dig a bowl-shaped closed depression, deepest at (11, 11)."""
    z = z.copy()
    z[10:13, 10:13] -= np.array([[3.0, 3.0, 3.0], [3.0, 10.0, 3.0], [3.0, 3.0, 3.0]])
    return z


class TestFill:
    def test_pit_is_removed_and_drains(self):
        z = dig_pit(east_draining_plane())
        assert flow.interior_pit_mask(z).any()
        filled = flow.fill_depressions(z)
        assert not flow.interior_pit_mask(filled).any()
        assert (filled >= z - 1e-12).all()
        pointer = flow.d8_pointer(filled)
        assert not flow.unresolved_interior_mask(pointer, np.isfinite(filled)).any()

    def test_cells_outside_depression_unchanged(self):
        z = dig_pit(east_draining_plane())
        filled = flow.fill_depressions(z)
        untouched = np.ones(z.shape, dtype=bool)
        untouched[9:14, 9:14] = False
        np.testing.assert_array_equal(filled[untouched], z[untouched])

    def test_flat_dem_drains_after_fill(self):
        z = np.full((30, 30), 500.0)
        filled = flow.fill_depressions(z)
        pointer = flow.d8_pointer(filled)
        valid = np.isfinite(filled)
        assert not flow.unresolved_interior_mask(pointer, valid).any()
        ds = flow.downstream_index(pointer, valid)
        accum = flow.flow_accumulation(ds, valid)
        assert accum[flow.terminal_mask(ds, valid)].sum() == valid.sum()

    def test_nodata_untouched(self):
        z = east_draining_plane()
        z[0:5, 0:5] = np.nan
        filled = flow.fill_depressions(z)
        assert np.isnan(filled[0:5, 0:5]).all()


# --------------------------------------------------------------------------
# Cross-validation against the naive implementation.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def conditioned():
    z = smooth_random_dem()
    z[30:34, 5:9] = np.nan  # a nodata hole to exercise boundary handling
    filled = flow.fill_depressions(z)
    valid = np.isfinite(filled)
    return filled, valid


class TestAgainstNaive:
    def test_pointer_matches_naive(self, conditioned):
        filled, _ = conditioned
        np.testing.assert_array_equal(flow.d8_pointer(filled), naive_pointer(filled))

    def test_accumulation_matches_naive(self, conditioned):
        filled, valid = conditioned
        pointer = flow.d8_pointer(filled)
        ds = flow.downstream_index(pointer, valid)
        accum = flow.flow_accumulation(ds, valid)
        np.testing.assert_array_equal(accum, naive_accumulation(pointer, valid))

    def test_watershed_matches_naive(self, conditioned):
        filled, valid = conditioned
        pointer = flow.d8_pointer(filled)
        ds = flow.downstream_index(pointer, valid)
        accum = flow.flow_accumulation(ds, valid)
        # Outlet with substantial accumulation plus two arbitrary cells.
        r, c = np.unravel_index(np.argmax(accum), accum.shape)
        for outlet in [(int(r), int(c)), (10, 10), (25, 30)]:
            got = flow.watershed(ds, valid, *outlet)
            np.testing.assert_array_equal(got, naive_watershed(pointer, valid, outlet))


# --------------------------------------------------------------------------
# Known catchment: the diamond valley.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hydro():
    z = diamond_valley()
    assert not flow.interior_pit_mask(z).any()  # designed depression-free
    valid = np.isfinite(z)
    pointer = flow.d8_pointer(z)
    ds = flow.downstream_index(pointer, valid)
    accum = flow.flow_accumulation(ds, valid)
    return z, valid, pointer, ds, accum


class TestDiamondValley:
    def test_exact_catchment(self, hydro):
        _, valid, _, ds, _ = hydro
        catch = flow.watershed(ds, valid, *DIAMOND_OUTLET)
        assert catch.sum() == DIAMOND_CATCHMENT_CELLS
        expected = np.zeros(valid.shape, dtype=bool)
        expected[16:45, 11:70] = True
        expected[DIAMOND_OUTLET] = True
        np.testing.assert_array_equal(catch, expected)

    def test_catchment_is_interior(self, hydro):
        _, valid, _, ds, _ = hydro
        catch = flow.watershed(ds, valid, *DIAMOND_OUTLET)
        touches, sides = flow.touches_boundary(catch, valid)
        assert not touches and sides == []

    def test_outlet_accumulation_equals_catchment(self, hydro):
        _, _, _, _, accum = hydro
        assert accum[DIAMOND_OUTLET] == DIAMOND_CATCHMENT_CELLS

    def test_snap_from_hillslope_to_stream(self, hydro):
        _, valid, _, _, accum = hydro
        # (33, 69): 3 cells south of the valley axis. Within 100 m the stream
        # cells are (30, 69) at 90 m and (30, 70) at 94.9 m; nearest wins.
        r, c, on_stream = flow.snap_outlet(accum, valid, 33, 69, 100.0, 30.0, 100.0)
        assert (r, c, on_stream) == (30, 69, True)

    def test_snap_keeps_point_already_on_stream(self, hydro):
        _, valid, _, _, accum = hydro
        r, c, on_stream = flow.snap_outlet(accum, valid, *DIAMOND_OUTLET, 150.0, 30.0, 100.0)
        assert (r, c, on_stream) == (*DIAMOND_OUTLET, True)

    def test_snap_reports_off_network(self, hydro):
        _, valid, _, _, accum = hydro
        # (20, 40) is mid-hillslope; no cell within 60 m has 100+ upstream cells.
        r, c, on_stream = flow.snap_outlet(accum, valid, 20, 40, 60.0, 30.0, 100.0)
        assert not on_stream
        assert abs(r - 20) <= 2 and abs(c - 40) <= 2


class TestBoundaryDetection:
    def test_plane_catchment_touches_west_edge(self):
        z = east_draining_plane()
        valid = np.isfinite(z)
        ds = flow.downstream_index(flow.d8_pointer(z), valid)
        catch = flow.watershed(ds, valid, 10, 15)
        touches, sides = flow.touches_boundary(catch, valid)
        assert touches and "west" in sides

    def test_catchment_next_to_nodata_is_flagged(self):
        z = diamond_valley()
        z[25:28, 40:43] = np.nan  # hole inside the catchment
        valid = np.isfinite(z)
        filled = flow.fill_depressions(z)
        ds = flow.downstream_index(flow.d8_pointer(filled), valid)
        catch = flow.watershed(ds, valid, *DIAMOND_OUTLET)
        touches, sides = flow.touches_boundary(catch, valid)
        assert touches and "nodata" in sides
