"""Small authenticated HTTP job bridge for optional n8n automation."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from app.core.search_service import (search_all_and_save, search_and_save_with_id,
                                    expanded_search_and_save, get_history_resources)
from app.core.topic import validate_topic
from app.providers.openalex import MAX_RESULTS
from app.providers.openalex import search_papers as search_openalex
from app.providers.crossref import search_papers as search_crossref
from app.storage import DEFAULT_DB_PATH, ResearchStore

MAX_BODY_BYTES = 16_384
JOB_RETENTION = 256
MAX_PENDING_JOBS = 16


@dataclass(frozen=True)
class SearchJob:
    """In-memory state for one submitted search request."""

    id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


class JobRegistry:
    """Thread-safe bounded registry plus a small search worker pool."""

    def __init__(self, db_path: str | Path, *, workers: int = 2) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._jobs: dict[str, SearchJob] = {}
        self._futures: dict[str, Future] = {}
        self._executor = _BoundedExecutor(workers=workers, max_pending=MAX_PENDING_JOBS,
                                          thread_name_prefix="research-search")

    def submit(self, request: dict[str, Any]) -> str:
        job_id = uuid4().hex
        with self._lock:
            self._jobs[job_id] = SearchJob(job_id, "queued")
            try:
                self._futures[job_id] = self._executor.submit(self._run, job_id, request)
            except RuntimeError:
                self._jobs.pop(job_id, None)
                raise
            self._trim()
        return job_id

    def get(self, job_id: str) -> SearchJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _trim(self) -> None:
        while len(self._jobs) > JOB_RETENTION:
            removable = next((key for key, job in self._jobs.items()
                              if job.status in {"succeeded", "failed"}), None)
            if removable is None:
                break
            self._jobs.pop(removable, None)
            self._futures.pop(removable, None)

    def _run(self, job_id: str, request: dict[str, Any]) -> None:
        with self._lock:
            self._jobs[job_id] = SearchJob(job_id, "running")
        try:
            topic = validate_topic(request["topic"])
            limit = request.get("limit", MAX_RESULTS)
            timeout = request.get("timeout", 10.0)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
                raise ValueError(f"limit must be an integer from 1 to {MAX_RESULTS}")
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
                raise ValueError("timeout must be a number greater than 0 and at most 120 seconds")
            provider = request.get("provider", "openalex")
            if provider not in {"openalex", "crossref", "all"}:
                raise ValueError("provider must be openalex, crossref, or all")
            expand = request.get("expand", False)
            if not isinstance(expand, bool):
                raise ValueError("expand must be a boolean")
            if expand:
                expansion_limit = request.get("expand_limit", 4)
                if isinstance(expansion_limit, bool) or not isinstance(expansion_limit, int) or not 0 <= expansion_limit <= 4:
                    raise ValueError("expand_limit must be an integer from 0 to 4")
                providers = (("OpenAlex", search_openalex), ("Crossref", search_crossref))
                selected = providers if provider == "all" else tuple(
                    item for item in providers if item[0].casefold() == provider.casefold()
                )
                outcome = expanded_search_and_save(topic, limit=limit, timeout=timeout,
                                                   db_path=self.db_path, expansion_limit=expansion_limit,
                                                   providers=selected)
                search_id = outcome.search_id
                metadata: dict[str, Any] = {
                    "status": "partial" if outcome.failed_queries else "complete",
                    "providers": outcome.providers,
                    "provider_counts": outcome.provider_counts,
                    "failed_providers": outcome.failed_queries,
                }
            elif provider == "all":
                outcome = search_all_and_save(topic, limit=limit, timeout=timeout, db_path=self.db_path)
                search_id = outcome.search_id
                metadata = {"status": "partial" if outcome.failed_providers else "complete",
                            "providers": ["OpenAlex", "Crossref"],
                            "provider_counts": outcome.provider_counts,
                            "failed_providers": outcome.failed_providers}
            else:
                selected_provider = search_crossref if provider == "crossref" else None
                _resources, search_id = search_and_save_with_id(
                    topic, limit=limit, timeout=timeout, db_path=self.db_path, provider=selected_provider
                )
                metadata = {"status": "complete", "providers": ["Crossref" if provider == "crossref" else "OpenAlex"]}
            resources = get_history_resources(search_id, self.db_path)
            with ResearchStore(self.db_path) as store:
                summary = next(item for item in store.list_searches() if item.id == search_id)
                metadata = summary.metadata or metadata
            result = {"search_id": search_id, "topic": topic, "result_count": len(resources),
                      "status": metadata.get("status", "complete"), "metadata": metadata,
                      "results": [_resource_data(item) for item in resources]}
            self._finish(job_id, SearchJob(job_id, "succeeded", result=result))
        except Exception as exc:
            self._finish(job_id, SearchJob(job_id, "failed", error=str(exc) or exc.__class__.__name__))

    def _finish(self, job_id: str, job: SearchJob) -> None:
        with self._lock:
            self._jobs[job_id] = job
            self._futures.pop(job_id, None)
            self._trim()


def _resource_data(resource: Any) -> dict[str, Any]:
    from app.storage.sqlite import _resource_data as serialize
    return serialize(resource)


def _job_data(job: SearchJob) -> dict[str, Any]:
    data: dict[str, Any] = {"job_id": job.id, "status": job.status}
    if job.result is not None:
        data["result"] = job.result
    if job.error is not None:
        data["error"] = job.error
    return data


class _BoundedExecutor(ThreadPoolExecutor):
    """Thread pool that rejects submissions once its pending queue is full."""

    def __init__(self, *, workers: int, max_pending: int, thread_name_prefix: str) -> None:
        super().__init__(max_workers=workers, thread_name_prefix=thread_name_prefix)
        self._pending_slots = threading.BoundedSemaphore(max_pending)

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future:
        if not self._pending_slots.acquire(blocking=False):
            raise RuntimeError("search queue is full; retry after an active job completes")
        try:
            future = super().submit(fn, *args, **kwargs)
        except BaseException:
            self._pending_slots.release()
            raise
        future.add_done_callback(lambda _future: self._pending_slots.release())
        return future


def create_server(*, host: str = "127.0.0.1", port: int = 8765,
                  token: str | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> ThreadingHTTPServer:
    """Create the loopback API server; a bearer token is mandatory."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("the n8n bridge may bind only to loopback addresses")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer from 0 to 65535")
    auth_token = token or os.environ.get("RESEARCH_DISCOVERY_TOKEN")
    if not auth_token or len(auth_token) < 24:
        raise ValueError("set RESEARCH_DISCOVERY_TOKEN to a secret of at least 24 characters")
    registry = JobRegistry(db_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ResearchDiscoveryBridge/1.0"
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/jobs":
                self._send(404, {"error": "not found"})
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"}, headers={"WWW-Authenticate": "Bearer"})
                return
            try:
                length_text = self.headers.get("Content-Length", "")
                if not length_text.isdecimal():
                    raise ValueError("a valid Content-Length header is required")
                length = int(length_text)
                if not 1 <= length <= MAX_BODY_BYTES:
                    self._send(413 if length > MAX_BODY_BYTES else 400, {"error": "request body size is invalid"})
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) - {"topic", "provider", "limit", "timeout", "expand", "expand_limit"}:
                    raise ValueError("body must be an object containing only supported search fields")
                if not isinstance(body.get("topic"), str):
                    raise ValueError("topic must be a string")
                validate_topic(body["topic"])
                if "limit" in body and (isinstance(body["limit"], bool) or
                                         not isinstance(body["limit"], int) or
                                         not 1 <= body["limit"] <= MAX_RESULTS):
                    raise ValueError(f"limit must be an integer from 1 to {MAX_RESULTS}")
                if "timeout" in body and (isinstance(body["timeout"], bool) or
                                           not isinstance(body["timeout"], (int, float)) or
                                           not 0 < body["timeout"] <= 120):
                    raise ValueError("timeout must be a number greater than 0 and at most 120 seconds")
                if "provider" in body and body["provider"] not in {"openalex", "crossref", "all"}:
                    raise ValueError("provider must be openalex, crossref, or all")
                if "expand" in body and not isinstance(body["expand"], bool):
                    raise ValueError("expand must be a boolean")
                if "expand_limit" in body and (isinstance(body["expand_limit"], bool) or
                                                not isinstance(body["expand_limit"], int) or
                                                not 0 <= body["expand_limit"] <= 4):
                    raise ValueError("expand_limit must be an integer from 0 to 4")
                try:
                    job_id = registry.submit(body)
                except RuntimeError as exc:
                    self._send(503, {"error": str(exc)})
                    return
                self._send(202, {"job_id": job_id, "status": "queued",
                                 "status_url": f"/jobs/{job_id}"}, headers={"Location": f"/jobs/{job_id}"})
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send(400, {"error": str(exc)})

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if not self._authorized():
                self._send(401, {"error": "unauthorized"}, headers={"WWW-Authenticate": "Bearer"})
                return
            match = re.fullmatch(r"/jobs/([0-9a-f]{32})", urlsplit(self.path).path)
            if not match:
                self._send(404, {"error": "not found"})
                return
            job = registry.get(match.group(1))
            if job is None:
                self._send(404, {"error": "job not found or expired"})
                return
            self._send(200, _job_data(job))

        def log_message(self, fmt: str, *args: object) -> None:
            # Avoid logging authorization headers or untrusted request bodies.
            return

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {auth_token}"
            return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

        def _send(self, status: int, data: dict[str, Any], *, headers: dict[str, str] | None = None) -> None:
            encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.registry = registry  # type: ignore[attr-defined]
    return server


def serve(*, host: str = "127.0.0.1", port: int = 8765,
          token: str | None = None, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    server = create_server(host=host, port=port, token=token, db_path=db_path)
    try:
        server.serve_forever()
    finally:
        server.registry.close()  # type: ignore[attr-defined]
        server.server_close()
