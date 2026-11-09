export const COLORS = {
  city: 0xffffff,
  buildings: 0xffffff,
  ground: 0xffffff,
  wall: 0xffffff,
  // The underside is the section cut: it stays flat graphite instead of taking
  // the height ramp, which would paint the whole slab the lightest stop.
  bottom: 0x6e675f,
  plate: 0xf2efe9,
};

export const MATERIALS = {
  city: { color: COLORS.city, vertexColors: true, roughness: 0.9, metalness: 0.0 },
  buildings: { color: COLORS.buildings, vertexColors: true, roughness: 0.88, metalness: 0.0 },
  ground: { color: COLORS.ground, vertexColors: true, roughness: 0.88, metalness: 0.0 },
  wall: { color: COLORS.wall, vertexColors: true, roughness: 0.88, metalness: 0.0 },
  bottom: { color: COLORS.bottom, vertexColors: false, roughness: 0.95, metalness: 0.0 },
};

export const HEIGHT_RAMP = [
  [0.0, "#DCD6CC"],
  [0.1, "#CFC6B9"],
  [0.25, "#ACA196"],
  [0.45, "#7F756C"],
  [0.7, "#4F4842"],
  [1.0, "#241F1B"],
];

function srgbToLinear(c) {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

// The stops are authored as sRGB hex; vertex colours are consumed in linear
// space, so convert once and interpolate between the linear triples.
const RAMP_LINEAR = HEIGHT_RAMP.map(([t, hex]) => [
  t,
  [1, 3, 5].map((i) => srgbToLinear(parseInt(hex.slice(i, i + 2), 16) / 255)),
]);

export function rampColor(t) {
  const clamped = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0));
  for (let i = 1; i < RAMP_LINEAR.length; i += 1) {
    const [t1, c1] = RAMP_LINEAR[i];
    const [t0, c0] = RAMP_LINEAR[i - 1];
    if (clamped <= t1) {
      const k = t1 === t0 ? 0 : (clamped - t0) / (t1 - t0);
      return [0, 1, 2].map((j) => c0[j] + (c1[j] - c0[j]) * k);
    }
  }
  return RAMP_LINEAR[RAMP_LINEAR.length - 1][1];
}

export function rampColors(positions, minY, maxY, gamma = 1) {
  const span = maxY - minY;
  const out = new Float32Array(positions.length);
  for (let i = 0; i < positions.length; i += 3) {
    // A flat selection has no height range to normalise against; every vertex
    // takes the lightest stop so the model reads as light ground, not black.
    const raw = span >= 1 ? (positions[i + 1] - minY) / span : 0;
    // Height is normalised against the tallest tower, so the ordinary 10-60 m
    // stock lands in the first ramp stop and the gradient disappears. A gamma
    // below 1 lifts the low end while leaving both endpoints untouched.
    const t = gamma === 1 ? raw : raw ** gamma;
    const [r, g, b] = rampColor(t);
    out[i] = r;
    out[i + 1] = g;
    out[i + 2] = b;
  }
  return out;
}

export function legendGroups(counts) {
  // Ground and walls share one material, so they share one swatch and one
  // legend line; the counts keep both patch groups legible.
  const surface = [];
  if (!counts || (!counts.ground && !counts.wall && !counts.bottom)) {
    return [{ swatch: "building", label: "城市实体" }];
  }
  if (counts.ground) surface.push(`地面 ${formatInt(counts.ground)}`);
  if (counts.wall) surface.push(`建筑 ${formatInt(counts.wall)}`);
  const groups = [];
  if (surface.length) groups.push({ swatch: "building", label: surface.join(" · ") });
  if (counts.bottom) groups.push({ swatch: "bottom", label: `底面 ${formatInt(counts.bottom)}` });
  return groups;
}

const PATCH_LABELS = { ground: "地面", wall: "建筑", bottom: "底面" };

let runtime = null;
let loadToken = 0;

function formatMeters(value) {
  if (!Number.isFinite(value)) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(2)} km`;
  return `${value.toFixed(value >= 100 ? 0 : 1)} m`;
}

function formatInt(value) {
  return new Intl.NumberFormat("zh-CN").format(Math.round(value));
}

function syncAutoRotateButton() {
  const button = document.getElementById("viewerRotate");
  if (!button || !runtime) return;
  const active = runtime.controls.autoRotate;
  button.classList.toggle("is-active", active);
  button.setAttribute("aria-pressed", String(active));
  button.textContent = active ? "暂停旋转" : "自动旋转";
}

async function createRuntime() {
  const THREE = await import("three");
  const { STLLoader } = await import("three/addons/loaders/STLLoader.js");
  const { OrbitControls } = await import("three/addons/controls/OrbitControls.js");

  let RoundedBoxGeometry = null;
  try {
    ({ RoundedBoxGeometry } = await import("three/addons/geometries/RoundedBoxGeometry.js"));
  } catch {
    // A plain box is a perfectly usable fallback if the addon is unavailable.
  }

  const container = document.getElementById("viewerCanvas");
  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: true,
    logarithmicDepthBuffer: true,
    powerPreference: "high-performance",
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.82;
  renderer.shadowMap.enabled = false;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.replaceChildren(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100000);
  camera.up.set(0, 1, 0);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.065;
  controls.enablePan = true;
  controls.screenSpacePanning = true;
  controls.mouseButtons = {
    LEFT: THREE.MOUSE.ROTATE,
    MIDDLE: THREE.MOUSE.PAN,
    RIGHT: null,
  };
  controls.rotateSpeed = 0.55;
  controls.zoomSpeed = 0.8;

  // Middle-drag pans the model; suppress the browser's autoscroll cursor.
  renderer.domElement.addEventListener("mousedown", (event) => {
    if (event.button === 1) event.preventDefault();
  });
  renderer.domElement.addEventListener("auxclick", (event) => {
    if (event.button === 1) event.preventDefault();
  });
  controls.minPolarAngle = Math.PI * 0.14;
  controls.maxPolarAngle = Math.PI * 0.49;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.55;

  try {
    const { RoomEnvironment } = await import("three/addons/environments/RoomEnvironment.js");
    const envScene = new RoomEnvironment();
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(envScene, 0.04).texture;
    envScene.dispose?.();
    pmrem.dispose();
  } catch {
    // Three.js lights below still give a clean studio look without an HDRI.
  }

  const hemisphere = new THREE.HemisphereLight(0xfffcf6, 0x6e675f, 0.42);
  scene.add(hemisphere);

  const keyLight = new THREE.DirectionalLight(0xfff8ee, 1.45);
  keyLight.castShadow = true;
  keyLight.shadow.mapSize.set(2048, 2048);
  keyLight.shadow.bias = -0.0004;
  keyLight.shadow.normalBias = 0.02;
  keyLight.shadow.radius = 3;
  scene.add(keyLight);
  scene.add(keyLight.target);

  const fillLight = new THREE.DirectionalLight(0xb9c4cc, 0.3);
  fillLight.position.set(-1.6, 1.1, -1.1);
  scene.add(fillLight);

  const rimLight = new THREE.DirectionalLight(0xffffff, 0.25);
  rimLight.position.set(0.2, 1.4, -1.8);
  scene.add(rimLight);

  const modelGroup = new THREE.Group();
  scene.add(modelGroup);

  const state = {
    THREE,
    STLLoader,
    RoundedBoxGeometry,
    renderer,
    scene,
    camera,
    controls,
    keyLight,
    modelGroup,
    loader: new STLLoader(),
    visible: false,
    wireframe: false,
    pedestal: null,
    contactShadow: null,
    homeView: null,
  };

  controls.addEventListener("start", () => {
    if (!state.controls.autoRotate) return;
    state.controls.autoRotate = false;
    syncAutoRotateButton();
  });

  function resize() {
    const width = container.clientWidth || 1;
    const height = container.clientHeight || 1;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }

  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();

  function animate() {
    requestAnimationFrame(animate);
    if (!state.visible) return;
    controls.update();
    renderer.render(scene, camera);
  }
  animate();

  state.resize = resize;
  return state;
}

function disposeObject(state, object) {
  if (!object) return;
  state.scene.remove(object);
  object.geometry?.dispose();
  const materials = Array.isArray(object.material) ? object.material : [object.material];
  for (const material of materials) {
    if (!material) continue;
    material.map?.dispose();
    material.dispose();
  }
}

function disposeModel(state) {
  for (const child of [...state.modelGroup.children]) {
    state.modelGroup.remove(child);
    disposeObject(state, child);
  }
  disposeObject(state, state.pedestal);
  disposeObject(state, state.contactShadow);
  state.pedestal = null;
  state.contactShadow = null;
  state.homeView = null;
}

function buildPedestal(state, box) {
  const { THREE } = state;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.z, 1);
  const margin = Math.max(maxDim * 0.055, 1.6);
  const width = size.x + margin * 2;
  const depth = size.z + margin * 2;
  const height = Math.max(1.2, Math.min(maxDim * 0.025, 36));

  const bevel = Math.min(Math.min(width, depth) * 0.018, height * 0.42);

  const geometry = state.RoundedBoxGeometry
    ? new state.RoundedBoxGeometry(width, height, depth, 4, bevel)
    : new THREE.BoxGeometry(width, height, depth);
  const material = new THREE.MeshStandardMaterial({
    color: COLORS.plate,
    roughness: 0.85,
    metalness: 0.0,
    envMapIntensity: 0.25,
  });
  const pedestal = new THREE.Mesh(geometry, material);
  pedestal.position.set(center.x, -height / 2 - 0.01, center.z);
  pedestal.castShadow = true;
  pedestal.receiveShadow = true;
  pedestal.name = "pedestal";
  state.scene.add(pedestal);
  state.pedestal = pedestal;

  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 256;
  const context = canvas.getContext("2d");
  const gradient = context.createRadialGradient(128, 128, 8, 128, 128, 126);
  gradient.addColorStop(0, "rgba(48, 43, 37, 0.26)");
  gradient.addColorStop(0.55, "rgba(48, 43, 37, 0.10)");
  gradient.addColorStop(1, "rgba(48, 43, 37, 0)");
  context.fillStyle = gradient;
  context.fillRect(0, 0, 256, 256);
  const texture = new THREE.CanvasTexture(canvas);
  const shadowGeometry = new THREE.PlaneGeometry(width * 1.5, depth * 1.5);
  const shadowMaterial = new THREE.MeshBasicMaterial({
    map: texture,
    transparent: true,
    depthWrite: false,
    opacity: 0.8,
  });
  const shadow = new THREE.Mesh(shadowGeometry, shadowMaterial);
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.set(center.x, -height - 0.06, center.z);
  shadow.renderOrder = -1;
  state.scene.add(shadow);
  state.contactShadow = shadow;

  return { width, depth, height, margin, center };
}

function setShadowBounds(state, plate) {
  const { keyLight } = state;
  const extent = Math.max(plate.width, plate.depth) * 0.72;
  const camera = keyLight.shadow.camera;
  camera.left = -extent;
  camera.right = extent;
  camera.top = extent;
  camera.bottom = -extent;
  camera.near = 0.1;
  camera.far = extent * 8;
  camera.updateProjectionMatrix();

  // A fixed 2 cm bias is far too small for a city measured in kilometres and
  // produces self-shadow banding across the ground slab. Scale it with the
  // shadow frustum instead.
  keyLight.shadow.normalBias = Math.max(0.05, Math.min(extent * 0.0015, 1.5));

  keyLight.position.set(
    plate.center.x + extent * 0.72,
    plate.height + extent * 1.45,
    plate.center.z + extent * 0.72
  );
  keyLight.target.position.set(plate.center.x, 0, plate.center.z);
  keyLight.target.updateMatrixWorld();
}

function fitCamera(state, box, plate, triangles) {
  const { THREE, camera, controls } = state;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const direction = new THREE.Vector3(1, 0.9, 1).normalize();
  const viewBox = new THREE.Box3(
    new THREE.Vector3(
      center.x - plate.width / 2,
      box.min.y - plate.height,
      center.z - plate.depth / 2
    ),
    new THREE.Vector3(
      center.x + plate.width / 2,
      box.max.y,
      center.z + plate.depth / 2
    )
  );
  const target = viewBox.getCenter(new THREE.Vector3());

  // Fit the projected plate/city, not its bounding sphere. A sphere fit pushes
  // a wide isometric model unnecessarily far away and makes it look like a toy.
  const forward = direction.clone().negate();
  const right = new THREE.Vector3().crossVectors(forward, camera.up).normalize();
  const up = new THREE.Vector3().crossVectors(right, forward).normalize();
  let halfWidth = 0;
  let halfHeight = 0;
  for (const x of [viewBox.min.x, viewBox.max.x]) {
    for (const y of [viewBox.min.y, viewBox.max.y]) {
      for (const z of [viewBox.min.z, viewBox.max.z]) {
        const offset = new THREE.Vector3(x, y, z).sub(target);
        halfWidth = Math.max(halfWidth, Math.abs(offset.dot(right)));
        halfHeight = Math.max(halfHeight, Math.abs(offset.dot(up)));
      }
    }
  }

  const tanV = Math.tan(((camera.fov * Math.PI) / 180) / 2);
  const tanH = tanV * Math.max(camera.aspect, 0.6);
  const distance = Math.max(halfWidth / tanH, halfHeight / tanV) * 1.14;
  const radius = Math.max(viewBox.getSize(new THREE.Vector3()).length() * 0.5, 1);

  camera.near = Math.max(radius / 5000, 0.01);
  camera.far = Math.max(distance * 5, 1000);
  camera.updateProjectionMatrix();

  camera.position.copy(target).addScaledVector(direction, distance);
  controls.target.copy(target);
  controls.minDistance = Math.max(distance * 0.38, 1);
  controls.maxDistance = Math.max(distance * 4.5, 100);
  controls.update();

  state.homeView = {
    position: camera.position.clone(),
    target: target.clone(),
  };

  setShadowBounds(state, plate);

  const meta = document.getElementById("viewerMeta");
  if (meta) {
    meta.textContent = `${formatInt(triangles)} 个三角形 · 范围 ${formatMeters(
      size.x
    )} × ${formatMeters(size.z)} × ${formatMeters(size.y)}`;
  }
  const scale = document.getElementById("viewerScale");
  if (scale) scale.textContent = `${formatMeters(size.x)} × ${formatMeters(size.z)}`;
}

function cleanPreviewGeometry(state, geometry) {
  const THREE = state.THREE;
  const source = geometry.index ? geometry.toNonIndexed() : geometry;
  const position = source.getAttribute("position");
  const normal = source.getAttribute("normal");
  if (!position || position.count < 3) return source;

  const positions = [];
  const normals = [];
  const a = new THREE.Vector3();
  const b = new THREE.Vector3();
  const c = new THREE.Vector3();
  const ab = new THREE.Vector3();
  const ac = new THREE.Vector3();
  const cross = new THREE.Vector3();
  const minAltitude = 0.15;

  for (let i = 0; i < position.count; i += 3) {
    a.fromBufferAttribute(position, i);
    b.fromBufferAttribute(position, i + 1);
    c.fromBufferAttribute(position, i + 2);
    ab.subVectors(b, a);
    ac.subVectors(c, a);
    cross.crossVectors(ab, ac);
    const area = cross.length() * 0.5;
    const longest = Math.max(ab.length(), ac.length(), b.distanceTo(c));
    const altitude = longest > 0 ? (2 * area) / longest : 0;
    if (altitude < minAltitude) continue;

    for (let j = 0; j < 3; j += 1) {
      const vertex = i + j;
      positions.push(position.getX(vertex), position.getY(vertex), position.getZ(vertex));
      if (normal) normals.push(normal.getX(vertex), normal.getY(vertex), normal.getZ(vertex));
    }
  }

  if (!positions.length) return source;
  const cleaned = new THREE.BufferGeometry();
  cleaned.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  if (normals.length === positions.length) {
    cleaned.setAttribute("normal", new THREE.Float32BufferAttribute(normals, 3));
  } else {
    cleaned.computeVertexNormals();
  }
  cleaned.computeBoundingBox();
  cleaned.computeBoundingSphere();
  source.dispose();
  geometry.dispose();
  return cleaned;
}

function applyRamp(state, geometry, minY, maxY) {
  const position = geometry.getAttribute("position");
  if (!position) return geometry;
  const colors = rampColors(position.array, minY, maxY, 0.65);
  geometry.setAttribute("color", new state.THREE.Float32BufferAttribute(colors, 3));
  return geometry;
}

function applyWireframe(state) {
  state.modelGroup.traverse((child) => {
    if (!child.isMesh) return;
    const materials = Array.isArray(child.material) ? child.material : [child.material];
    for (const material of materials) {
      material.wireframe = state.wireframe;
    }
  });
}

function splitByPatches(state, geometry, patches) {
  const ranges = patches?.ranges;
  if (!Array.isArray(ranges) || !ranges.length) return null;
  const { THREE } = state;
  const source = geometry.index ? geometry.toNonIndexed() : geometry;
  const position = source.getAttribute("position");
  const normal = source.getAttribute("normal");
  if (!position) {
    if (source !== geometry) source.dispose();
    return null;
  }
  const triangles = Math.floor(position.count / 3);
  const covered = ranges.reduce((sum, range) => sum + (range.count || 0), 0);
  if (covered !== triangles) {
    console.warn(
      `patches.json 覆盖 ${covered} 个三角形，模型有 ${triangles} 个，使用单色预览`
    );
    if (source !== geometry) source.dispose();
    return null;
  }

  const positionArray = position.array;
  const normalArray = normal ? normal.array : null;
  const spans = new Map();
  for (const range of ranges) {
    const kind = PATCH_LABELS[range.kind] ? range.kind : "wall";
    if (!spans.has(kind)) spans.set(kind, []);
    spans.get(kind).push([range.start, range.count]);
  }

  const parts = [];
  for (const [kind, kindSpans] of spans) {
    const vertexCount = kindSpans.reduce((sum, [, count]) => sum + count, 0) * 3;
    const positions = new Float32Array(vertexCount * 3);
    const normals = normalArray ? new Float32Array(vertexCount * 3) : null;
    let offset = 0;
    for (const [start, count] of kindSpans) {
      const from = start * 9;
      const to = (start + count) * 9;
      positions.set(positionArray.subarray(from, to), offset);
      if (normals) normals.set(normalArray.subarray(from, to), offset);
      offset += to - from;
    }
    const part = new THREE.BufferGeometry();
    part.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    if (normals) part.setAttribute("normal", new THREE.Float32BufferAttribute(normals, 3));
    parts.push({ geometry: part, kind });
  }
  if (source !== geometry) source.dispose();
  geometry.dispose();
  return parts;
}

function updateLegend(patches) {
  const container = document.getElementById("viewerKinds");
  if (!container) return;
  container.replaceChildren();
  for (const group of legendGroups(patches?.counts)) {
    const item = document.createElement("span");
    const swatch = document.createElement("i");
    swatch.className = `legend-swatch ${group.swatch}`;
    item.append(swatch, document.createTextNode(group.label));
    container.append(item);
  }
}

async function loadModels(urls, state, patches = null) {
  const token = ++loadToken;
  const status = document.getElementById("viewerStatus");
  const meta = document.getElementById("viewerMeta");
  disposeModel(state);
  state.resize();
  status.hidden = false;
  status.textContent = "加载 STL…";
  meta.textContent = "";

  let triangles = 0;
  let split = false;
  try {
    const loaded = [];
    const combined = new state.THREE.Box3();
    for (const item of urls) {
      const geometry = await state.loader.loadAsync(item.url);
      if (token !== loadToken) {
        loaded.forEach(({ geometry: previous }) => previous.dispose());
        geometry.dispose();
        return { split: false };
      }
      geometry.rotateX(-Math.PI / 2);
      const pieces =
        patches && item.kind === "city" ? splitByPatches(state, geometry, patches) : null;
      if (pieces) {
        split = true;
        for (const piece of pieces) {
          piece.geometry.computeBoundingBox();
          if (!piece.geometry.boundingBox) throw new Error("STL 缺少包围盒");
          combined.union(piece.geometry.boundingBox);
          loaded.push(piece);
          triangles += piece.geometry.attributes.position.count / 3;
        }
        continue;
      }
      geometry.computeBoundingBox();
      if (!geometry.boundingBox) throw new Error("STL 缺少包围盒");
      combined.union(geometry.boundingBox);
      loaded.push({ geometry, kind: item.kind });
      triangles += geometry.attributes.position.count / 3;
    }

    if (token !== loadToken) {
      loaded.forEach(({ geometry }) => geometry.dispose());
      return { split: false };
    }
    if (combined.isEmpty()) throw new Error("模型为空");

    // All output files share the same local frame. Translate them together,
    // otherwise separately loaded buildings and ground would lose alignment.
    const center = combined.getCenter(new state.THREE.Vector3());
    const lift = -combined.min.y;
    for (const { geometry, kind } of loaded) {
      geometry.translate(-center.x, lift, -center.z);
      geometry.computeBoundingSphere();

      // One material per patch group when the ranges were applied; a job
      // without patches renders the whole city in the city graphite material.
      const previewGeometry = cleanPreviewGeometry(state, geometry);
      const config = MATERIALS[kind] ?? MATERIALS.city;
      const material = new state.THREE.MeshStandardMaterial({
        ...config,
        flatShading: false,
        side: state.THREE.DoubleSide,
        envMapIntensity: 0.3,
        dithering: false,
      });
      const mesh = new state.THREE.Mesh(previewGeometry, material);
      mesh.name = kind;
      mesh.castShadow = false;
      mesh.receiveShadow = false;
      state.modelGroup.add(mesh);
    }

    const box = new state.THREE.Box3().setFromObject(state.modelGroup);
    if (box.isEmpty()) throw new Error("模型为空");

    // One height scale for every patch mesh: the ramp is keyed to the whole
    // model's vertical extent, so the ground slab and the walls share it. The
    // section-cut underside keeps its flat graphite colour.
    for (const child of state.modelGroup.children) {
      if (!child.isMesh || child.material.vertexColors !== true) continue;
      applyRamp(state, child.geometry, box.min.y, box.max.y);
    }

    status.textContent = "布置展台…";
    const plate = buildPedestal(state, box);
    fitCamera(state, box, plate, triangles);
    applyWireframe(state);
    state.controls.autoRotate = true;
    syncAutoRotateButton();
    status.hidden = true;
    return { split };
  } catch (error) {
    if (token !== loadToken) return { split: false };
    console.error(error);
    status.hidden = false;
    status.textContent = `加载失败：${error?.message || "未知错误"}`;
    return { split: false };
  }
}

export async function openViewer({ urls, patches = null, title = "3D 预览", meta = "" }) {
  const modal = document.getElementById("viewerModal");
  const status = document.getElementById("viewerStatus");
  document.getElementById("viewerTitle").textContent = title;
  document.getElementById("viewerMeta").textContent = meta;
  updateLegend(null);
  modal.hidden = false;
  status.hidden = false;
  status.textContent = "初始化查看器…";

  try {
    if (!runtime) {
      runtime = await createRuntime();
    }
    runtime.visible = true;
    runtime.resize();
    const result = await loadModels(urls, runtime, patches);
    updateLegend(result?.split ? patches : null);
  } catch (error) {
    console.error(error);
    status.hidden = false;
    status.textContent = `Three.js 加载失败：${error?.message || "请检查 CDN 访问"}`;
  }
}

export function closeViewer() {
  if (runtime) runtime.visible = false;
  document.getElementById("viewerModal").hidden = true;
  loadToken += 1;
}

export function resetView() {
  if (!runtime?.homeView) return;
  runtime.camera.position.copy(runtime.homeView.position);
  runtime.controls.target.copy(runtime.homeView.target);
  runtime.controls.update();
}

export function toggleAutoRotate() {
  if (!runtime) return;
  runtime.controls.autoRotate = !runtime.controls.autoRotate;
  syncAutoRotateButton();
}

export function setWireframe(enabled) {
  if (!runtime) return;
  runtime.wireframe = Boolean(enabled);
  applyWireframe(runtime);
}
