"""HTTP service boundary tests: auth, isolation, idempotency, limits, streaming."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from typing import Any
from unittest import mock

import httpx

import service.config as service_config
from agent_platform import SQLiteCheckpointRepository, SQLiteRunRepository
from agent_platform.compiler import CompiledAgent
from agent_platform.models import ModelSpec
from agent_platform.spec import AgentSpec, PromptSpec, ToolReference
from service.api import API_PREFIX
from service.app import create_server
from service.config import ServiceConfigurationError, ServiceSettings, Tenant

TENANT_A = Tenant("tenant-a", "a" * 16)
TENANT_B = Tenant("tenant-b", "b" * 16)


class FakeExecutable:
    """Minimal invoke/stream compatible agent used by the service tests."""

    def __init__(self, content: str = "合肥当前天气：晴朗，20℃。") -> None:
        self.content = content

    def invoke(self, payload: Any, *, config: Any = None) -> dict[str, Any]:
        return {"messages": [{"content": self.content}]}

    def stream(self, payload: Any, *, config: Any = None, stream_mode: Any = None):
        yield {"messages": [{"content": "working"}]}
        yield {"messages": [{"content": self.content}]}


def _compiled_agent() -> CompiledAgent:
    spec = AgentSpec(
        agent_id="service-agent",
        version=1,
        goal="test service boundary",
        model=ModelSpec(provider="openai-compatible", model="test-model"),
        prompt=PromptSpec(prompt_id="service", version=1, system="test"),
        tools=(ToolReference("lookup-weather"),),
        runtime_type="langgraph",
        timeout_seconds=5.0,
    )
    return CompiledAgent(spec=spec, executable=FakeExecutable())


class ServiceAPITestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        directory = Path(self._temporary.name)
        self.audit_path = directory / "audit.jsonl"
        self.db_path = str(directory / "runs.sqlite3")
        self._start_server(rate_limit_per_minute=1000)
        self.addCleanup(self._stop_server)

    # --- server helpers ----------------------------------------------------

    def _settings(self, *, rate_limit_per_minute: int) -> ServiceSettings:
        return ServiceSettings(
            tenants=(TENANT_A, TENANT_B),
            host="127.0.0.1",
            port=0,
            rate_limit_per_minute=rate_limit_per_minute,
            max_body_bytes=512,
            request_timeout_seconds=30.0,
            audit_path=str(self.audit_path),
            checkpoint_db_path=self.db_path,
        )

    def _start_server(self, *, rate_limit_per_minute: int) -> None:
        self.run_repository = SQLiteRunRepository(self.db_path)
        self.checkpoint_repository = SQLiteCheckpointRepository(self.db_path)
        self.server = create_server(
            self._settings(rate_limit_per_minute=rate_limit_per_minute),
            agent_provider=_compiled_agent,
            run_repository=self.run_repository,
            checkpoint_repository=self.checkpoint_repository,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = httpx.Client(base_url=self.base_url, timeout=10.0)

    def _stop_server(self) -> None:
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.run_repository.close()
        self.checkpoint_repository.close()

    def _restart(self, *, rate_limit_per_minute: int) -> None:
        self._stop_server()
        self._start_server(rate_limit_per_minute=rate_limit_per_minute)

    @staticmethod
    def _headers(key: str = TENANT_A.api_key, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", **extra}

    def _create_run(self, question: str = "合肥天气", **extra: str) -> httpx.Response:
        return self.client.post(
            f"{API_PREFIX}/runs",
            json={"question": question},
            headers=self._headers(**extra),
        )

    # --- tests -------------------------------------------------------------

    def test_health_is_available_without_authentication(self) -> None:
        response = self.client.get(f"{API_PREFIX}/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_missing_and_invalid_keys_are_rejected(self) -> None:
        missing = self.client.post(
            f"{API_PREFIX}/runs",
            json={"question": "合肥天气"},
            headers={"Content-Type": "application/json"},
        )
        invalid = self.client.post(
            f"{API_PREFIX}/runs",
            json={"question": "合肥天气"},
            headers=self._headers("wrong-key-wrong-key"),
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["error"]["code"], "unauthorized")
        self.assertEqual(invalid.status_code, 401)
        self.assertIn("request_id", invalid.json()["error"])

    def test_create_run_returns_completed_run(self) -> None:
        response = self._create_run()

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["content"], "合肥当前天气：晴朗，20℃。")
        self.assertTrue(payload["thread_id"].startswith("tenant-a:"))
        self.assertEqual(payload["runtime_type"], "langgraph")

    def test_tenants_cannot_read_each_others_runs(self) -> None:
        created = self._create_run().json()

        other = self.client.get(
            f"{API_PREFIX}/runs/{created['run_id']}",
            headers=self._headers(TENANT_B.api_key),
        )
        owner = self.client.get(
            f"{API_PREFIX}/runs/{created['run_id']}",
            headers=self._headers(),
        )

        self.assertEqual(other.status_code, 404)
        self.assertEqual(owner.status_code, 200)

    def test_tenants_do_not_see_each_others_threads(self) -> None:
        self._create_run()

        threads = self.client.get(
            f"{API_PREFIX}/threads",
            headers=self._headers(TENANT_B.api_key),
        ).json()["threads"]

        self.assertEqual(threads, [])

    def test_idempotency_key_reuses_the_same_run(self) -> None:
        first = self._create_run(**{"Idempotency-Key": "abc-123"}).json()
        second = self._create_run(**{"Idempotency-Key": "abc-123"}).json()

        self.assertEqual(first["run_id"], second["run_id"])
        stored = self.run_repository.list(session_id=first["thread_id"])
        self.assertEqual(len(stored), 1)

    def test_rate_limit_returns_429_with_retry_after(self) -> None:
        self._restart(rate_limit_per_minute=1)

        first = self._create_run()
        second = self._create_run()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"]["code"], "rate_limited")
        self.assertIn("Retry-After", second.headers)

    def test_streaming_returns_server_sent_events(self) -> None:
        with self.client.stream(
            "POST",
            f"{API_PREFIX}/runs",
            json={"question": "合肥天气"},
            headers=self._headers(Accept="text/event-stream"),
        ) as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        events = [
            json.loads(line[6:])
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        event_types = [event["type"] for event in events]
        self.assertEqual(event_types[0], "stream.opened")
        self.assertIn("run.completed", event_types)
        self.assertEqual(event_types[-1], "stream.closed")

    def test_validation_errors_are_stable(self) -> None:
        empty = self.client.post(
            f"{API_PREFIX}/runs",
            json={"question": ""},
            headers=self._headers(),
        )
        too_big = self.client.post(
            f"{API_PREFIX}/runs",
            json={"question": "a" * 600},
            headers=self._headers(),
        )
        bad_type = self.client.post(
            f"{API_PREFIX}/runs",
            content=b"question=x",
            headers=self._headers(**{"Content-Type": "text/plain"}),
        )

        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["error"]["code"], "invalid_request")
        self.assertIn("question", empty.json()["error"]["message"])
        self.assertEqual(too_big.status_code, 413)
        self.assertEqual(too_big.json()["error"]["code"], "payload_too_large")
        self.assertEqual(bad_type.status_code, 415)
        self.assertEqual(bad_type.json()["error"]["code"], "unsupported_media_type")

    def test_resume_of_unknown_thread_conflicts(self) -> None:
        response = self.client.post(
            f"{API_PREFIX}/threads/tenant-a:missing/resume",
            json={},
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "conflict")

    def test_foreign_thread_is_not_found(self) -> None:
        response = self.client.get(
            f"{API_PREFIX}/threads/tenant-a:some-thread/checkpoints",
            headers=self._headers(TENANT_B.api_key),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_list_runs_is_scoped_and_filterable(self) -> None:
        self._create_run()
        self._create_run()

        listed = self.client.get(
            f"{API_PREFIX}/runs?limit=1",
            headers=self._headers(),
        ).json()["runs"]

        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]["thread_id"].startswith("tenant-a:"))

    def test_unknown_route_and_method_are_reported(self) -> None:
        unknown = self.client.get(f"{API_PREFIX}/nope", headers=self._headers())
        wrong_method = self.client.delete(f"{API_PREFIX}/runs", headers=self._headers())

        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(wrong_method.status_code, 405)

    def test_audit_trail_records_every_request(self) -> None:
        self._create_run()

        records = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        self.assertTrue(records)
        last = records[-1]
        self.assertEqual(last["tenant"], "tenant-a")
        self.assertEqual(last["outcome"], "ok")
        self.assertEqual(last["path"], f"{API_PREFIX}/runs")
        self.assertIsNotNone(last["run_id"])

    def test_audit_is_written_before_the_response_is_observable(self) -> None:
        """Regression: auditing after the response lost records on crash."""
        response = self._create_run()

        request_id = response.headers["X-Request-Id"]
        # Deliberately no sleep: the record must already be on disk because it
        # is written before the response is emitted.
        records = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matching = [
            record for record in records if record.get("request_id") == request_id
        ]

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["outcome"], "ok")
        self.assertEqual(matching[0]["run_id"], response.json()["run_id"])

    def test_kept_alive_connection_survives_an_error_response(self) -> None:
        """Regression: an unread body desynchronised the next request (400)."""
        unauthenticated = self.client.post(
            f"{API_PREFIX}/runs",
            json={"question": "合肥天气"},
            headers={"Content-Type": "application/json"},
        )
        # Same client and no explicit close, so the socket is reused here.
        authenticated = self._create_run()

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertNotEqual(
            unauthenticated.headers.get("Connection", "").lower(),
            "close",
        )
        self.assertEqual(authenticated.status_code, 201)

    def test_oversized_body_is_rejected_and_next_request_still_works(self) -> None:
        """An oversized body cannot be drained, so the connection is closed."""
        oversized = self.client.post(
            f"{API_PREFIX}/runs",
            json={"question": "a" * 600},
            headers=self._headers(),
        )
        following = self._create_run()

        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(
            oversized.headers.get("Connection", "").lower(),
            "close",
        )
        self.assertEqual(following.status_code, 201)


class ServiceSettingsSafetyTests(unittest.TestCase):
    def test_non_loopback_binding_requires_explicit_opt_in(self) -> None:
        with self.assertRaises(ServiceConfigurationError):
            ServiceSettings(tenants=(TENANT_A,), host="0.0.0.0")

    def test_public_exposure_can_be_enabled_explicitly(self) -> None:
        settings = ServiceSettings(
            tenants=(TENANT_A,),
            host="0.0.0.0",
            allow_public_exposure=True,
        )

        self.assertEqual(settings.host, "0.0.0.0")

    def test_at_least_one_tenant_is_required(self) -> None:
        with self.assertRaises(ServiceConfigurationError):
            ServiceSettings(tenants=())

    def test_short_api_keys_are_rejected(self) -> None:
        with self.assertRaises(ServiceConfigurationError):
            Tenant("tenant-a", "short")


class InCodeApiKeyTests(unittest.TestCase):
    """The explicit in-code switch must not weaken the fail-closed default."""

    def _resolve(
        self,
        *,
        environment: dict[str, str],
        in_code: str,
    ) -> ServiceSettings:
        """Resolve settings hermetically, without reading the real .env file."""
        with mock.patch.object(service_config, "load_dotenv", lambda *a, **k: None):
            with mock.patch.dict(os.environ, environment, clear=False):
                if "SERVICE_API_KEYS" not in environment:
                    os.environ.pop("SERVICE_API_KEYS", None)
                with mock.patch.object(
                    service_config,
                    "IN_CODE_API_KEYS",
                    in_code,
                ):
                    return ServiceSettings.from_environment()

    def test_in_code_keys_start_the_service_without_environment(self) -> None:
        settings = self._resolve(environment={}, in_code="team-a:" + "a" * 16)

        self.assertEqual(settings.tenants, (Tenant("team-a", "a" * 16),))

    def test_environment_keys_win_over_in_code_keys(self) -> None:
        settings = self._resolve(
            environment={"SERVICE_API_KEYS": "team-b:" + "b" * 16},
            in_code="team-a:" + "a" * 16,
        )

        self.assertEqual(settings.tenants, (Tenant("team-b", "b" * 16),))

    def test_in_code_keys_still_face_the_same_validation(self) -> None:
        with self.assertRaises(ServiceConfigurationError):
            self._resolve(environment={}, in_code="team-a:short")

    def test_missing_keys_everywhere_still_fail_closed(self) -> None:
        with self.assertRaises(ServiceConfigurationError):
            self._resolve(environment={}, in_code="")


if __name__ == "__main__":
    unittest.main()
