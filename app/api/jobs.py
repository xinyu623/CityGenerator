"""Job creation, status, download, and deletion endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.models.schemas import GenerateRequest, JobCreateResponse, JobStatus
from app.services.errors import JobBusyError, JobNotReadyError
from app.services.jobs import JobManager

router = APIRouter(tags=["jobs"])

_MEDIA_TYPES = {
    ".stl": "model/stl",
    ".json": "application/json",
    ".geojson": "application/geo+json",
    ".txt": "text/plain; charset=utf-8",
    ".zip": "application/zip",
}


def _manager(request: Request) -> JobManager:
    return request.app.state.jobs


@router.post("/jobs", response_model=JobCreateResponse, status_code=202)
def create_job(payload: GenerateRequest, request: Request) -> JobCreateResponse:
    job = _manager(request).create(payload)
    return JobCreateResponse(job_id=job.id, status=job.status)


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str, request: Request) -> JobStatus:
    try:
        return _manager(request).get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@router.get("/jobs/{job_id}/download")
def download_job(job_id: str, request: Request) -> FileResponse:
    manager = _manager(request)
    try:
        path = manager.download_path(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except JobNotReadyError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"citygenerator_{job_id}.zip",
    )


@router.get("/jobs/{job_id}/files/{filename}")
def download_file(job_id: str, filename: str, request: Request) -> FileResponse:
    manager = _manager(request)
    try:
        path = manager.file_path(job_id, filename)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream"),
        filename=filename,
    )


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str, request: Request) -> dict[str, bool]:
    manager = _manager(request)
    try:
        manager.delete(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return {"ok": True}
