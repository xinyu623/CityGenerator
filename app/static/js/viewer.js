const COLORS = {
  city: 0xaebdca,
  buildings: 0xb9c6d1,
  ground: 0x2f4859,
};

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

async function createRuntime() {
  const THREE = await import("three");
  const { STLLoader } = await import("three/addons/loaders/STLLoader.js");
  const { OrbitControls } = await import("three/addons/controls/OrbitControls.js");

  const container = document.getElementById("viewerCanvas");
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  container.replaceChildren(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100000);
  camera.up.set(0, 1, 0);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = true;
  controls.maxPolarAngle = Math.PI * 0.98;

  const hemisphere = new THREE.HemisphereLight(0xdff8ff, 0x0b1722, 1.5);
  scene.add(hemisphere);

  const keyLight = new THREE.DirectionalLight(0xffffff, 2.2);
  keyLight.position.set(1, 2.4, 1.4);
  scene.add(keyLight);

  const fillLight = new THREE.DirectionalLight(0x8fc7d8, 0.9);
  fillLight.position.set(-1.8, 1.2, -0.8);
  scene.add(fillLight);

  const modelGroup = new THREE.Group();
  scene.add(modelGroup);

  let grid = null;
  let axes = null;

  const state = {
    THREE,
    STLLoader,
    renderer,
    scene,
    camera,
    controls,
    modelGroup,
    grid,
    axes,
    loader: new STLLoader(),
    visible: false,
  };

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

function disposeModel(state) {
  const group = state.modelGroup;
  for (const child of [...group.children]) {
    group.remove(child);
    if (child.geometry) child.geometry.dispose();
    if (child.material) {
      if (Array.isArray(child.material)) {
        child.material.forEach((material) => material.dispose());
      } else {
        child.material.dispose();
      }
    }
  }
  if (state.grid) {
    state.scene.remove(state.grid);
    state.grid.geometry.dispose();
    state.grid.material.dispose();
    state.grid = null;
  }
  if (state.axes) {
    state.scene.remove(state.axes);
    state.axes.geometry.dispose();
    state.axes.material.dispose();
    state.axes = null;
  }
}

function fitCamera(state, box, triangles) {
  const { THREE, camera, controls } = state;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.length() * 0.5, 1);

  camera.near = Math.max(radius / 5000, 0.01);
  camera.far = Math.max(radius * 80, 1000);
  camera.updateProjectionMatrix();

  camera.position.set(
    center.x + radius * 1.15,
    center.y + radius * 0.85,
    center.z + radius * 1.15
  );
  controls.target.copy(center);
  controls.minDistance = radius * 0.03;
  controls.maxDistance = radius * 15;
  controls.update();

  const gridSize = Math.max(size.x, size.z) * 2.2;
  const grid = new THREE.GridHelper(gridSize, 40, 0x3e6072, 0x243b4b);
  grid.position.set(center.x, 0, center.z);
  grid.material.transparent = true;
  grid.material.opacity = 0.42;
  state.scene.add(grid);
  state.grid = grid;

  const axes = new THREE.AxesHelper(Math.max(radius * 0.4, 2));
  axes.position.set(box.min.x, 0, box.min.z);
  state.scene.add(axes);
  state.axes = axes;

  const meta = document.getElementById("viewerMeta");
  meta.textContent = `${formatInt(triangles)} 个三角形 · 范围 ${formatMeters(size.x)} × ${formatMeters(
    size.z
  )} × ${formatMeters(size.y)}`;
}

async function loadModels(urls, state) {
  const token = ++loadToken;
  const status = document.getElementById("viewerStatus");
  const meta = document.getElementById("viewerMeta");
  disposeModel(state);
  state.resize();
  status.hidden = false;
  status.textContent = "加载 STL…";
  meta.textContent = "";

  let triangles = 0;
  try {
    for (const item of urls) {
      const geometry = await state.loader.loadAsync(item.url);
      if (token !== loadToken) {
        geometry.dispose();
        return;
      }
      geometry.rotateX(-Math.PI / 2);
      geometry.computeBoundingBox();
      const bbox = geometry.boundingBox;
      if (!bbox) throw new Error("STL 缺少包围盒");
      const center = new state.THREE.Vector3();
      bbox.getCenter(center);
      geometry.translate(-center.x, -bbox.min.y, -center.z);
      geometry.computeBoundingSphere();

      const material = new state.THREE.MeshStandardMaterial({
        color: COLORS[item.kind] ?? COLORS.city,
        metalness: 0.04,
        roughness: 0.82,
        flatShading: true,
        side: state.THREE.DoubleSide,
      });
      const mesh = new state.THREE.Mesh(geometry, material);
      mesh.name = item.kind;
      state.modelGroup.add(mesh);
      triangles += geometry.attributes.position.count / 3;
    }

    if (token !== loadToken) return;
    const box = new state.THREE.Box3().setFromObject(state.modelGroup);
    if (box.isEmpty()) throw new Error("模型为空");
    fitCamera(state, box, triangles);
    status.hidden = true;
  } catch (error) {
    if (token !== loadToken) return;
    console.error(error);
    status.hidden = false;
    status.textContent = `加载失败：${error?.message || "未知错误"}`;
  }
}

export async function openViewer({ urls, title = "3D 预览", meta = "" }) {
  const modal = document.getElementById("viewerModal");
  const status = document.getElementById("viewerStatus");
  document.getElementById("viewerTitle").textContent = title;
  document.getElementById("viewerMeta").textContent = meta;
  modal.hidden = false;
  status.hidden = false;
  status.textContent = "初始化查看器…";

  try {
    if (!runtime) {
      runtime = await createRuntime();
    }
    runtime.visible = true;
    runtime.resize();
    await loadModels(urls, runtime);
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

export function setWireframe(enabled) {
  if (!runtime) return;
  runtime.modelGroup.traverse((child) => {
    if (!child.isMesh) return;
    const materials = Array.isArray(child.material) ? child.material : [child.material];
    for (const material of materials) {
      material.wireframe = Boolean(enabled);
    }
  });
}
