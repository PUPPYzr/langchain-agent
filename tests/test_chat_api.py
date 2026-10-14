"""End-to-end tests for the Web chat boundary.

The Agent under test is a real ``create_agent`` graph with a scripted model and
a real tool, streamed through ``service``. Nothing is mocked at the HTTP layer:
the tests exercise the same code path the browser uses.
"""

import json
import tempfile
import unittest
from pathlib import Path
from threading import Thread

import httpx
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool as make_tool

from agent_platform import SQLiteCheckpointRepository, SQLiteRunRepository
from agent_platform.compiler import CompiledAgent, LangChainAgentCompiler
from agent_platform.models import ModelSpec
from agent_platform.spec import AgentSpec, PromptSpec, ToolReference
from agent_platform.tools import ToolRegistry, ToolSpec
from service.api import API_PREFIX
from service.app import create_server
from service.config import ServiceSettings, Tenant

TENANT_A = Tenant("tenant-a", "a" * 16)
TENANT_B = Tenant("tenant-b", "b" * 16)
ANSWER = "合肥当前天气：晴朗，20℃。"


def _tool_call(call_id: str, location: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "lookup_weather",
                "args": {"location": location},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


class BindableFakeMessagesListChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _build_compiled_agent(script: list, calls: list[str]) -> CompiledAgent:
    @make_tool
    def lookup_weather(location: str) -> str:
        """Return a stub weather payload for a location."""
        calls.append(location)
        return json.dumps(
            {
                "status": "success",
                "error_type": None,
                "message": None,
                "retryable": False,
                "data": {"requested_location": location, "weather_description": "晴朗"},
            },
            ensure_ascii=False,
        )

    registry = ToolRegistry(
        [
            ToolSpec(
                tool_id="lookup-weather",
                version=1,
                description="stub weather lookup",
                implementation=lookup_weather,
                input_schema=dict,
                output_schema=dict,
            )
        ]
    )
    spec = AgentSpec(
        agent_id="chat-agent",
        version=1,
        goal="chat boundary test",
        model=ModelSpec(provider="openai-compatible", model="test-model"),
        prompt=PromptSpec(prompt_id="chat", version=1, system="test"),
        tools=(ToolReference("lookup-weather"),),
        runtime_type="langgraph",
        timeout_seconds=30.0,
    )
    model = BindableFakeMessagesListChatModel(responses=script)

    class FakeModelProvider:
        def create_chat_model(self, spec):
            return model

    return LangChainAgentCompiler(
        model_provider=FakeModelProvider(),
        tool_provider=registry,
    ).compile(spec)


class ChatAPITestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        directory = Path(self._temporary.name)
        self.db_path = str(directory / "runs.sqlite3")
        self.web_directory = directory / "web"
        self.web_directory.mkdir()
        (self.web_directory / "index.html").write_text(
            "<!doctype html><h1>chat ui</h1>",
            encoding="utf-8",
        )
        self.calls: list[str] = []
        self._compile_agent([
            _tool_call("call-1", "合肥"),
            AIMessage(content=ANSWER),
            AIMessage(content=ANSWER),
            AIMessage(content=ANSWER),
        ])
        self._start_server()
        self.addCleanup(self._stop_server)

    def _compile_agent(self, script: list) -> None:
        self.compiled = _build_compiled_agent(script, self.calls)

    def _start_server(self) -> None:
        self.run_repository = SQLiteRunRepository(self.db_path)
        self.checkpoint_repository = SQLiteCheckpointRepository(self.db_path)
        settings = ServiceSettings(
            tenants=(TENANT_A, TENANT_B),
            host="127.0.0.1",
            port=0,
            rate_limit_per_minute=1000,
            max_body_bytes=20000,
            request_timeout_seconds=30.0,
            audit_path=str(Path(self._temporary.name) / "audit.jsonl"),
            checkpoint_db_path=self.db_path,
        )
        self.server = create_server(
            settings,
            agent_provider=lambda: self.compiled,
            run_repository=self.run_repository,
            checkpoint_repository=self.checkpoint_repository,
            web_directory=self.web_directory,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = httpx.Client(base_url=self.base_url, timeout=15.0)

    def _stop_server(self) -> None:
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.run_repository.close()
        self.checkpoint_repository.close()

    @staticmethod
    def _headers(key: str = TENANT_A.api_key, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", **extra}

    def _create_session(self) -> str:
        response = self.client.post(
            f"{API_PREFIX}/sessions",
            json={},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["session_id"]

    def _chat(self, session_id: str, message: str = "合肥天气") -> list[dict]:
        with self.client.stream(
            "POST",
            f"{API_PREFIX}/chat",
            json={"session_id": session_id, "message": message},
            headers=self._headers(Accept="text/event-stream"),
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(response.iter_text())
        return [
            json.loads(line[6:])
            for line in body.splitlines()
            if line.startswith("data: ")
        ]

    # --- static UI ---------------------------------------------------------

    def test_web_ui_is_served_without_an_api_key(self) -> None:
        page = self.client.get("/")
        asset = self.client.get("/index.html")

        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.assertIn("chat ui", page.text)
        self.assertEqual(asset.status_code, 200)

    def test_unknown_static_resource_is_not_found(self) -> None:
        missing = self.client.get("/does-not-exist.js")
        traversal = self.client.get("/%2e%2e%2fagent_platform%2fdomain.py")

        self.assertEqual(missing.status_code, 404)
        self.assertEqual(traversal.status_code, 404)

    def test_health_reports_context_mode(self) -> None:
        body = self.client.get(f"{API_PREFIX}/health").json()

        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["context_mode"], "checkpointed")

    # --- session isolation -------------------------------------------------

    def test_session_endpoints_require_authentication(self) -> None:
        response = self.client.get(f"{API_PREFIX}/sessions")

        self.assertEqual(response.status_code, 401)

    def test_sessions_are_isolated_between_tenants(self) -> None:
        session_id = self._create_session()

        as_other_tenant = self.client.get(
            f"{API_PREFIX}/sessions/{session_id}/messages",
            headers=self._headers(TENANT_B.api_key),
        )
        as_owner = self.client.get(
            f"{API_PREFIX}/sessions/{session_id}/messages",
            headers=self._headers(),
        )

        self.assertEqual(as_other_tenant.status_code, 404)
        self.assertEqual(as_owner.status_code, 200)
        self.assertEqual(
            self.client.get(
                f"{API_PREFIX}/sessions", headers=self._headers(TENANT_B.api_key)
            ).json()["sessions"],
            [],
        )

    # --- chat flow ---------------------------------------------------------

    def test_chat_streams_real_tool_execution_and_answer(self) -> None:
        session_id = self._create_session()

        frames = self._chat(session_id)
        frame_types = [frame["type"] for frame in frames]

        # Every frame shares one envelope; the chat events sit inside it.
        self.assertEqual(frame_types[0], "stream.opened")
        self.assertEqual(frame_types[-1], "stream.closed")
        events = [
            frame
            for frame in frames
            if frame["type"] not in {"stream.opened", "stream.closed"}
        ]
        types = [event["type"] for event in events]

        self.assertEqual(events[0]["type"], "started")
        self.assertIn("tool_call", types)
        self.assertIn("tool_result", types)
        self.assertIn("answer", types)
        self.assertEqual(events[-1]["type"], "done")

        tool_call = next(event for event in events if event["type"] == "tool_call")
        self.assertEqual(tool_call["name"], "lookup_weather")
        self.assertEqual(tool_call["arguments"], {"location": "合肥"})

        tool_result = next(event for event in events if event["type"] == "tool_result")
        self.assertTrue(tool_result["ok"])
        self.assertIn("晴朗", tool_result["summary"])

        answer = next(event for event in events if event["type"] == "answer")
        self.assertEqual(answer["content"], ANSWER)
        # The tool really ran through the Agent, not a stub.
        self.assertEqual(self.calls, ["合肥"])

    def test_chat_persists_the_transcript(self) -> None:
        session_id = self._create_session()

        self._chat(session_id)
        messages = self.client.get(
            f"{API_PREFIX}/sessions/{session_id}/messages",
            headers=self._headers(),
        ).json()["messages"]

        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "合肥天气")
        self.assertEqual(messages[1]["content"], ANSWER)

    def test_session_title_is_derived_from_the_first_message(self) -> None:
        session_id = self._create_session()

        self._chat(session_id, "北京未来三天天气怎么样？")
        sessions = self.client.get(
            f"{API_PREFIX}/sessions", headers=self._headers()
        ).json()["sessions"]

        self.assertEqual(sessions[0]["title"], "北京未来三天天气怎么样？")

    def test_chat_rejects_unknown_session(self) -> None:
        with self.client.stream(
            "POST",
            f"{API_PREFIX}/chat",
            json={"session_id": "missing", "message": "hi"},
            headers=self._headers(Accept="text/event-stream"),
        ) as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 404)
        self.assertIn("not_found", body)

    def test_chat_requires_a_non_empty_message(self) -> None:
        session_id = self._create_session()

        response = self.client.post(
            f"{API_PREFIX}/chat",
            json={"session_id": session_id, "message": ""},
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    # --- deletion ----------------------------------------------------------

    def test_deleting_a_session_removes_its_transcript(self) -> None:
        session_id = self._create_session()
        self._chat(session_id)

        deleted = self.client.request(
            "DELETE",
            f"{API_PREFIX}/sessions/{session_id}",
            headers=self._headers(),
        )
        after = self.client.get(
            f"{API_PREFIX}/sessions/{session_id}/messages",
            headers=self._headers(),
        )

        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(after.status_code, 404)

    # --- cancellation ------------------------------------------------------

    def test_cancelling_a_streaming_run_stops_it(self) -> None:
        session_id = self._create_session()
        service = self.server.service

        run_id, events = service.chat_stream(
            tenant_id="tenant-a",
            session_id=session_id,
            message="合肥天气",
        )
        service.cancel_run(tenant_id="tenant-a", run_id=run_id)
        payloads = list(events)

        self.assertTrue(
            any(
                item["type"] == "error" and item["code"] == "run_cancelled"
                for item in payloads
            ),
            payloads,
        )
        self.assertEqual(
            self.run_repository.get(run_id).status,
            "cancelled",
        )
        self.assertEqual(self.calls, [])

    def test_cancelling_an_unknown_run_is_not_found(self) -> None:
        response = self.client.post(
            f"{API_PREFIX}/runs/does-not-exist/cancel",
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 404)

    def test_cancelling_a_finished_run_conflicts(self) -> None:
        session_id = self._create_session()
        done = self._chat(session_id)
        run_id = next(e for e in done if e["type"] == "started")["run_id"]

        response = self.client.post(
            f"{API_PREFIX}/runs/{run_id}/cancel",
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "conflict")


if __name__ == "__main__":
    unittest.main()
