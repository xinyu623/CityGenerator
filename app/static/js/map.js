const DEFAULT_CENTER = [28.2278, 112.9389];
const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

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

function selectionFromBounds(bounds) {
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
    areaKm2: (width * height) / 1_000_000,
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
      color: "#0f766e",
      weight: 2,
      opacity: 0.95,
      fillColor: "#14b8a6",
      fillOpacity: 0.1,
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
        color: "#0f766e",
        weight: 2,
        fillColor: "#ffffff",
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
        color: "#0f766e",
        weight: 1.5,
        dashArray: "5 5",
        fillColor: "#14b8a6",
        fillOpacity: 0.06,
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
