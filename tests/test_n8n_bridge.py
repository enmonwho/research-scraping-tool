"""Tests for the optional authenticated local n8n bridge."""

from http.client import HTTPConnection
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.integrations.n8n.server import create_server

TOKEN = "test-token-with-at-least-24-characters"


class N8nBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "research.db"
        self.server = create_server(port=0, token=TOKEN, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.registry.close()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, *, body: object = None,
                token: str | None = TOKEN) -> tuple[int, dict, dict[str, str]]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, payload, response_headers

    def test_requires_valid_bearer_token_for_both_routes(self) -> None:
        self.assertEqual(self.request("GET", "/jobs/" + "a" * 32, token=None)[0], 401)
        self.assertEqual(self.request("GET", "/jobs/" + "a" * 32, token="wrong-token-value-123456789")[0], 401)
        self.assertEqual(self.request("POST", "/jobs", body={"topic": "x"}, token=None)[0], 401)

    def test_submit_and_poll_job_with_stubbed_search(self) -> None:
        with patch("app.core.search_service.search_papers", create=True):
            from app.core import search_service
            from app.models import ResearchResource, ResourceType
            from datetime import datetime, timezone

            resource = ResearchResource(ResourceType.PAPER, "Test Paper", None, None, 2024,
                                        "OpenAlex", "W1", None, None, datetime.now(timezone.utc))
            with patch.object(search_service, "search_openalex", return_value=[resource]):
                status, payload, headers = self.request("POST", "/jobs", body={"topic": "test topic"})
                self.assertEqual(status, 202)
                self.assertEqual(headers["Location"], payload["status_url"])
                job_id = payload["job_id"]
                for _ in range(100):
                    status, job, _ = self.request("GET", payload["status_url"])
                    if job["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
            self.assertEqual(status, 200)
            self.assertEqual(job["status"], "succeeded", job)
            self.assertEqual(job["result"]["topic"], "test topic")
            self.assertEqual(job["result"]["results"][0]["title"], "Test Paper")
            self.assertEqual(job["result"]["search_id"], 1)
            self.assertEqual(self.request("GET", f"/jobs/{job_id}")[1], job)

    def test_invalid_request_fields_return_400_and_unknown_route_404(self) -> None:
        for body in ({}, {"topic": " "}, {"topic": "valid", "limit": "many"},
                     {"topic": "valid", "extra": True}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/jobs", body=body)[0], 400)
        self.assertEqual(self.request("GET", "/missing")[0], 404)

    def test_rejects_non_loopback_host_and_short_token(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_server(host="0.0.0.0", port=8765, token=TOKEN, db_path=self.db_path)
        with self.assertRaisesRegex(ValueError, "24 characters"):
            create_server(port=8765, token="short", db_path=self.db_path)


if __name__ == "__main__":
    unittest.main()
