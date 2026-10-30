"""Disk-backed job manager around a small thread pool."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.models.schemas import GenerateRequest, JobStatus
from app.services.errors import (
    CityGeneratorError,
    JobBusyError,
    JobCancelled,
    JobNotReadyError,
)
from app.services.io_utils import (
    is_older_than,
    read_json,
    utc_now_iso,
    write_json_atomic,
)
from app.services.pipeline import run_pipeline

logger = logging.getLogger(__name__)


class JobManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_dirs()
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, settings.max_workers),
            thread_name_prefix="citygen",
        )
        self._cancel_events: dict[str, threading.Event] = {}
        self._futures: dict[str, Future] = {}
        self.recover()

    # -- paths ---------------------------------------------------------
    def _job_dir(self, job_id: str) -> Path:
        return self.settings.jobs_dir / job_id

    def _state_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    # -- state ---------------------------------------------------------
    def _read_state(self, job_id: str) -> dict[str, Any]:
        path = self._state_path(job_id)
        if not path.exists():
            raise KeyError(job_id)
        return read_json(path)

    def _write_state(self, state: dict[str, Any]) -> None:
        write_json_atomic(self._state_path(state["id"]), state)

    def _update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            state = self._read_state(job_id)
            state.update(fields)
            state["updated_at"] = utc_now_iso()
            self._write_state(state)
            return state

    def _public(self, state: dict[str, Any]) -> JobStatus:
        return JobStatus.model_validate(state)

    # -- lifecycle -----------------------------------------------------
    def create(self, request: GenerateRequest) -> JobStatus:
        job_id = uuid4().hex
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        now = utc_now_iso()
        state: dict[str, Any] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "stage": "queued",
            "message": "等待开始",
            "created_at": now,
            "updated_at": now,
            "stats": {},
            "warnings": [],
            "error": None,
            "files": [],
            "download_url": None,
            "request": request.model_dump(),
        }
        self._write_state(state)
        write_json_atomic(job_dir / "request.json", request.model_dump())

        event = threading.Event()
        with self._lock:
            self._cancel_events[job_id] = event
            future = self._executor.submit(self._run, job_id, request, event)
            self._futures[job_id] = future
        return self._public(state)

    def _run(
        self,
        job_id: str,
        request: GenerateRequest,
        cancel_event: threading.Event,
    ) -> None:
        try:
            self._update(
                job_id,
                status="running",
                progress=1,
                stage="starting",
                message="任务开始",
            )

            last_progress_at = 0.0
            last_progress_stage: str | None = None

            def progress(percent: int, stage: str, message: str) -> None:
                nonlocal last_progress_at, last_progress_stage
                now = time.monotonic()
                # build_meshes reports every building. Rewriting job.json for
                # each one dominates cache-hit latency on large areas, while
                # the UI polls less than once a second anyway.
                if stage == last_progress_stage and percent < 100 and now - last_progress_at < 0.5:
                    return
                last_progress_at = now
                last_progress_stage = stage
                self._update(
                    job_id,
                    status="running",
                    progress=max(0, min(100, int(percent))),
                    stage=stage,
                    message=message,
                )

            result = run_pipeline(
                request,
                self.settings,
                self._job_dir(job_id),
                progress_cb=progress,
                cancel_event=cancel_event,
            )
            self._update(
                job_id,
                status="completed",
                progress=100,
                stage="completed",
                message="生成完成",
                stats=result.stats,
                warnings=[warning.model_dump() for warning in result.warnings],
                files=[file.model_dump() for file in result.files],
                download_url=f"/api/jobs/{job_id}/download",
                error=None,
            )
        except JobCancelled:
            self._update(
                job_id,
                status="cancelled",
                progress=0,
                stage="cancelled",
                message="任务已取消",
                error={"code": "JOB_CANCELLED", "message": "任务已取消", "detail": None},
            )
        except CityGeneratorError as exc:
            self._update(
                job_id,
                status="failed",
                stage="failed",
                message=exc.message,
                error={
                    "code": exc.code,
                    "message": exc.message,
                    "detail": exc.detail,
                },
            )
        except Exception as exc:  # noqa: BLE001 - job boundary
            logger.exception("job %s failed", job_id)
            self._update(
                job_id,
                status="failed",
                stage="failed",
                message="生成失败",
                error={
                    "code": "INTERNAL_ERROR",
                    "message": "生成失败",
                    "detail": str(exc),
                },
            )
        finally:
            with self._lock:
                self._cancel_events.pop(job_id, None)
                self._futures.pop(job_id, None)

    # -- queries -------------------------------------------------------
    def get(self, job_id: str) -> JobStatus:
        with self._lock:
            return self._public(self._read_state(job_id))

    def cancel(self, job_id: str) -> JobStatus:
        with self._lock:
            state = self._read_state(job_id)
            if state["status"] in {"completed", "failed", "cancelled"}:
                return self._public(state)
            event = self._cancel_events.get(job_id)
            if event is not None:
                event.set()
            future = self._futures.get(job_id)
            if future is not None and future.cancel():
                state = self._update(
                    job_id,
                    status="cancelled",
                    stage="cancelled",
                    message="任务已取消",
                    error={"code": "JOB_CANCELLED", "message": "任务已取消", "detail": None},
                )
                return self._public(state)
            return self._public(self._update(job_id, message="正在取消…", stage="cancelling"))

    def delete(self, job_id: str) -> None:
        with self._lock:
            state = self._read_state(job_id)
            future = self._futures.get(job_id)
            event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
        if future is not None and not future.done():
            try:
                future.result(timeout=2.0)
            except Exception:
                pass
            if not future.done():
                raise JobBusyError("任务仍在运行，请稍后再删除")
        if state["status"] == "running":
            # The worker may have been between state writes; re-read after the
            # cancellation wait to decide whether it is safe to remove files.
            with self._lock:
                state = self._read_state(job_id)
            if state["status"] == "running":
                raise JobBusyError("任务仍在运行，请稍后再删除")
        shutil.rmtree(self._job_dir(job_id), ignore_errors=True)

    def file_path(self, job_id: str, filename: str) -> Path:
        if Path(filename).name != filename or "\\" in filename or "/" in filename:
            raise KeyError(filename)
        with self._lock:
            state = self._read_state(job_id)
        allowed = {item["name"] for item in state.get("files", [])}
        if filename not in allowed:
            raise KeyError(filename)
        path = self._job_dir(job_id) / "output" / filename
        if not path.is_file():
            raise KeyError(filename)
        return path

    def preview_path(self, job_id: str) -> Path:
        with self._lock:
            state = self._read_state(job_id)
        if state["status"] != "completed":
            raise JobNotReadyError("任务尚未完成")
        preview = self._job_dir(job_id) / "output" / "preview.stl"
        if preview.is_file():
            return preview
        fallback = self._job_dir(job_id) / "output" / "city.stl"
        if fallback.is_file():
            return fallback
        raise JobNotReadyError("预览文件不存在")

    def download_path(self, job_id: str) -> Path:
        with self._lock:
            state = self._read_state(job_id)
        if state["status"] != "completed":
            raise JobNotReadyError("任务尚未完成")
        path = self._job_dir(job_id) / "output" / "result.zip"
        if not path.is_file():
            raise JobNotReadyError("结果文件不存在")
        return path

    # -- maintenance ---------------------------------------------------
    def recover(self) -> None:
        for job_dir in self.settings.jobs_dir.iterdir():
            if not job_dir.is_dir():
                continue
            state_path = job_dir / "job.json"
            if not state_path.exists():
                continue
            try:
                state = read_json(state_path)
            except Exception:
                continue
            if state.get("status") in {"queued", "running"}:
                state.update(
                    {
                        "status": "failed",
                        "stage": "failed",
                        "message": "服务重启导致任务中断，请重试",
                        "updated_at": utc_now_iso(),
                        "error": {
                            "code": "SERVER_RESTARTED",
                            "message": "服务重启导致任务中断，请重试",
                            "detail": None,
                        },
                    }
                )
                write_json_atomic(state_path, state)
        self.cleanup()

    def cleanup(self) -> None:
        for job_dir in list(self.settings.jobs_dir.iterdir()):
            if not job_dir.is_dir() or job_dir.name in self._futures:
                continue
            if is_older_than(job_dir, self.settings.job_ttl_days):
                shutil.rmtree(job_dir, ignore_errors=True)
        cache_dir = self.settings.cache_dir / "osm"
        if cache_dir.exists():
            for path in cache_dir.iterdir():
                if path.is_file() and is_older_than(path, self.settings.cache_ttl_days):
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def shutdown(self) -> None:
        with self._lock:
            for event in self._cancel_events.values():
                event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
