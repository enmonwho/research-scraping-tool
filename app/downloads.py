"""Explicit, bounded downloads from provider-supplied research file URLs."""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
from urllib.parse import unquote, urljoin, urlsplit

from app.models import DownloadableFile, ResearchResource, ResourceType

DEFAULT_DOWNLOAD_DIR = Path("data/downloads")
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
REDIRECT_CODES = {301, 302, 303, 307, 308}


class DownloadError(Exception):
    """A refused or failed download with a user-readable reason."""


def _public_url(url: str) -> tuple[str, int, str, str]:
    if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
        raise DownloadError("download URL is malformed")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DownloadError("download URL is malformed") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise DownloadError("only public HTTP or HTTPS download URLs are allowed")
    host = parsed.hostname.rstrip(".")
    if not host:
        raise DownloadError("download URL is malformed")
    port = port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )]
        except (OSError, ValueError) as exc:
            raise DownloadError("download host could not be resolved") from exc
    if not addresses or any(not ip.is_global for ip in addresses):
        raise DownloadError("download URL resolves to a non-public network address")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return parsed.scheme, port, host, path


def _connect_pinned(scheme: str, port: int, host: str, timeout: float):
    """Resolve, reject every non-public answer, and connect to a vetted IP directly."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DownloadError(f"Could not resolve download host: {exc}") from exc
    candidates = list(dict.fromkeys(info[4][0] for info in infos))
    if not candidates or any(not ipaddress.ip_address(address).is_global for address in candidates):
        raise DownloadError("download host resolves to a non-public network address")
    errors = []
    sock = None
    for address in candidates:
        try:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((address, port))
            break
        except OSError as exc:
            errors.append(exc)
            if sock:
                sock.close()
            sock = None
    if sock is None:
        raise DownloadError(f"Could not connect to download host: {errors[-1] if errors else 'no address'}")
    if scheme == "https":
        try:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        except (OSError, ssl.SSLError) as exc:
            sock.close()
            raise DownloadError(f"Secure connection failed: {exc}") from exc
    return sock


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)

    def connect(self):
        self.sock = _connect_pinned("http", self.port, self.host, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)

    def connect(self):
        raw = _connect_pinned("https", self.port, self.host, self.timeout)
        self.sock = raw


def _request(url: str, timeout: float):
    scheme, port, host, path = _public_url(url)
    connection_class = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    connection = connection_class(host, port, timeout)
    try:
        connection.request("GET", path, headers={"User-Agent": "research-discovery-cli/0.1",
                                                  "Accept": "application/pdf,text/csv,application/zip,*/*"})
        response = connection.getresponse()
        return connection, response
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        connection.close()
        raise DownloadError(f"Network failure while downloading: {exc}") from exc


def _safe_filename(value: str | None, fallback: str) -> str:
    value = unquote(value or fallback).replace("\\", "/").split("/")[-1]
    value = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", value).strip(" .")
    if value in ("", ".", ".."):
        value = fallback
    return value[:180]


def download_file(file: DownloadableFile, resource: ResearchResource, *,
                  download_dir: str | Path = DEFAULT_DOWNLOAD_DIR,
                  timeout: float = 15.0, max_bytes: int = MAX_DOWNLOAD_BYTES,
                  max_redirects: int = 5) -> Path:
    """Download one selected provider file, streaming into a confined temp file."""
    if file.is_open_access is False:
        raise DownloadError("file is marked restricted by its provider")
    if not math.isfinite(timeout) or timeout <= 0 or max_bytes <= 0:
        raise ValueError("timeout must be positive and finite, and maximum size must be positive")
    if not isinstance(max_redirects, int) or isinstance(max_redirects, bool) or max_redirects < 0:
        raise ValueError("maximum redirects must be a nonnegative integer")
    root = Path(download_dir).expanduser().resolve()
    kind = "datasets" if resource.resource_type == ResourceType.DATASET else "papers"
    target_dir = (root / kind).resolve()
    if not target_dir.is_relative_to(root):
        raise DownloadError("download destination escapes the selected directory")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if not target_dir.is_dir():
            raise OSError("destination is not a directory")
    except OSError as exc:
        raise DownloadError(f"Invalid download directory: {exc}") from exc

    extension = file.file_format or {
        "application/pdf": "pdf", "text/csv": "csv", "application/zip": "zip",
        "application/x-zip-compressed": "zip",
    }.get((file.mime_type or "").split(";", 1)[0].lower(), "")
    extension = extension.lower().lstrip(".")
    suffix = f".{extension}" if re.fullmatch(r"[a-z0-9]{1,12}", extension) else ""
    fallback = _safe_filename(None, f"resource-{resource.external_id or 'file'}") + suffix
    name = _safe_filename(file.filename, fallback)
    temporary: str | None = None
    sidecar_name: str | None = None
    finalized_path: Path | None = None
    complete = False
    connection = None
    response = None
    url = file.url
    try:
        for redirects in range(max_redirects + 1):
            connection, response = _request(url, timeout)
            if response.status in REDIRECT_CODES:
                location = response.getheader("Location")
                connection.close()
                connection = None
                if not location or redirects == max_redirects:
                    raise DownloadError("download redirect limit reached or missing location")
                url = urljoin(url, location)
                _public_url(url)  # validate each redirect before making its request
                continue
            break
        if response.status in (401, 403):
            raise DownloadError(f"access restricted by provider (HTTP {response.status})")
        if response.status == 404:
            raise DownloadError("file is unavailable (HTTP 404)")
        if response.status == 429:
            raise DownloadError("provider rate limit reached (HTTP 429)")
        if response.status >= 400:
            raise DownloadError(f"provider returned HTTP {response.status}")
        content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type in ("text/html", "application/xhtml+xml"):
            raise DownloadError("provider returned an HTML page instead of a file")
        length = response.getheader("Content-Length")
        declared_length = None
        if length:
            try:
                declared_length = int(length)
                if declared_length < 0:
                    raise ValueError
                if declared_length > max_bytes:
                    raise DownloadError(f"file exceeds maximum size of {max_bytes} bytes")
            except ValueError:
                raise DownloadError("provider returned an invalid Content-Length")
        prefix = bytearray()
        with tempfile.NamedTemporaryFile(mode="wb", dir=target_dir, prefix=".download-", delete=False) as stream:
            temporary = stream.name
            total = 0
            while True:
                chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadError(f"file exceeds maximum size of {max_bytes} bytes")
                if len(prefix) < 512:
                    prefix.extend(chunk[:512 - len(prefix)])
                stream.write(chunk)
        if not prefix:
            raise DownloadError("provider returned an empty file")
        if declared_length is not None and total != declared_length:
            raise DownloadError("download ended before the provider-reported file size")
        if prefix.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise DownloadError("provider returned an HTML page instead of a file")
        if file.mime_type and content_type and file.mime_type.split(";", 1)[0].lower() != content_type:
            # Generic provider MIME types do not veto a valid response.
            if file.mime_type.lower() not in ("application/octet-stream", "binary/octet-stream"):
                raise DownloadError(f"response content type {content_type} does not match provider metadata")
        candidate = target_dir / name
        stem, suffix = candidate.stem, candidate.suffix
        counter = 1
        while True:
            if candidate.exists() or candidate.with_name(candidate.name + ".source.json").exists():
                candidate = target_dir / f"{stem} ({counter}){suffix}"
                counter += 1
                continue
            try:
                os.link(temporary, candidate)
                finalized_path = candidate
                break
            except FileExistsError:
                candidate = target_dir / f"{stem} ({counter}){suffix}"
                counter += 1
            except OSError:
                raise DownloadError("destination filesystem cannot safely finalize downloads")
        metadata_path = candidate.with_name(candidate.name + ".source.json")
        metadata = {
            "source_url": file.url,
            "provider": file.source_provider,
            "resource_type": resource.resource_type.value,
            "title": resource.title,
            "license": resource.license,
            "provider_filename": file.filename,
            "format": file.file_format,
            "provider_mime_type": file.mime_type,
            "response_mime_type": content_type or None,
            "size_bytes": total,
        }
        # Keep provenance as an adjacent, atomically created file without modifying SQLite.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target_dir,
                                         prefix=".metadata-", delete=False) as sidecar:
            sidecar_name = sidecar.name
            json.dump(metadata, sidecar, ensure_ascii=False, indent=2)
            sidecar.write("\n")
        try:
            os.link(sidecar_name, metadata_path)
        except FileExistsError:
            candidate.unlink(missing_ok=True)
            raise DownloadError("download filename collided while saving provenance metadata")
        finally:
            os.unlink(sidecar_name)
            sidecar_name = None
        complete = True
        return candidate
    except DownloadError:
        raise
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise DownloadError(f"Download failed: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
        if response is not None:
            response.close()
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        if sidecar_name:
            try:
                os.unlink(sidecar_name)
            except FileNotFoundError:
                pass
        if finalized_path and not complete:
            finalized_path.unlink(missing_ok=True)
