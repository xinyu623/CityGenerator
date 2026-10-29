# From OSM footprints to CFD STL

This note explains the geometry and the assumptions behind the generated
files. It is aimed at users who will import the STL into OpenFOAM, STAR-CCM+,
Fluent, or another outdoor wind solver.

## 1. Source data

The Overpass query requests:

```overpassql
[out:json][timeout:180];
(
  way["building"](south,west,north,east);
  relation["building"](south,west,north,east);
);
out body;
>;
out skel qt;
```

A `way` with a `building` tag is a footprint. A `relation` with a `building` tag
is usually a multipolygon (courtyard, apartment block, campus building). The
parser keeps polygon geometries and skips relation member ways so the same
footprint is not emitted twice. `building=no`, `construction`, and `proposed`
are ignored.

OSM footprints are 2D and often approximate. They do not contain roof shape or
terrain. The generated model is therefore a set of vertical prisms, which is the
usual first-order geometry for urban wind CFD.

## 2. Projection and local frame

Let the selected WGS84 bounding box be
`(south, west, north, east)`. The UTM zone is computed from the bbox centre:

```text
zone = floor((lon_center + 180) / 6) + 1
EPSG = 32600 + zone   (north)
     = 32700 + zone   (south)
```

Each footprint ring is transformed `WGS84 → UTM`, then shifted by the UTM
coordinates of the bbox south-west corner:

```text
x_local = easting  - easting_origin
y_local = northing - northing_origin
z_local = height above the local ground plane
```

The result is a right-handed frame:

- `+X` east,
- `+Y` north,
- `+Z` up,
- 1 unit = 1 metre.

The STL is written in this Z-up frame. The Three.js preview rotates it to Y-up
only for display. `metadata.json` stores the origin's easting/northing and
longitude/latitude, so a point can be mapped back:

```text
easting  = x_local + origin.easting
northing = y_local + origin.northing
lon, lat = UTM_to_WGS84(easting, northing)
```

## 3. Height resolution

For each footprint, height is resolved in this order:

1. **`height` tag.** Examples: `12`, `12 m`, `12.5m`, `40'` (feet). The parser
   extracts the first number and converts feet to metres.
2. **Levels.** `building:levels × level_height`, plus `roof:height` if present.
3. **Default.** `default_height_m` (9 m by default).

`min_height` raises the base of the prism. If a height is outside the accepted
range it is clamped and a `HEIGHT_CLAMPED` warning is emitted. A missing height
produces `MISSING_HEIGHT`; this is the most common warning in OSM data and
should be reviewed before a production CFD run.

The extrusion height is:

```text
h_extrude = max(height - base, 0) + embed_depth   if base <= 0
h_extrude = max(height - base, 0)                 if base > 0
```

The `embed_depth` (0.5 m by default) extends the shell below `z=0` so the
building side walls do not share a coplanar face with the ground patch. This
avoids a common meshing failure at the wall/ground intersection.

## 4. Footprint cleanup

Raw OSM polygons can be invalid (self-intersecting rings, duplicate nodes,
zero-area spikes). The pipeline:

1. applies `make_valid` (fallback `buffer(0)`);
2. extracts Polygon/MultiPolygon parts;
3. orients the exterior ring counter-clockwise and keeps holes;
4. drops holes below 1 m²;
5. optionally simplifies with `preserve_topology=True` (default 0.05 m);
6. drops parts below `min_building_area_m2` (default 4 m²).

A footprint with a courtyard becomes an extruded prism with an inner hole. The
triangulation (mapbox-earcut via trimesh) creates top/bottom caps with holes and
vertical side walls for both the exterior and the hole.

## 5. Closed shell construction

For a polygon with area `A`, perimeter `P`, height `h`, and embed depth `e`, the
closed shell has:

- a bottom cap at `z = -e` (or `base`),
- a top cap at `z = height`,
- side walls along every exterior and interior ring,
- volume approximately `A × (h + e)` for a vertical prism,
- outward normals after `trimesh.repair.fix_winding` / `fix_normals`.

Every shell is checked before it enters the output:

```text
is_watertight
is_winding_consistent
all(isfinite(vertices))
volume > 0
faces > 0
```

A shell that fails is skipped and counted in `INVALID_MESH`. The remaining
shells are concatenated into `buildings.stl`.

### Why the combined STL may not be watertight

`buildings.stl` is a **multi-shell obstacle file**. If two buildings touch or
overlap, STL's shared vertex positions can be merged when the file is read back,
creating edges shared by four triangles. `trimesh.is_watertight` on the
concatenated mesh can therefore be false even though every individual building
is closed. The metadata records:

- `buildings.each_shell_watertight`: all individual shells passed;
- `buildings.non_manifold_edges`: edge count with more than two adjacent faces;
- a `NON_MANIFOLD_CONTACTS` warning when the written file has such edges.

This is a contact/topology warning, not a claim that every building is broken.
`city.stl` is a preview concatenation of buildings and ground and is
intentionally not a single watertight solid.

## 6. Ground

Two ground modes are available:

- **Plane** (default): two triangles at `z=0`, normal `+Z`. It is an open
  surface, intended as a separate ground patch.
- **Slab**: a closed box from `-thickness` to `0` (1 m by default). Useful when
  a solver wants a solid ground volume.

`ground_margin_m` expands the ground patch beyond the selected bbox. If
`include_ground=false`, no ground file is written, but `buildings.stl` is still
generated.

## 7. Binary STL layout

The writer emits a standard binary STL:

```text
80-byte header
uint32   triangle count
repeat triangle count times:
  float32[3] normal
  float32[3] vertex A
  float32[3] vertex B
  float32[3] vertex C
  uint16     attribute byte count (0)
```

STL is a triangle soup: it has no units, no coordinate-system metadata, and no
per-building groups. That is why the ZIP also contains `metadata.json` and
`buildings.geojson`. Always import the STL with the metadata's unit and frame;
do not assume the solver's default unit is metres.

## 8. CFD import checklist

1. **Units.** Confirm the solver reads the STL as metres. A 100 m building
   should measure 100 units, not 100 mm or 100 ft.
2. **Orientation.** `+Z` is up. If the solver uses Y-up, rotate `-90°` about X.
3. **Patches.** Import `buildings.stl` and `ground.stl` separately. Name them
   `buildings` and `ground`; avoid merging them into one patch.
4. **Domain.** Create a fluid domain with an inlet, outlet, sides, and top.
   Recommended initial distances: 5H upstream, 10–15H downstream, 5H sides,
   5H top, where H is the tallest building (tune for your study).
5. **Ground.** The ground patch is at `z=0`; building shells extend 0.5 m below
   it. If your mesher requires a single closed ground, use the slab mode or
   close the domain explicitly.
6. **Contacts.** Check `NON_MANIFOLD_CONTACTS` and `OVERLAP_DETECTED` in the UI
   or `metadata.json`. Adjacent buildings may need a local boolean/surface wrap
   or a small separation before meshing.
7. **Heights.** Check `MISSING_HEIGHT` and `HEIGHT_CLAMPED`. A default height of
   9 m is a placeholder, not a surveyed value.
8. **Wind direction.** The local axes are east/north. Rotate the model or the
   domain to align the inlet with the study's prevailing wind.
9. **Mesh.** This tool stops at surface STL. Volume meshing, boundary layers,
   turbulence models, and solver dictionaries are outside its scope.

## 9. Known limitations

- No terrain, buildings on slopes, bridges, or overhangs.
- No pitched/flat roof distinction; every building is a vertical prism.
- No `building:part` or roof detail.
- No roads, trees, or porous vegetation.
- No boolean union; overlapping footprints remain overlapping.
- OSM height tags are sparse and sometimes wrong.
- Public Overpass endpoints can be slow or rate-limited; the cache helps after
  the first request.
- The combined `city.stl` is for preview/quick import, not a validated
  watertight fluid-domain boundary.

These limitations are deliberate for v1: the goal is a transparent, inspectable
geometry pipeline with provenance and warnings, not a black-box city model.
