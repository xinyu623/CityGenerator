const DEFAULT_CENTER = [28.2278, 112.9389];
const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// WGS84 ellipsoid, the datum the backend's bbox_area_km2 uses through pyproj.
const WGS84_A = 6378137.0;
const WGS84_F = 1 / 298.257223563;
const WGS84_E2 = WGS84_F * (2 - WGS84_F);
const WGS84_E = Math.sqrt(WGS84_E2);

function ellipsoidAreaTerm(sinPhi) {
  // Latitude half of the cell area integral, ∫ du / (1 − e²u²)² from 0 to sin φ.
  return (
    sinPhi / (2 * (1 - WGS84_E2 * sinPhi * sinPhi)) +
    Math.atanh(WGS84_E * sinPhi) / (2 * WGS84_E)
  );
}

export function geodesicAreaKm2(south, west, north, east) {
  // The ellipsoid area of the lat/lon cell:
  // A = Δλ · a²(1 − e²) · [F(sin φN) − F(sin φS)].
  // A geodesic quadrilateral bounded by two meridians and two parallels has the
  // same area to first order — the poleward bulge of one parallel cancels the
  // other — so this matches the backend's pyproj area (~1e-8 relative) instead
  // of the planar width × height the readout used to show.
  const deltaLon = ((east - west) * Math.PI) / 180;
  const sinSouth = Math.sin((south * Math.PI) / 180);
  const sinNorth = Math.sin((north * Math.PI) / 180);
  const area =
    deltaLon *
    WGS84_A *
    WGS84_A *
    (1 - WGS84_E2) *
    (ellipsoidAreaTerm(sinNorth) - ellipsoidAreaTerm(sinSouth));
  return Math.abs(area) / 1_000_000;
}

function distanceMeters(lat1, lon1, lat2, lon2) {
  const radius = 6371008.8;
  const toRad = (value) => (value * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(toRad(lat1)) *
      Math.cos(toRad(lat2)) *
      Math.sin(dLon / 2) *
      Math.sin(dLon / 2);
  return 2 * radius * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

export function selectionFromBounds(bounds) {
  const south = bounds.getSouth();
  const west = bounds.getWest();
  const north = bounds.getNorth();
  const east = bounds.getEast();
  const width = distanceMeters(south, west, south, east);
  const height = distanceMeters(south, west, north, west);
  return {
    bbox: { south, west, north, east },
    width,
    height,
    areaKm2: geodesicAreaKm2(south, west, north, east),
    bounds,
  };
}

export function createMapController({ onChange, onError } = {}) {
  if (!window.L) {
    onError?.("地图库 Leaflet 加载失败，请检查网络或 CDN 访问。");
    return null;
  }

  const map = L.map("map", {
    center: DEFAULT_CENTER,
    zoom: 13,
    zoomControl: false,
    preferCanvas: true,
  });

  L.tileLayer(TILE_URL, {
    maxZoom: 19,
    attribution: TILE_ATTRIBUTION,
  }).addTo(map);

  L.control.zoom({ position: "topright" }).addTo(map);
  L.control.scale({ position: "bottomright", metric: true, imperial: false }).addTo(map);

  let selecting = false;
  let firstPoint = null;
  let previewRect = null;
  let selectionRect = null;
  let firstMarker = null;
  let selection = null;

  const container = map.getContainer();

  function clearLayers() {
    if (previewRect) {
      map.removeLayer(previewRect);
      previewRect = null;
    }
    if (firstMarker) {
      map.removeLayer(firstMarker);
      firstMarker = null;
    }
    if (selectionRect) {
      map.removeLayer(selectionRect);
      selectionRect = null;
    }
  }

  function setSelecting(value) {
    selecting = value;
    if (value) {
      map.dragging.disable();
      map.boxZoom.disable();
      map.doubleClickZoom.disable();
      container.classList.add("crosshair");
    } else {
      map.dragging.enable();
      map.boxZoom.enable();
      map.doubleClickZoom.enable();
      container.classList.remove("crosshair");
    }
  }

  function finish(bounds) {
    setSelecting(false);
    if (previewRect) {
      map.removeLayer(previewRect);
      previewRect = null;
    }
    if (firstMarker) {
      map.removeLayer(firstMarker);
      firstMarker = null;
    }
    selectionRect = L.rectangle(bounds, {
      color: "#544d45",
      weight: 1.5,
      opacity: 0.95,
      fillColor: "#9a9286",
      fillOpacity: 0.13,
    }).addTo(map);
    selection = selectionFromBounds(bounds);
    onChange?.(selection);
  }

  function startSelection() {
    clearLayers();
    selection = null;
    firstPoint = null;
    onChange?.(null);
    setSelecting(true);
  }

  function clearSelection() {
    clearLayers();
    selection = null;
    firstPoint = null;
    setSelecting(false);
    onChange?.(null);
  }

  map.on("click", (event) => {
    if (!selecting) return;
    if (!firstPoint) {
      firstPoint = event.latlng;
      firstMarker = L.circleMarker(firstPoint, {
        radius: 5,
        color: "#544d45",
        weight: 2,
        fillColor: "#fbfaf6",
        fillOpacity: 1,
      }).addTo(map);
      return;
    }

    const bounds = L.latLngBounds(firstPoint, event.latlng);
    const candidate = selectionFromBounds(bounds);
    if (candidate.width < 20 || candidate.height < 20) {
      onError?.("选择区域太小，请至少框选 20 m × 20 m 的范围。");
      return;
    }
    finish(bounds);
  });

  map.on("mousemove", (event) => {
    if (!selecting || !firstPoint) return;
    const bounds = L.latLngBounds(firstPoint, event.latlng);
    if (!previewRect) {
      previewRect = L.rectangle(bounds, {
        color: "#544d45",
        weight: 1.5,
        dashArray: "5 5",
        fillColor: "#9a9286",
        fillOpacity: 0.08,
      }).addTo(map);
    } else {
      previewRect.setBounds(bounds);
    }
  });

  return {
    map,
    startSelection,
    clearSelection,
    getSelection: () => selection,
    invalidate: () => map.invalidateSize(),
  };
}
