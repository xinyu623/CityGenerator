const JSON_HEADERS = { "Content-Type": "application/json" };

export class ApiError extends Error {
  constructor(message, status = 0, detail = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function detailMessage(payload) {
  if (!payload) return "";
  const detail = payload.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => item?.msg || JSON.stringify(item))
      .filter(Boolean)
      .join("; ");
  }
  if (detail) return JSON.stringify(detail);
  return "";
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers: { ...(options.headers || {}) },
    });
  } catch (error) {
    throw new ApiError("无法连接后端服务，请确认 uvicorn 正在运行", 0, error);
  }

  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      // ignore non-JSON error bodies
    }
    const message = detailMessage(payload) || `请求失败（HTTP ${response.status}）`;
    throw new ApiError(message, response.status, payload);
  }

  if (response.status === 204) return null;
  return response.json();
}

export function getConfig() {
  return request("/api/config");
}

export function getHealth() {
  return request("/api/health");
}

export function createJob(payload) {
  return request("/api/jobs", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });
}

export function getJob(jobId) {
  return request(`/api/jobs/${encodeURIComponent(jobId)}`);
}

export function deleteJob(jobId) {
  return request(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
}

export function fileUrl(jobId, name) {
  return `/api/jobs/${encodeURIComponent(jobId)}/files/${encodeURIComponent(name)}`;
}
