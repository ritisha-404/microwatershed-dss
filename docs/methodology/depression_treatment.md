# Depression treatment (hydrological conditioning)

**Output:** conditioned DEM (`dem_conditioned.tif`, metres). **Kind: derived**
— a modified copy of the measured DEM used only for flow modelling. All
terrain metrics are computed from the *original* DEM.

## Why it is needed

Raw DEMs contain closed depressions (pits): cells or groups of cells with no
downslope neighbour. Some are real (quarries, sinkholes, tanks), but at ~30 m
resolution most are artefacts — sensor noise, interpolation error, or roads
and embankments that block the digital surface where a culvert or bridge
carries the real flow. The D8 flow model routes water strictly downhill, so
any depression swallows all flow entering it: flow accumulation downstream of
an untreated pit is dramatically underestimated and watershed delineation
stops at the pit. (This is exactly the failure observed in the earlier manual
WhiteboxTools experiment: an interior-pit warning accompanied by suspiciously
low flow accumulation.)

## Algorithms

Two standard treatments exist, with different side effects:

- **Filling** raises every cell inside a depression to its spill elevation,
  creating a flat (or epsilon-graded) surface over the depression. Simple and
  robust, but it erases real terrain inside the depression and can flood
  large areas behind a single spurious dam cell.
- **Breaching** lowers the cells along a short path through the blockage,
  carving a channel out of the depression. It preserves the surrounding
  terrain and matches reality where the blockage is an embankment over a
  culvert, at the cost of cutting through genuinely closed basins.

### What this application does

With **WhiteboxTools** available (the preferred engine):

1. `BreachDepressionsLeastCost` (Lindsay & Dhun 2015) with a search distance
   of 50 cells and the `--fill` option — depressions are breached where a
   low-cost path exists and filled otherwise.
2. `FillDepressions` with flat fixing (Wang & Liu 2006) on the result, so any
   remaining flat area receives a tiny gradient and drains.

Both strategies (breach-then-fill vs fill-only) are run on the original DEM
and the number of modified cells and the maximum raise/lower are recorded in
the report (`delineation.depression_treatment_comparison`), so the choice is
inspectable per run. Breach-then-fill is selected because it minimises
elevation distortion on embanked, ~30 m terrain.

Without WhiteboxTools, the built-in engine uses **priority-flood filling**
(Barnes, Lehman & Mulla 2014) with an epsilon gradient (1e-5 m) applied from
the boundary inward, which removes all pits and flats in a single pass.

### Validation

The conditioned DEM is checked before use; failures trigger a fallback to the
built-in engine or abort the run:

- no interior cell may remain strictly lower than all 8 neighbours;
- no interior cell may lack a D8 flow direction (unresolved pit or flat);
- flow accumulation must satisfy mass balance (see
  [flow_accumulation.md](flow_accumulation.md)).

## Data source and resolution

Input: the acquired DEM on the ≈30 m UTM analysis grid (see
[dem_source.md](dem_source.md)). Conditioning is computed in float64 so the
epsilon gradients survive.

## Assumptions and limitations

- Conditioning assumes every depression is spurious. Real closed basins
  (endorheic areas, excavations) are forced to drain, which overstates their
  contribution to downstream catchments.
- Breaching carves channels that do not exist in the measured surface;
  filling raises terrain that does exist. The conditioned DEM is a modelling
  surface, not an elevation product — never read elevations from it.
- The 50-cell breach search distance (~1.5 km at 30 m) bounds how long an
  artificial dam can be before the algorithm falls back to filling.
