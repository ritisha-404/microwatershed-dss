"""Pure-NumPy D8 hydrology engine.

Implements the raster hydrology primitives needed for watershed delineation:

- priority-flood depression filling (Barnes et al. 2014) with an epsilon
  gradient so filled areas and flats still drain,
- D8 flow pointer (steepest descent of the 8 neighbours, distance-weighted),
- D8 flow accumulation (upstream cell count, including the cell itself),
- pour-point snapping to the nearest drainage cell,
- upstream watershed traversal from an outlet cell.

The D8 pointer uses the WhiteboxTools encoding so rasters produced by either
engine are interchangeable:

    64  128  1
    32   0   2        (0 = no downslope neighbour: pit, flat or grid outlet)
    16    8  4

This module serves two purposes: it is the fallback engine when WhiteboxTools
is unavailable (e.g. offline Windows installs), and it provides independent
implementations used to validate WhiteboxTools outputs in the test suite.

All functions operate on float64 arrays with NaN marking nodata, and assume
square cells in a projected CRS (guaranteed by the acquisition module).
"""

from __future__ import annotations

import heapq
import math

import numpy as np

# Whitebox-style D8 codes, clockwise from north-east, with row/col offsets
# and the distance factor used for the steepest-descent gradient.
D8_CODES = np.array([1, 2, 4, 8, 16, 32, 64, 128], dtype="uint8")
D8_OFFSETS = [(-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0)]
D8_DISTANCE = np.array([math.sqrt(2), 1, math.sqrt(2), 1, math.sqrt(2), 1, math.sqrt(2), 1])

FILL_EPSILON = 1e-5  # metres; applied by priority-flood so filled areas drain


class FlowError(Exception):
    """Raised when a hydrological primitive produces an inconsistent result."""


def _padded(z: np.ndarray) -> np.ndarray:
    """z with a 1-cell NaN border, so neighbour slicing never wraps."""
    return np.pad(z, 1, mode="constant", constant_values=np.nan)


def _neighbour_views(padded: np.ndarray) -> list[np.ndarray]:
    """The 8 neighbour arrays of every cell, in D8_OFFSETS order."""
    h, w = padded.shape[0] - 2, padded.shape[1] - 2
    return [padded[1 + dr : 1 + dr + h, 1 + dc : 1 + dc + w] for dr, dc in D8_OFFSETS]


def fill_depressions(z: np.ndarray, epsilon: float = FILL_EPSILON) -> np.ndarray:
    """Fill depressions with the priority-flood algorithm (Barnes et al. 2014).

    Cells are flooded inward from the raster boundary (grid edges and cells
    adjacent to nodata) in order of increasing elevation; every processed cell
    is raised to at least its pour elevation plus ``epsilon``, which removes
    both pits and flats, so the result drains everywhere.

    Returns a new float64 array; NaN (nodata) cells are left untouched.
    """
    z = np.asarray(z, dtype="float64")
    valid = np.isfinite(z)
    filled = z.copy()
    if not valid.any():
        return filled

    padded = _padded(z)
    adjacent_to_nodata = np.zeros_like(valid)
    for nb in _neighbour_views(padded):
        adjacent_to_nodata |= ~np.isfinite(nb)
    seeds = valid & adjacent_to_nodata  # grid edges count: the pad is NaN

    closed = ~valid  # nodata never enters the queue
    heap: list[tuple[float, int, int, int]] = []
    counter = 0
    for r, c in zip(*np.nonzero(seeds)):
        heap.append((z[r, c], counter, int(r), int(c)))
        counter += 1
    heapq.heapify(heap)
    closed[seeds] = True

    nrows, ncols = z.shape
    while heap:
        elev, _, r, c = heapq.heappop(heap)
        for dr, dc in D8_OFFSETS:
            nr, nc = r + dr, c + dc
            if nr < 0 or nr >= nrows or nc < 0 or nc >= ncols or closed[nr, nc]:
                continue
            closed[nr, nc] = True
            new_elev = max(z[nr, nc], elev + epsilon)
            filled[nr, nc] = new_elev
            heapq.heappush(heap, (new_elev, counter, nr, nc))
            counter += 1
    return filled


def d8_pointer(z: np.ndarray) -> np.ndarray:
    """D8 flow pointer (Whitebox encoding) from a conditioned DEM.

    Each valid cell points to the neighbour with the steepest downward
    gradient (elevation drop divided by 1 for cardinal, sqrt(2) for diagonal
    steps). Cells with no lower valid neighbour get 0 — on a properly
    conditioned DEM these occur only where flow leaves the valid area.
    """
    z = np.asarray(z, dtype="float64")
    valid = np.isfinite(z)
    padded = _padded(z)

    best_grad = np.full(z.shape, -np.inf)
    best_code = np.zeros(z.shape, dtype="uint8")
    for code, nb, dist in zip(D8_CODES, _neighbour_views(padded), D8_DISTANCE):
        grad = (z - nb) / dist
        grad = np.where(np.isfinite(grad), grad, -np.inf)
        better = grad > best_grad
        best_grad = np.where(better, grad, best_grad)
        best_code = np.where(better, code, best_code)

    pointer = np.where(valid & (best_grad > 0), best_code, 0).astype("uint8")
    return pointer


def downstream_index(pointer: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Flat index of each cell's downstream cell; -1 for terminal cells.

    A cell is terminal when it is nodata, has pointer 0, or its pointer leads
    off-grid or into a nodata cell (flow exits the valid area there).
    """
    nrows, ncols = pointer.shape
    n = nrows * ncols
    ds = np.full(n, -1, dtype="int64")
    rows, cols = np.indices(pointer.shape)
    for code, (dr, dc) in zip(D8_CODES, D8_OFFSETS):
        sel = (pointer == code) & valid
        if not sel.any():
            continue
        tr, tc = rows[sel] + dr, cols[sel] + dc
        inside = (tr >= 0) & (tr < nrows) & (tc >= 0) & (tc < ncols)
        target = np.full(tr.shape, -1, dtype="int64")
        tin = tr[inside] * ncols + tc[inside]
        tin_valid = valid.ravel()[tin]
        target[inside] = np.where(tin_valid, tin, -1)
        ds[(rows[sel] * ncols + cols[sel])] = target
    return ds


def flow_accumulation(ds: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """D8 flow accumulation as upstream cell count, including the cell itself.

    Processes cells in topological order (Kahn's algorithm in vectorised
    waves). Raises FlowError if the pointer graph contains a cycle, which
    indicates a corrupt flow-direction raster.
    """
    valid_flat = valid.ravel()
    n = valid_flat.size
    accum = np.where(valid_flat, 1.0, 0.0)

    indegree = np.bincount(ds[ds >= 0], minlength=n)
    queue = np.flatnonzero(valid_flat & (indegree == 0))
    processed = 0
    while queue.size:
        processed += queue.size
        targets = ds[queue]
        has_target = targets >= 0
        q, t = queue[has_target], targets[has_target]
        np.add.at(accum, t, accum[q])
        np.add.at(indegree, t, -1)
        candidates = np.unique(t)
        queue = candidates[indegree[candidates] == 0]

    if processed != int(valid_flat.sum()):
        raise FlowError(
            f"Flow pointer graph contains a cycle: {int(valid_flat.sum()) - processed} "
            "cells could not be ordered. The flow-direction raster is corrupt."
        )
    return accum.reshape(valid.shape)


def terminal_mask(ds: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Valid cells whose flow leaves the valid area (downstream index -1)."""
    return valid & (ds == -1).reshape(valid.shape)


def interior_pit_mask(z: np.ndarray) -> np.ndarray:
    """Cells strictly lower than all 8 neighbours, with all neighbours valid.

    Cells on the grid edge or adjacent to nodata are never counted — flow can
    legitimately exit there. On a conditioned DEM this mask must be empty.
    """
    z = np.asarray(z, dtype="float64")
    valid = np.isfinite(z)
    padded = _padded(z)
    min_nb = np.full(z.shape, np.inf)
    all_nb_valid = valid.copy()
    for nb in _neighbour_views(padded):
        all_nb_valid &= np.isfinite(nb)
        min_nb = np.fmin(min_nb, np.where(np.isfinite(nb), nb, np.inf))
    return valid & all_nb_valid & (z < min_nb)


def unresolved_interior_mask(pointer: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Interior cells (all 8 neighbours valid) with no flow direction.

    Such cells are unresolved pits or flats: water entering them disappears,
    which corrupts flow accumulation. A conditioned DEM must have none.
    """
    padded = np.pad(valid, 1, mode="constant", constant_values=False)
    h, w = valid.shape
    all_nb_valid = valid.copy()
    for dr, dc in D8_OFFSETS:
        all_nb_valid &= padded[1 + dr : 1 + dr + h, 1 + dc : 1 + dc + w]
    return valid & all_nb_valid & (pointer == 0)


def snap_outlet(
    accum: np.ndarray,
    valid: np.ndarray,
    row: int,
    col: int,
    max_distance_m: float,
    resolution_m: float,
    stream_threshold_cells: float,
) -> tuple[int, int, bool]:
    """Snap a pour point to the nearest drainage cell.

    Searches within ``max_distance_m`` of (row, col) for the nearest cell
    whose flow accumulation is at least ``stream_threshold_cells`` (Jenson
    1991 style snapping: nearest stream cell, not maximum accumulation, so
    the point does not slide downstream). Ties on distance are broken by the
    larger accumulation.

    Returns (row, col, on_stream). If no cell within range reaches the
    threshold, returns the highest-accumulation cell within range with
    on_stream=False, so the caller can warn that the outlet does not lie on
    a recognisable drainage network.
    """
    nrows, ncols = accum.shape
    radius_cells = int(math.floor(max_distance_m / resolution_m)) if resolution_m > 0 else 0
    r0, r1 = max(row - radius_cells, 0), min(row + radius_cells, nrows - 1)
    c0, c1 = max(col - radius_cells, 0), min(col + radius_cells, ncols - 1)

    rr, cc = np.meshgrid(np.arange(r0, r1 + 1), np.arange(c0, c1 + 1), indexing="ij")
    dist_m = np.hypot(rr - row, cc - col) * resolution_m
    window_valid = valid[r0 : r1 + 1, c0 : c1 + 1] & (dist_m <= max_distance_m)
    window_accum = accum[r0 : r1 + 1, c0 : c1 + 1]

    if not window_valid.any():
        raise FlowError("No valid cells within the snapping distance of the outlet.")

    on_stream = window_valid & (window_accum >= stream_threshold_cells)
    if on_stream.any():
        # Nearest stream cell; break distance ties with the larger accumulation.
        dist_key = np.where(on_stream, dist_m, np.inf)
        best_dist = dist_key.min()
        tie = on_stream & (dist_key == best_dist)
        tie_accum = np.where(tie, window_accum, -np.inf)
        idx = np.unravel_index(np.argmax(tie_accum), tie_accum.shape)
        return int(idx[0] + r0), int(idx[1] + c0), True

    fallback_accum = np.where(window_valid, window_accum, -np.inf)
    idx = np.unravel_index(np.argmax(fallback_accum), fallback_accum.shape)
    return int(idx[0] + r0), int(idx[1] + c0), False


def watershed(ds: np.ndarray, valid: np.ndarray, outlet_row: int, outlet_col: int) -> np.ndarray:
    """Boolean mask of all cells draining through the outlet cell (inclusive).

    Traverses the D8 pointer graph upstream from the outlet. Because every
    cell has at most one downstream cell, each cell is the predecessor of
    exactly one target and is visited at most once.
    """
    nrows, ncols = valid.shape
    if not valid[outlet_row, outlet_col]:
        raise FlowError("Watershed outlet lies on a nodata cell.")

    order = np.argsort(ds, kind="stable")
    ds_sorted = ds[order]

    mask = np.zeros(nrows * ncols, dtype=bool)
    outlet = outlet_row * ncols + outlet_col
    mask[outlet] = True
    frontier = np.array([outlet], dtype="int64")
    while frontier.size:
        left = np.searchsorted(ds_sorted, frontier, side="left")
        right = np.searchsorted(ds_sorted, frontier, side="right")
        preds = [order[l:r] for l, r in zip(left, right) if r > l]
        if not preds:
            break
        frontier = np.concatenate(preds)
        mask[frontier] = True
    return mask.reshape(valid.shape)


def touches_boundary(catchment: np.ndarray, valid: np.ndarray) -> tuple[bool, list[str]]:
    """Whether a catchment reaches the data boundary, and on which sides.

    A catchment cell on the grid edge, or 8-adjacent to a nodata cell, means
    the true catchment may extend beyond the available elevation data — the
    delineation cannot be trusted as complete.
    """
    sides = []
    if catchment[0, :].any():
        sides.append("north")
    if catchment[-1, :].any():
        sides.append("south")
    if catchment[:, 0].any():
        sides.append("west")
    if catchment[:, -1].any():
        sides.append("east")

    padded = np.pad(valid, 1, mode="constant", constant_values=True)
    h, w = valid.shape
    next_to_nodata = np.zeros_like(valid)
    for dr, dc in D8_OFFSETS:
        next_to_nodata |= ~padded[1 + dr : 1 + dr + h, 1 + dc : 1 + dc + w]
    if (catchment & next_to_nodata).any():
        sides.append("nodata")
    return bool(sides), sides
