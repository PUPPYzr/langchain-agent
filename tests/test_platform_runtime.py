import unittest
from unittest.mock import patch

from agent_platform import (
    AgentApplicationService,
    AgentContext,
    InMemoryEventSink,
    InMemoryLangGraphCheckpointProvider,
    LangChainAgentCompiler,
    ResumeNotAllowedError,
    RuntimeRegistry,
    SQLiteCheckpointProvider,
)
from agent_platform.events import AgentEvent, CallbackEventSink
from agent_platform.domain import AgentRuntimeError
from agent_platform.langgraph_runtime import LangGraphAgentRuntime
from agent_platform.models import ModelSpec
from agent_platform.spec import AgentSpec, PromptSpec, ToolReference
from agent_platform.tools import ToolRegistry, ToolSpec
from agent_platform.runs import InMemoryRunRepository, SQLiteRunRepository, RunRecord
from agent_platform.compiler import CompiledAgent
from agent_platform.state import LangGraphAgentState
from agent_platform.domain import AgentRunResult, AgentRunTimeoutError, AgentRunCancelledError


class FakeExecutable:
    def __init__(self) -> None:
        self.received_config = None
        self.received_payload = None

    def invoke(self, payload, *, config=None):
        self.received_payload = payload
        self.received_config = config
        return {"messages": [{"content": "done"}]}

    def stream(self, payload, *, config=None, stream_mode=None):
        self.received_config = config
        yield {"messages": [{"content": "working"}]}
        yield {"messages": [{"content": "done"}]}


class PlatformRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = AgentSpec(
            agent_id="test-agent",
            version=1,
            goal="test",
            model=ModelSpec(provider="openai-compatible", model="test-model"),
            prompt=PromptSpec(prompt_id="test", version=1, system="test"),
            tools=(ToolReference("test-tool"),),
            runtime_type="langgraph",
        )
        self.context = AgentContext(
            question="hello",
            run_id="run-1",
            session_id="thread-1",
        )

    def test_spec_projects_to_runtime_definition(self) -> None:
        definition = self.spec.to_definition()

        self.assertEqual(definition.runtime_type, "langgraph")
        self.assertEqual(definition.tool_ids, ("test-tool",))
        self.assertEqual(definition.model_name, "test-model")

    def test_default_registry_resolves_both_runtimes(self) -> None:
        registry = RuntimeRegistry.with_defaults()

        self.assertEqual(
            registry.create("legacy", FakeExecutable).runtime_type,
            "legacy",
        )
        self.assertEqual(
            registry.create("langgraph", FakeExecutable).runtime_type,
            "langgraph",
        )

    def test_langgraph_runtime_adds_thread_id_and_emits_events(self) -> None:
        executable = FakeExecutable()
        sink = InMemoryEventSink()
        context = AgentContext(
            question=self.context.question,
            run_id=self.context.run_id,
            session_id=self.context.session_id,
            metadata={"event_sink": sink},
        )

        result = LangGraphAgentRuntime(lambda: executable).run(
            self.spec.to_definition(),
            context,
        )

        self.assertEqual(result.content, "done")
        self.assertEqual(
            executable.received_config["configurable"]["thread_id"],
            "thread-1",
        )
        self.assertEqual(
            [event.event_type for event in sink.events],
            ["run.started", "run.completed"],
        )

    def test_langgraph_runtime_streams_normalized_events(self) -> None:
        events = list(
            LangGraphAgentRuntime(FakeExecutable).stream(
                self.spec.to_definition(),
                self.context,
            )
        )

        self.assertEqual(
            [event.event_type for event in events],
            ["run.started", "state.updated", "state.updated", "run.completed"],
        )
        self.assertEqual(events[-1].data["content"], "done")

    def test_runtime_rejects_mismatched_definition(self) -> None:
        invalid = self.spec.to_definition()
        invalid = type(invalid)(
            agent_id=invalid.agent_id,
            version=invalid.version,
            goal=invalid.goal,
            runtime_type="legacy",
        )

        with self.assertRaises(AgentRuntimeError):
            LangGraphAgentRuntime(FakeExecutable).run(invalid, self.context)

    def test_compiler_uses_spec_references_and_checkpoint_provider(self) -> None:
        implementation = object()
        registry = ToolRegistry(
            [
                ToolSpec(
                    tool_id="test-tool",
                    version=1,
                    description="test",
                    implementation=implementation,
                    input_schema=dict,
                    output_schema=dict,
                )
            ]
        )

        class FakeModelProvider:
            def create_chat_model(self, spec):
                return "model"

        checkpointer = object()

        class FakeCheckpointProvider:
            def create_checkpointer(self):
                return checkpointer

        with patch(
            "agent_platform.compiler.create_agent",
            return_value="compiled",
        ) as create_agent:
            compiled = LangChainAgentCompiler(
                model_provider=FakeModelProvider(),
                tool_provider=registry,
                checkpoint_provider=FakeCheckpointProvider(),
            ).compile(self.spec)

        self.assertEqual(compiled.executable, "compiled")
        self.assertEqual(create_agent.call_args.kwargs["tools"], [implementation])
        self.assertIs(create_agent.call_args.kwargs["checkpointer"], checkpointer)
        self.assertIs(create_agent.call_args.kwargs["state_schema"], LangGraphAgentState)

    def test_in_memory_checkpoint_provider_returns_langgraph_saver(self) -> None:
        saver = InMemoryLangGraphCheckpointProvider().create_checkpointer()

        self.assertEqual(type(saver).__name__, "InMemorySaver")

    def test_sqlite_checkpoint_survives_new_saver_instance(self) -> None:
        import tempfile
        from pathlib import Path
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        class BindableFakeListChatModel(FakeListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "checkpoints.sqlite3")
            provider = SQLiteCheckpointProvider(path)
            saver = provider.create_checkpointer()
            graph = __import__("langchain.agents", fromlist=["create_agent"]).create_agent(
                model=BindableFakeListChatModel(responses=["ok"]),
                tools=[],
                state_schema=LangGraphAgentState,
                checkpointer=saver,
            )
            result = graph.invoke(
                {"messages": [{"role": "user", "content": "hello"}]},
                config={"configurable": {"thread_id": "durable-thread"}},
            )
            self.assertEqual(result["messages"][-1].content, "ok")
            saver.close()

            fresh_saver = provider.create_checkpointer()
            latest = fresh_saver.get_tuple(
                {"configurable": {"thread_id": "durable-thread"}}
            )
            self.assertIsNotNone(latest)
            self.assertEqual(latest.checkpoint["channel_values"]["messages"][-1].content, "ok")
            fresh_graph = __import__("langchain.agents", fromlist=["create_agent"]).create_agent(
                model=BindableFakeListChatModel(responses=["continued"]),
                tools=[],
                state_schema=LangGraphAgentState,
                checkpointer=fresh_saver,
            )
            resumed = fresh_graph.invoke(
                None,
                config={"configurable": {"thread_id": "durable-thread"}},
            )
            self.assertEqual(resumed["messages"][-1].content, "ok")
            fresh_saver.close()

            with provider.repository() as repository:
                records = repository.list("durable-thread")
                self.assertTrue(records)

    def test_sqlite_checkpoint_migrates_legacy_snapshot_without_deleting_it(self) -> None:
        import pickle
        import sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "checkpoints.sqlite3")
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE agent_checkpoints (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    parent_checkpoint_id TEXT,
                    checkpoint_blob BLOB NOT NULL,
                    metadata_blob BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                )
                """
            )
            checkpoint = {
                "v": 1,
                "id": "legacy-checkpoint",
                "ts": "2026-01-01T00:00:00+00:00",
                "channel_values": {"value": "preserved"},
                "channel_versions": {},
                "versions_seen": {},
            }
            connection.execute(
                """
                INSERT INTO agent_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-thread",
                    "",
                    checkpoint["id"],
                    checkpoint["ts"],
                    None,
                    pickle.dumps(checkpoint),
                    pickle.dumps({"source": "legacy"}),
                ),
            )
            connection.commit()
            connection.close()

            provider = SQLiteCheckpointProvider(path)
            with provider.create_checkpointer() as saver:
                restored = saver.get_tuple(
                    {"configurable": {"thread_id": "legacy-thread"}}
                )
            self.assertEqual(
                restored.checkpoint["channel_values"]["value"],
                "preserved",
            )

            with provider.repository() as repository:
                record = repository.get("legacy-thread", "legacy-checkpoint")
                self.assertEqual(record.metadata["source"], "legacy")
                AgentApplicationService(
                    checkpoint_repository=repository,
                ).delete_checkpoints("legacy-thread")
                self.assertEqual(repository.list("legacy-thread"), ())

            connection = sqlite3.connect(path)
            legacy_count = connection.execute(
                "SELECT COUNT(*) FROM agent_checkpoints WHERE thread_id = ?",
                ("legacy-thread",),
            ).fetchone()[0]
            connection.close()
            self.assertEqual(legacy_count, 0)

    def test_langgraph_interrupt_resumes_after_restart_with_pending_writes(self) -> None:
        import tempfile
        from pathlib import Path
        from typing import Annotated, TypedDict

        from langchain_core.messages import AIMessage, ToolMessage
        from langgraph.graph import END, START, StateGraph
        from langgraph.graph.message import add_messages
        from langgraph.types import interrupt

        class ApprovalState(TypedDict, total=False):
            messages: Annotated[list, add_messages]
            side_effect: str
            tool_result: ToolMessage
            decision: str

        side_effect_calls = 0

        def completed_side_effect(state):
            nonlocal side_effect_calls
            side_effect_calls += 1
            return {
                "side_effect": "already-completed",
                "tool_result": ToolMessage(
                    content="side effect completed",
                    tool_call_id="side-effect-call",
                ),
            }

        def wait_for_approval(state):
            decision = interrupt({"kind": "approval", "question": "Continue?"})
            return {
                "decision": decision,
                "messages": [AIMessage(content=f"Decision: {decision}")],
            }

        def build_graph(saver):
            builder = StateGraph(ApprovalState)
            builder.add_node("side_effect", completed_side_effect)
            builder.add_node("approval", wait_for_approval)
            builder.add_edge(START, "side_effect")
            builder.add_edge(START, "approval")
            builder.add_edge("side_effect", END)
            builder.add_edge("approval", END)
            return builder.compile(checkpointer=saver)

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "runs.sqlite3")
            provider = SQLiteCheckpointProvider(path)
            with provider.create_checkpointer() as first_saver:
                first_graph = build_graph(first_saver)
                first_context = AgentContext(
                    question="Please continue",
                    run_id="run-interrupted",
                    session_id="thread-interrupt",
                )
                compiled = CompiledAgent(spec=self.spec, executable=first_graph)
                with SQLiteRunRepository(path) as runs:
                    interrupted = AgentApplicationService(
                        run_repository=runs,
                    ).run(compiled, first_context)
                    self.assertEqual(interrupted.status, "interrupted")
                    self.assertTrue(interrupted.metadata["interrupts"])
                    self.assertEqual(runs.get("run-interrupted").status, "interrupted")

            with provider.create_checkpointer() as second_saver:
                pending = second_saver.get_tuple(
                    {"configurable": {"thread_id": "thread-interrupt"}}
                )
                self.assertTrue(pending.pending_writes)
                resumed_graph = build_graph(second_saver)
                resumed_context = AgentContext(
                    question="Resume approval",
                    run_id="run-resumed",
                    session_id="thread-interrupt",
                    metadata={"resume_value": "approved"},
                )
                resumed_compiled = CompiledAgent(
                    spec=self.spec,
                    executable=resumed_graph,
                )
                with SQLiteRunRepository(path) as runs:
                    resumed = AgentApplicationService(
                        run_repository=runs,
                    ).resume(resumed_compiled, resumed_context)
                    self.assertEqual(resumed.status, "completed")
                    self.assertEqual(resumed.content, "Decision: approved")
                    self.assertEqual(runs.get("run-resumed").status, "completed")
                self.assertEqual(side_effect_calls, 1)

    def test_streamed_langgraph_interrupt_is_persisted_as_interrupted(self) -> None:
        from typing import Annotated, TypedDict

        from langgraph.graph import END, START, StateGraph
        from langgraph.graph.message import add_messages
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import interrupt

        class ApprovalState(TypedDict, total=False):
            messages: Annotated[list, add_messages]

        def wait_for_approval(state):
            interrupt({"kind": "approval"})
            return {}

        builder = StateGraph(ApprovalState)
        builder.add_node("approval", wait_for_approval)
        builder.add_edge(START, "approval")
        builder.add_edge("approval", END)
        graph = builder.compile(checkpointer=InMemorySaver())
        compiled = CompiledAgent(spec=self.spec, executable=graph)
        repository = InMemoryRunRepository()
        context = AgentContext(
            question="Please continue",
            run_id="run-stream-interrupted",
            session_id="thread-stream-interrupted",
        )

        events = list(
            AgentApplicationService(run_repository=repository).stream(
                compiled,
                context,
            )
        )

        self.assertEqual(events[-1].event_type, "run.interrupted")
        self.assertEqual(repository.get("run-stream-interrupted").status, "interrupted")

    def test_application_service_tracks_completed_run(self) -> None:
        repository = InMemoryRunRepository()
        service = AgentApplicationService(run_repository=repository)
        executable = FakeExecutable()
        compiled = CompiledAgent(spec=self.spec, executable=executable)

        result = service.run(compiled, self.context)

        self.assertEqual(result.content, "done")
        record = service.get_run("run-1")
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.result, result)

    def test_application_service_tracks_stream_result(self) -> None:
        repository = InMemoryRunRepository()
        service = AgentApplicationService(run_repository=repository)
        compiled = CompiledAgent(spec=self.spec, executable=FakeExecutable())

        events = list(service.stream(compiled, self.context))

        self.assertEqual(events[-1].event_type, "run.completed")
        record = service.get_run("run-1")
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.result.content, "done")

    def test_application_service_resumes_langgraph_run(self) -> None:
        repository = InMemoryRunRepository()
        service = AgentApplicationService(run_repository=repository)
        compiled = CompiledAgent(spec=self.spec, executable=FakeExecutable())

        result = service.resume(compiled, self.context)

        self.assertEqual(result.content, "done")
        self.assertEqual(service.get_run("run-1").status, "completed")

    def test_langgraph_resume_passes_command_when_resume_value_is_supplied(self) -> None:
        executable = FakeExecutable()
        context = AgentContext(
            question="hello",
            run_id="run-resume-command",
            session_id="thread-1",
            metadata={"resume_value": "approved"},
        )

        LangGraphAgentRuntime(lambda: executable).resume(
            self.spec.to_definition(), context
        )

        self.assertEqual(executable.received_config["configurable"]["thread_id"], "thread-1")
        self.assertEqual(executable.received_payload.resume, "approved")

    def test_application_service_enforces_timeout_and_cancel(self) -> None:
        import time
        from threading import Event

        class SlowExecutable(FakeExecutable):
            def invoke(self, payload, *, config=None):
                time.sleep(0.05)
                return super().invoke(payload, config=config)

        short_spec = AgentSpec(
            agent_id=self.spec.agent_id,
            version=self.spec.version,
            goal=self.spec.goal,
            model=self.spec.model,
            prompt=self.spec.prompt,
            tools=self.spec.tools,
            runtime_type=self.spec.runtime_type,
            timeout_seconds=0.001,
        )
        service = AgentApplicationService()
        with self.assertRaises(AgentRunTimeoutError):
            service.run(CompiledAgent(spec=short_spec, executable=SlowExecutable()), self.context)
        with self.assertRaises(AgentRunTimeoutError):
            service.resume(
                CompiledAgent(spec=short_spec, executable=SlowExecutable()),
                AgentContext(
                    question="resume",
                    run_id="run-resume-timeout",
                    session_id=self.context.session_id,
                ),
            )

        cancel = Event()
        cancel.set()
        cancelled_context = AgentContext(
            question=self.context.question,
            run_id="run-cancelled",
            session_id=self.context.session_id,
            metadata={"cancel_event": cancel},
        )
        with self.assertRaises(AgentRunCancelledError):
            service.run(CompiledAgent(spec=self.spec, executable=FakeExecutable()), cancelled_context)
        with self.assertRaises(AgentRunCancelledError):
            service.resume(
                CompiledAgent(spec=self.spec, executable=FakeExecutable()),
                cancelled_context,
            )

    def test_sqlite_run_repository_round_trips_result(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            with SQLiteRunRepository(str(Path(directory) / "runs.sqlite3")) as repository:
                record = RunRecord.pending(self.spec.to_definition(), self.context).transition(
                    "completed",
                    result=AgentRunResult(
                        run_id="run-1",
                        session_id="thread-1",
                        content="done",
                    ),
                )
                repository.save(record)
                loaded = repository.get("run-1")

        self.assertEqual(loaded.status, "completed")
        self.assertEqual(loaded.result.content, "done")

    def test_run_repository_lists_runs_and_threads(self) -> None:
        repository = InMemoryRunRepository()
        first = RunRecord.pending(self.spec.to_definition(), self.context).transition("completed")
        second_context = AgentContext(question="again", run_id="run-2", session_id="thread-2")
        second = RunRecord.pending(self.spec.to_definition(), second_context).transition("failed", error_type="x")
        repository.save(first)
        repository.save(second)
        self.assertEqual(len(repository.list(status="completed")), 1)
        threads = repository.list_threads()
        self.assertEqual({item.session_id for item in threads}, {"thread-1", "thread-2"})

    def test_callback_event_sink_adapts_runtime_event(self) -> None:
        received = []
        sink = CallbackEventSink(lambda event_type, **details: received.append((event_type, details)))

        sink.publish(
            AgentEvent(
                event_type="run.completed",
                run_id="run-1",
                session_id="thread-1",
                data={"content": "done"},
            )
        )

        self.assertEqual(received[0][0], "run.completed")
        self.assertEqual(received[0][1]["content"], "done")

    def test_application_service_retries_with_bounded_attempts(self) -> None:
        class FlakyExecutable(FakeExecutable):
            attempts = 0

            def invoke(self, payload, *, config=None):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("temporary")
                return super().invoke(payload, config=config)

        compiled = CompiledAgent(spec=self.spec, executable=FlakyExecutable())
        context = AgentContext(
            question=self.context.question,
            run_id="run-retry",
            session_id=self.context.session_id,
            metadata={"max_retries": 1},
        )

        result = AgentApplicationService().run(compiled, context)

        self.assertEqual(result.content, "done")
        self.assertEqual(compiled.executable.attempts, 2)

    def test_compiler_builds_real_langgraph_state_graph(self) -> None:
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        class BindableFakeListChatModel(FakeListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        def unused_tool() -> str:
            """Return a test value."""
            return "unused"

        class FakeModelProvider:
            def create_chat_model(self, spec):
                return BindableFakeListChatModel(responses=["ok"])

        registry = ToolRegistry(
            [
                ToolSpec(
                    tool_id="test-tool",
                    version=1,
                    description="test",
                    implementation=unused_tool,
                    input_schema=dict,
                    output_schema=dict,
                )
            ]
        )
        compiled = LangChainAgentCompiler(
            model_provider=FakeModelProvider(),
            tool_provider=registry,
        ).compile(self.spec)

        result = compiled.executable.invoke(
            {"messages": [{"role": "user", "content": "hello"}]},
            config={"configurable": {"thread_id": "integration-thread"}},
        )

        self.assertEqual(type(compiled.executable).__name__, "CompiledStateGraph")
        self.assertEqual(result["messages"][-1].content, "ok")

    def test_application_service_propagates_context_to_worker_thread(self) -> None:
        from contextvars import ContextVar

        marker: ContextVar[str | None] = ContextVar("marker", default=None)
        observed: dict[str, str | None] = {}

        class ContextAwareExecutable(FakeExecutable):
            def invoke(self, payload, *, config=None):
                observed["value"] = marker.get()
                return super().invoke(payload, config=config)

        token = marker.set("visible")
        try:
            AgentApplicationService().run(
                CompiledAgent(spec=self.spec, executable=ContextAwareExecutable()),
                self.context,
            )
        finally:
            marker.reset(token)

        self.assertEqual(observed["value"], "visible")

    def test_verify_resumable_requires_checkpoint_and_unfinished_run(self) -> None:
        class FakeCheckpointRepository:
            def __init__(self, records=()):
                self._records = records

            def list(self, thread_id=None, *, limit=None):
                if thread_id == "no-checkpoints":
                    return ()
                return tuple(self._records)

            def get(self, thread_id, checkpoint_id=None):
                raise KeyError(thread_id)

            def delete_thread(self, thread_id):
                return None

        run_repository = InMemoryRunRepository()
        service = AgentApplicationService(
            run_repository=run_repository,
            checkpoint_repository=FakeCheckpointRepository(records=[object()]),
        )

        interrupted = RunRecord.pending(
            self.spec.to_definition(), self.context
        ).transition("interrupted")
        run_repository.save(interrupted)
        self.assertEqual(service.verify_resumable("thread-1").run_id, "run-1")

        run_repository.save(interrupted.transition("completed"))
        with self.assertRaises(ResumeNotAllowedError):
            service.verify_resumable("thread-1")

        with self.assertRaises(ResumeNotAllowedError):
            service.verify_resumable("no-checkpoints")

        with self.assertRaises(ValueError):
            service.verify_resumable("   ")

    def test_sqlite_run_repository_persists_parent_run_id(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            context = AgentContext(
                question="resume",
                run_id="run-child",
                session_id="thread-1",
                metadata={"parent_run_id": "run-parent"},
            )
            with SQLiteRunRepository(str(Path(directory) / "runs.sqlite3")) as repository:
                repository.save(
                    RunRecord.pending(self.spec.to_definition(), context)
                )
                loaded = repository.get("run-child")

        self.assertEqual(loaded.parent_run_id, "run-parent")

    def test_langgraph_resume_without_value_passes_no_input(self) -> None:
        executable = FakeExecutable()
        context = AgentContext(
            question="hello",
            run_id="run-resume-none",
            session_id="thread-1",
        )

        LangGraphAgentRuntime(lambda: executable).resume(
            self.spec.to_definition(),
            context,
        )

        self.assertIsNone(executable.received_payload)

    def test_langgraph_consecutive_interrupts_require_multiple_resumes(self) -> None:
        from typing import Annotated, TypedDict

        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.graph.message import add_messages
        from langgraph.types import interrupt

        class ApprovalState(TypedDict, total=False):
            messages: Annotated[list, add_messages]
            approvals: list[str]

        def first_approval(state):
            value = interrupt({"step": 1})
            return {"approvals": (state.get("approvals") or []) + [f"1:{value}"]}

        def second_approval(state):
            value = interrupt({"step": 2})
            return {
                "approvals": (state.get("approvals") or []) + [f"2:{value}"],
                "messages": [AIMessage(content="done")],
            }

        builder = StateGraph(ApprovalState)
        builder.add_node("first", first_approval)
        builder.add_node("second", second_approval)
        builder.add_edge(START, "first")
        builder.add_edge("first", "second")
        builder.add_edge("second", END)
        graph = builder.compile(checkpointer=InMemorySaver())

        service = AgentApplicationService()
        thread_id = "thread-consecutive"

        first = service.run(
            CompiledAgent(spec=self.spec, executable=graph),
            AgentContext(question="start", run_id="run-1", session_id=thread_id),
        )
        second = service.resume(
            CompiledAgent(spec=self.spec, executable=graph),
            AgentContext(
                question="approve first",
                run_id="run-2",
                session_id=thread_id,
                metadata={"resume_value": "a"},
            ),
        )
        third = service.resume(
            CompiledAgent(spec=self.spec, executable=graph),
            AgentContext(
                question="approve second",
                run_id="run-3",
                session_id=thread_id,
                metadata={"resume_value": "b"},
            ),
        )

        self.assertEqual(first.status, "interrupted")
        self.assertEqual(second.status, "interrupted")
        self.assertEqual(third.status, "completed")
        self.assertEqual(third.content, "done")
        final_state = graph.get_state(
            {"configurable": {"thread_id": thread_id}}
        ).values
        self.assertEqual(final_state["approvals"], ["1:a", "2:b"])


if __name__ == "__main__":
    unittest.main()
