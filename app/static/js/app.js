import {
  createJob,
  deleteJob,
  getConfig,
  getJob,
} from "./api.js";
import { createMapController } from "./map.js";
import {
  closeViewer,
  openViewer,
  resetView,
  setWireframe,
  toggleAutoRotate,
} from "./viewer.js";

const $ = (id) => document.getElementById(id);

const STAGE_LABELS = {
  queued: "排队等待",
  starting: "启动任务",
  validating: "校验区域",
  fetching: "获取 OSM 数据",
  parsing: "解析建筑轮廓",
  projecting: "投影到米制坐标",
  building: "挤出建筑",
  ground: "生成实体底座",
  fusing: "融合建筑与地面",
  exporting: "写出 STL 与元数据",
  completed: "生成完成",
  failed: "生成失败",
  cancelled: "已取消",
  cancelling: "正在取消",
};

const state = {
  config: null,
  map: null,
  selection: null,
  jobId: null,
  job: null,
  timer: null,
  busy: false,
};

function formatInt(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat("zh-CN").format(Math.round(number));
}

function formatDistance(meters) {
  if (!Number.isFinite(meters)) return "—";
  if (meters >= 1000) return `${(meters / 1000).toFixed(2)} km`;
  return `${meters.toFixed(0)} m`;
}

function formatArea(km2) {
  if (!Number.isFinite(km2)) return "—";
  if (km2 < 0.01) return `${Math.round(km2 * 1_000_000)} m²`;
  return `${km2.toFixed(3)} km²`;
}

function formatTimestamp(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function setText(id, text) {
  const element = $(id);
  if (element) element.textContent = text;
}

function showError(message) {
  const panel = $("errorPanel");
  panel.hidden = false;
  panel.textContent = message;
}

function clearError() {
  const panel = $("errorPanel");
  panel.hidden = true;
  panel.textContent = "";
}

function showSelectionError(message) {
  const panel = $("selectionError");
  panel.hidden = false;
  panel.textContent = message;
}

function clearSelectionError() {
  const panel = $("selectionError");
  panel.hidden = true;
  panel.textContent = "";
}

function maxAreaKm2() {
  return Number(state.config?.max_area_km2 ?? 4);
}

function applyConfig(config) {
  state.config = config;
  setText("versionPill", `v${config.version}`);
  setText("maxAreaLabel", `${Number(config.max_area_km2).toFixed(1)} km²`);

  const defaults = config.defaults || {};
  if (Number.isFinite(defaults.default_height_m)) {
    $("defaultHeight").value = defaults.default_height_m;
  }
  if (Number.isFinite(defaults.level_height_m)) {
    $("levelHeight").value = defaults.level_height_m;
  }
  if (Number.isFinite(defaults.embed_depth_m)) {
    $("embedDepth").value = defaults.embed_depth_m;
  }
  if (Number.isFinite(defaults.simplify_m)) {
    $("simplify").value = defaults.simplify_m;
  }
  if (Number.isFinite(defaults.min_building_area_m2)) {
    $("minArea").value = defaults.min_building_area_m2;
  }
}

function numberValue(id, fallback) {
  const value = Number($(id).value);
  return Number.isFinite(value) ? value : fallback;
}

function readOptions() {
  return {
    default_height_m: numberValue("defaultHeight", 25),
    level_height_m: numberValue("levelHeight", 3),
    embed_depth_m: numberValue("embedDepth", 0.5),
    ground_margin_m: numberValue("groundMargin", 0),
    simplify_m: numberValue("simplify", 0.05),
    min_building_area_m2: numberValue("minArea", 4),
  };
}

function handleSelection(selection) {
  state.selection = selection;
  const readout = $("mapReadout");
  const badge = $("areaBadge");

  if (!selection) {
    readout.hidden = true;
    badge.textContent = "未选择";
    badge.className = "badge";
    $("drawBtn").textContent = "框选区域";
    $("drawBtn").disabled = state.busy;
    $("clearBtn").disabled = true;
    $("generateBtn").disabled = true;
    $("mapHint").textContent = "点击“框选区域”，然后在地图上点两个角点";
    clearSelectionError();
    return;
  }

  const overLimit = selection.areaKm2 > maxAreaKm2();
  readout.hidden = false;
  setText("widthValue", formatDistance(selection.width));
  setText("heightValue", formatDistance(selection.height));
  setText("areaValue", formatArea(selection.areaKm2));
  setText(
    "bboxValue",
    `${selection.bbox.south.toFixed(5)}, ${selection.bbox.west.toFixed(5)} → ` +
      `${selection.bbox.north.toFixed(5)}, ${selection.bbox.east.toFixed(5)}`
  );
  badge.textContent = overLimit ? "超出上限" : formatArea(selection.areaKm2);
  badge.className = `badge ${overLimit ? "warn" : "ok"}`;
  $("drawBtn").textContent = "重新框选";
  $("drawBtn").disabled = state.busy;
  $("clearBtn").disabled = state.busy;
  $("generateBtn").disabled = state.busy || overLimit;
  $("mapHint").textContent = overLimit
    ? "区域超过上限，请缩小范围"
    : "已选择区域，可调整参数后生成";
  if (overLimit) {
    showSelectionError(
      `当前 ${formatArea(selection.areaKm2)}，超过上限 ${maxAreaKm2().toFixed(
        1
      )} km²，请缩小框选范围。`
    );
  } else {
    clearSelectionError();
  }
}

function setBusy(busy) {
  state.busy = busy;
  const overLimit = Boolean(state.selection && state.selection.areaKm2 > maxAreaKm2());
  $("generateBtn").disabled = busy || !state.selection || overLimit;
  $("generateBtn").textContent = busy ? "生成中…" : "生成 STL";
  $("drawBtn").disabled = busy;
  $("clearBtn").disabled = busy || !state.selection;
  $("cancelBtn").disabled = !busy;
}

function showStatus(percent, stage, message) {
  $("statusPanel").hidden = false;
  setText("statusStage", STAGE_LABELS[stage] || stage || "处理中");
  const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
  setText("statusPercent", `${Math.round(safePercent)}%`);
  $("progressBar").style.width = `${safePercent}%`;
  setText("statusMessage", message || "");
}

function stopPolling() {
  if (state.timer) {
    clearTimeout(state.timer);
    state.timer = null;
  }
}

async function poll() {
  const jobId = state.jobId;
  if (!jobId) return;
  try {
    const job = await getJob(jobId);
    if (state.jobId !== jobId) return;
    state.job = job;
    showStatus(job.progress, job.stage, job.message);
    if (["completed", "failed", "cancelled"].includes(job.status)) {
      handleFinished(job);
      return;
    }
  } catch (error) {
    if (state.jobId !== jobId) return;
    setBusy(false);
    showError(error.message);
    return;
  }
  if (state.jobId === jobId) {
    state.timer = setTimeout(poll, 900);
  }
}

function clearResult() {
  $("resultPanel").hidden = true;
  $("statsGrid").replaceChildren();
  $("warnings").replaceChildren();
  setText("fileSummary", "");
}

function addStat(label, value) {
  const wrapper = document.createElement("div");
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  wrapper.append(dt, dd);
  $("statsGrid").append(wrapper);
}

function renderResult(job) {
  const stats = job.stats || {};
  const sources = stats.height_sources || {};
  clearResult();
  $("resultPanel").hidden = false;
  $("downloadBtn").href = job.download_url || "#";
  $("previewBtn").disabled = false;

  addStat("建筑", formatInt(stats.buildings));
  addStat("三角形", formatInt(stats.triangles));
  addStat("选择面积", `${Number(stats.area_km2 || 0).toFixed(3)} km²`);
  addStat("高度来源", `${formatInt(sources.tag)} / ${formatInt(sources.levels)} / ${formatInt(sources.default)}`);
  addStat("跳过", formatInt(stats.skipped));
  addStat("重叠", formatInt(stats.overlaps));
  addStat("密闭", stats.city_watertight ? "是" : "否");
  addStat("OSM 数据", formatTimestamp(stats.osm_base_timestamp));

  const warnings = job.warnings || [];
  const warningBox = $("warnings");
  if (!warnings.length) {
    const empty = document.createElement("p");
    empty.className = "muted small";
    empty.textContent = "没有数据质量警告。";
    warningBox.append(empty);
  } else {
    for (const warning of warnings) {
      const item = document.createElement("div");
      item.className = "warning-item";
      const code = document.createElement("strong");
      code.textContent = warning.code;
      item.append(code, document.createElement("br"), document.createTextNode(warning.message));
      warningBox.append(item);
    }
  }

  setText("fileSummary", `${job.files?.length || 0} 个文件 · ZIP 已打包`);
  $("resultPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function handleFinished(job) {
  stopPolling();
  setBusy(false);
  if (job.status === "completed") {
    showStatus(100, "completed", "生成完成");
    renderResult(job);
    return;
  }
  if (job.status === "cancelled") {
    showStatus(0, "cancelled", "任务已取消");
    showError("任务已取消。");
    return;
  }
  const error = job.error || {};
  const detail = error.detail ? `（${error.detail}）` : "";
  showStatus(job.progress || 0, "failed", "生成失败");
  showError(`${error.message || job.message || "生成失败"}${detail}`);
}

async function generate() {
  if (state.busy) return;
  if (!state.selection) {
    showSelectionError("请先在地图上框选一个区域。");
    return;
  }
  if (state.selection.areaKm2 > maxAreaKm2()) {
    showSelectionError("选择区域超过面积上限，请缩小范围。");
    return;
  }

  clearError();
  clearResult();
  setBusy(true);
  showStatus(0, "queued", "正在创建任务…");

  try {
    const response = await createJob({
      bbox: state.selection.bbox,
      options: readOptions(),
      refresh: $("refresh").checked,
    });
    state.jobId = response.job_id;
    state.job = null;
    poll();
  } catch (error) {
    setBusy(false);
    showStatus(0, "failed", "提交失败");
    showError(error.message);
  }
}

async function cancel() {
  if (!state.jobId) return;
  $("cancelBtn").disabled = true;
  try {
    await deleteJob(state.jobId);
    stopPolling();
    state.jobId = null;
    state.job = null;
    setBusy(false);
    showStatus(0, "cancelled", "任务已取消");
    showError("任务已取消。");
  } catch (error) {
    $("cancelBtn").disabled = false;
    showError(error.message);
  }
}

function preview() {
  if (!state.job || state.job.status !== "completed") return;
  const cityFile = (state.job.files || []).find((file) => file.name === "city.stl");
  if (!cityFile) {
    showError("当前结果没有可预览的 city.stl 文件。");
    return;
  }
  const stats = state.job.stats || {};
  openViewer({
    urls: [{ url: cityFile.url, kind: "city" }],
    title: "CityGenerator · 城市实体预览",
    meta: `${formatInt(stats.buildings)} 栋建筑 · ${formatInt(stats.triangles)} 个三角形`,
    groundLevel: Number(stats.base_thickness_m) || 1.0,
  });
}

function bindEvents() {
  $("optionsForm").addEventListener("submit", (event) => {
    event.preventDefault();
  });

  $("drawBtn").addEventListener("click", () => {
    if (!state.map || state.busy) return;
    clearSelectionError();
    state.map.startSelection();
    $("drawBtn").textContent = "选择中…";
    $("mapHint").textContent = "点击第一个角点，然后点击对角点";
  });

  $("clearBtn").addEventListener("click", () => {
    state.map?.clearSelection();
    handleSelection(null);
  });

  $("generateBtn").addEventListener("click", generate);
  $("cancelBtn").addEventListener("click", cancel);
  $("previewBtn").addEventListener("click", preview);

  $("viewerClose").addEventListener("click", closeViewer);
  $("viewerModal").addEventListener("click", (event) => {
    if (event.target === $("viewerModal")) closeViewer();
  });
  $("viewerReset").addEventListener("click", resetView);
  $("viewerRotate").addEventListener("click", toggleAutoRotate);
  $("wireframeToggle").addEventListener("change", (event) => {
    setWireframe(event.target.checked);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("viewerModal").hidden) closeViewer();
  });

  window.addEventListener("resize", () => state.map?.invalidate());
}

async function init() {
  bindEvents();
  setBusy(false);

  try {
    applyConfig(await getConfig());
  } catch (error) {
    showError(`无法读取后端配置：${error.message}`);
  }

  state.map = createMapController({
    onChange: handleSelection,
    onError: showError,
  });
  if (!state.map) {
    $("drawBtn").disabled = true;
    return;
  }
  window.setTimeout(() => state.map?.invalidate(), 120);
}

init();
