"""Deterministic security and integrity tests for the Phase 9 downloader."""

from __future__ import annotations

import contextlib
import io
import json
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch

from app.downloads import DownloadError, download_file
import app.downloads as manager
from app.models import DownloadableFile, LinkType, ResearchResource, ResourceLink, ResourceType
from app.cli.main import main


PUBLIC_URL = "https://files.example.test/research/file"


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict | None = None,
                 chunks: list[bytes] | None = None):
        self.status = status
        self._body = body
        self._chunks = list(chunks) if chunks is not None else [body]
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.closed = False

    def getheader(self, name: str, default=None):
        return self.headers.get(name.lower(), default)

    def read(self, amount: int = -1):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def make_resource(files: tuple[DownloadableFile, ...] = (), *, kind=ResourceType.PAPER):
    return ResearchResource(
        resource_type=kind, title="A real study", description=None, authors=None,
        publication_year=None, source_provider="OpenAlex", external_id="W123", doi=None,
        keywords=None, retrieved_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        license="CC-BY", is_open_access=True,
        links=(ResourceLink(LinkType.LANDING_PAGE, "https://example.org/study", "OpenAlex"),),
        downloadable_files=files,
    )


def make_file(url=PUBLIC_URL, filename="paper.pdf", mime="application/pdf", fmt="pdf", open_access=True):
    return DownloadableFile(url, fmt, mime, filename, "OpenAlex", open_access)


class DownloadManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "downloads"
        self.connection = FakeConnection()

    def perform(self, body: bytes, *, file=None, resource=None, status=200, headers=None,
                chunks=None, max_bytes=1024, request_mock=None):
        selected = file or make_file()
        response = FakeResponse(body, status=status, headers=headers, chunks=chunks)
        request_mock = request_mock or Mock(return_value=(self.connection, response))
        with patch.object(manager, "_public_url", return_value=("https", 443, "files.example.test", "/research/file")), \
                patch.object(manager, "_request", request_mock):
            path = download_file(selected, resource or make_resource((selected,)),
                                 download_dir=self.directory, timeout=2, max_bytes=max_bytes)
        return path, response, request_mock

    def test_successful_pdf_download_preserves_contents(self):
        body = b"%PDF-1.7\nactual pdf bytes"
        path, response, _ = self.perform(body, headers={"Content-Type": "application/pdf", "Content-Length": str(len(body))})
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(path.name, "paper.pdf")
        self.assertTrue(response.closed)

    def test_source_and_license_metadata_are_preserved_in_sidecar(self):
        path, _, _ = self.perform(b"%PDF-1.7 content")
        metadata = json.loads(Path(str(path) + ".source.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["source_url"], PUBLIC_URL)
        self.assertEqual(metadata["license"], "CC-BY")
        self.assertEqual(metadata["size_bytes"], len(b"%PDF-1.7 content"))

    def test_successful_csv_download(self):
        body = b"id,title\n1,Study\n"
        path, _, _ = self.perform(body, file=make_file(filename="data.csv", mime="text/csv", fmt="csv"),
                                  headers={"Content-Type": "text/csv"})
        self.assertEqual(path.read_bytes(), body)

    def test_successful_zip_download(self):
        body = b"PK\x03\x04synthetic archive bytes"
        path, _, _ = self.perform(body, file=make_file(filename="archive.zip", mime="application/zip", fmt="zip"),
                                  headers={"Content-Type": "application/zip"})
        self.assertEqual(path.read_bytes(), body)

    def test_missing_provider_filename_uses_evidenced_format_extension(self):
        path, _, _ = self.perform(b"%PDF-1.7", file=make_file(filename=None))
        self.assertEqual(path.name, "resource-W123.pdf")

    def test_duplicate_filename_gets_non_overwriting_name(self):
        first, _, _ = self.perform(b"first")
        second, _, _ = self.perform(b"second")
        self.assertEqual(first.name, "paper.pdf")
        self.assertEqual(second.name, "paper (1).pdf")
        self.assertEqual(first.read_bytes(), b"first")
        self.assertEqual(second.read_bytes(), b"second")

    def test_untrusted_filename_is_sanitized_and_confined(self):
        target, _, _ = self.perform(b"safe", file=make_file(filename="../../../../outside.txt"))
        self.assertEqual(target.parent.resolve(), (self.directory / "papers").resolve())
        self.assertNotIn("..", target.name)
        self.assertTrue(target.is_file())

    def test_windows_absolute_unc_and_parent_path_filenames_stay_in_download_directory(self):
        for filename in (r"C:\Users\Public\outside.csv", r"\\server\share\outside.zip",
                         "../../outside.pdf", "/etc/passwd"):
            with self.subTest(filename=filename):
                target, _, _ = self.perform(b"safe", file=make_file(filename=filename))
                self.assertEqual(target.parent.resolve(), (self.directory / "papers").resolve())
                self.assertTrue(target.resolve().is_relative_to((self.directory / "papers").resolve()))

    def test_resource_type_creates_type_subdirectory(self):
        selected = make_file(filename="table.csv", mime="text/csv", fmt="csv")
        dataset = make_resource((selected,), kind=ResourceType.DATASET)
        path, _, _ = self.perform(b"x,y\n", file=selected, resource=dataset,
                                  headers={"Content-Type": "text/csv"})
        self.assertIn("datasets", path.parts)

    def test_401_and_403_are_access_restrictions(self):
        for status in (401, 403):
            with self.subTest(status=status), self.assertRaises(DownloadError):
                self.perform(b"restricted", status=status)
            self.assertFalse(list(self.directory.rglob("*.part")))

    def test_http_failures_404_429_500_raise_and_leave_no_partial_file(self):
        for status in (404, 429, 500):
            with self.subTest(status=status), self.assertRaises(DownloadError):
                self.perform(b"error", status=status)
            self.assertFalse(list(self.directory.rglob("*.part")))

    def test_unknown_access_state_is_attempted_but_explicit_false_is_refused(self):
        unknown = make_file(open_access=None)
        path, _, _ = self.perform(b"content", file=unknown)
        self.assertTrue(path.exists())
        restricted = make_file(open_access=False)
        with self.assertRaises(DownloadError):
            self.perform(b"must not request", file=restricted)

    def test_missing_download_url_is_rejected(self):
        with self.assertRaises(DownloadError):
            download_file(make_file(url=""), make_resource(), download_dir=self.directory,
                          timeout=2, max_bytes=100)

    def test_malformed_and_non_http_urls_are_rejected(self):
        for url in ("file:///etc/passwd", "ftp://example.test/data", "https://[broken", "https:///missing-host"):
            with self.subTest(url=url), self.assertRaises(DownloadError):
                download_file(make_file(url=url), make_resource(), download_dir=self.directory,
                              timeout=2, max_bytes=100)

    def test_loopback_and_private_literal_addresses_are_rejected(self):
        for address in ("127.0.0.1", "::1", "10.0.0.1", "192.168.1.4", "169.254.1.1"):
            with self.subTest(address=address), self.assertRaises(DownloadError):
                manager._public_url(f"http://{address}/file")

    def test_dns_resolution_to_private_address_is_rejected(self):
        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))]
        with patch.object(manager.socket, "getaddrinfo", return_value=info):
            with self.assertRaises(DownloadError):
                manager._public_url("https://research-files.example.test/a.pdf")

    def test_pinned_connect_rejects_mixed_public_and_private_dns_before_socket_creation(self):
        info = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443)),
        ]
        with patch.object(manager.socket, "getaddrinfo", return_value=info), \
                patch.object(manager.socket, "socket") as socket_factory:
            with self.assertRaises(DownloadError):
                manager._connect_pinned("http", 443, "research-files.example.test", 2)
        socket_factory.assert_not_called()

    def test_pinned_connect_uses_vetted_ip_directly(self):
        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        connection_socket = Mock()
        with patch.object(manager.socket, "getaddrinfo", return_value=info), \
                patch.object(manager.socket, "socket", return_value=connection_socket) as socket_factory:
            result = manager._connect_pinned("http", 443, "research-files.example.test", 2)
        self.assertIs(result, connection_socket)
        socket_factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        connection_socket.connect.assert_called_once_with(("8.8.8.8", 443))

    def test_papers_symlink_cannot_escape_download_root(self):
        root = self.directory
        root.mkdir(parents=True)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        papers = root / "papers"
        try:
            papers.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable on this platform: {exc}")
        with self.assertRaises(DownloadError):
            download_file(make_file(), make_resource(), download_dir=root,
                          timeout=2, max_bytes=100)

    def test_resolved_destination_escape_is_rejected_without_symlink_privilege(self):
        root = self.directory.resolve()
        outside = Path(self.temp.name).parent / "outside-downloads"
        original_resolve = Path.resolve

        def resolve_with_escaped_papers(path, *args, **kwargs):
            if path == root / "papers":
                return outside
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve_with_escaped_papers):
            with self.assertRaises(DownloadError):
                download_file(make_file(), make_resource(), download_dir=root,
                              timeout=2, max_bytes=100)

    def test_redirect_to_loopback_is_rejected_before_second_request(self):
        response = FakeResponse(b"", status=302, headers={"Location": "http://127.0.0.1/secret"})
        request = Mock(return_value=(self.connection, response))
        with patch.object(manager, "_request", request):
            with self.assertRaises(DownloadError):
                download_file(make_file(), make_resource(), download_dir=self.directory,
                              timeout=2, max_bytes=100)
        request.assert_called_once()

    def test_redirect_location_without_valid_scheme_is_rejected(self):
        response = FakeResponse(b"", status=302, headers={"Location": "file:///secret"})
        with patch.object(manager, "_request", return_value=(self.connection, response)):
            with self.assertRaises(DownloadError):
                download_file(make_file(), make_resource(), download_dir=self.directory,
                              timeout=2, max_bytes=100)

    def test_size_limit_is_enforced_for_header_and_streamed_bytes(self):
        with self.assertRaises(DownloadError):
            self.perform(b"12345", max_bytes=4, headers={"Content-Length": "5"})
        with self.assertRaises(DownloadError):
            self.perform(b"12345", max_bytes=4, headers={})
        self.assertEqual(list(self.directory.rglob("*.pdf")), [])

    def test_content_length_mismatch_is_rejected(self):
        with self.assertRaises(DownloadError):
            self.perform(b"short", headers={"Content-Length": "99"})

    def test_html_error_page_is_never_saved_as_pdf(self):
        with self.assertRaises(DownloadError):
            self.perform(b"<!doctype html><title>Denied</title>",
                         headers={"Content-Type": "text/html"})
        self.assertEqual(list(self.directory.rglob("*.pdf")), [])

    def test_incompatible_content_type_is_rejected(self):
        with self.assertRaises(DownloadError):
            self.perform(b"plain text", headers={"Content-Type": "text/plain"})

    def test_network_failure_cleans_temporary_file(self):
        response = FakeResponse(b"partial", chunks=[b"partial"])
        request = Mock(return_value=(self.connection, response))
        with patch.object(manager, "_public_url", return_value=("https", 443, "files.example.test", "/research/file")), \
                patch.object(manager, "_request", request), \
                patch.object(response, "read", side_effect=[b"partial", TimeoutError("socket timed out")]):
            with self.assertRaises(DownloadError):
                download_file(make_file(), make_resource(), download_dir=self.directory,
                              timeout=0.01, max_bytes=100)
        self.assertFalse([path for path in self.directory.rglob("*") if path.is_file()])


class DownloadCliTests(unittest.TestCase):
    def setUp(self):
        self.files = (make_file(filename="first.pdf"),
                      make_file(filename="second.pdf", url="https://files.example.test/second"))
        self.resource = make_resource(self.files)

    def invoke(self, arguments):
        return main(["download", *arguments])

    def test_invalid_search_id_is_clear_usage_error(self):
        errors = io.StringIO()
        with patch("app.cli.main._resolve_search", side_effect=ValueError("saved search ID 999 was not found")), \
                contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            self.invoke(["999", "1"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("saved search ID 999 was not found", errors.getvalue())

    def test_invalid_resource_number_is_clear_usage_error(self):
        errors = io.StringIO()
        with patch("app.cli.main._resolve_search", return_value=("topic", [self.resource], {})), \
                contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            self.invoke(["3", "2"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("result number", errors.getvalue())

    def test_multiple_file_selection_downloads_only_selected_file(self):
        output = io.StringIO()
        with patch("app.cli.main._resolve_search", return_value=("topic", [self.resource], {})), \
                patch("app.cli.main.download_file", return_value=Path("saved/second.pdf")) as download, \
                patch.object(Path, "stat", return_value=Mock(st_size=20)), \
                contextlib.redirect_stdout(output):
            self.assertEqual(self.invoke(["3", "1", "--file", "2"]), 0)
        download.assert_called_once_with(self.files[1], self.resource, download_dir="data/downloads",
                                         timeout=15.0, max_bytes=100 * 1024 * 1024)
        self.assertIn("Saved to: saved\\second.pdf", output.getvalue())

    def test_missing_explicit_file_selection_does_not_download(self):
        output = io.StringIO()
        with patch("app.cli.main._resolve_search", return_value=("topic", [self.resource], {})), \
                patch("app.cli.main.download_file") as download, contextlib.redirect_stdout(output):
            self.assertEqual(self.invoke(["3", "1"]), 0)
        download.assert_not_called()
        self.assertIn("Select a file explicitly", output.getvalue())

    def test_missing_download_links_reports_landing_page(self):
        resource = make_resource(())
        output = io.StringIO()
        with patch("app.cli.main._resolve_search", return_value=("topic", [resource], {})), \
                contextlib.redirect_stdout(output):
            self.assertEqual(self.invoke(["3", "1"]), 1)
        self.assertIn("No provider-supplied downloadable files", output.getvalue())
        self.assertIn("https://example.org/study", output.getvalue())

    def test_invalid_file_number_is_clear_usage_error(self):
        errors = io.StringIO()
        with patch("app.cli.main._resolve_search", return_value=("topic", [self.resource], {})), \
                contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            self.invoke(["3", "1", "--file", "3"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("file number", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
