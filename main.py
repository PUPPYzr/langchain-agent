"""Command-line entry point for the LangChain weather agent."""

from __future__ import annotations

import argparse
import json
import sys
from threading import Event
from time import perf_counter
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler

from agent_platform import (
    AgentApplicationService,
    AgentContext,
    CallbackEventSink,
    ResumeNotAllowedError,
    SQLiteCheckpointRepository,
    SQLiteRunRepository,
)
from agent_platform.domain import RUN_STATUSES
from agent_platform.metrics import (
    Pricing,
    summarize_runs,
    summarize_trace_directory,
)
from agent_platform.runs import RunRecord, ThreadRecord
from agent_platform.telemetry import (
    TracingPolicy,
    classify_error,
    purge_expired_traces,
)
from weather_agent.agent import build_compiled_weather_agent
from weather_agent.config import (
    ConfigurationError,
    Settings,
    checkpoint_db_path_from_environment,
)
from weather_agent.observability import (
    TRACE_DIRECTORY,
    ProgressReporter,
    TraceRecorder,
    progress_context,
    record_trace,
)
from weather_agent.weather_service import OpenMeteoWeatherService, WeatherServiceError


class TimingCallbackHandler(BaseCallbackHandler):
    """Log individual model and tool run durations for Agent mode."""

    def __init__(
        self,
        *,
        run_id: str,
        model_name: str,
        progress: ProgressReporter | None = None,
    ) -> None:
        self.run_id = run_id
        self.model_name = model_name
        self.progress = progress
        self._started_at: dict[Any, float] = {}

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> Any:
        run_id = kwargs.get("run_id")
        if run_id is not None:
            self._started_at[run_id] = perf_counter()
        return run_id

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        run_id = kwargs.get("run_id")
        started_at = self._started_at.pop(run_id, None)
        if started_at is not None:
            usage = self._extract_usage(response)
            elapsed = round(perf_counter() - started_at, 3)
            record_trace(
                "model",
                "request_completed",
                model=self.model_name,
                elapsed_seconds=elapsed,
                input_tokens=usage.get("input_tokens", "unknown"),
                output_tokens=usage.get("output_tokens", "unknown"),
            )
            if self.progress is not None:
                self.progress.update("模型响应完成")

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, Any]:
        """Extract token usage from LangChain's possible response shapes."""
        usage = getattr(response, "usage_metadata", None) or {}
        if usage:
            return usage

        generations = getattr(response, "generations", None) or []
        if generations and generations[0]:
            message = getattr(generations[0][0], "message", None)
            usage = getattr(message, "usage_metadata", None) or {}
            if usage:
                return usage

        llm_output = getattr(response, "llm_output", None) or {}
        return llm_output.get("token_usage", {}) or {}

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> Any:
        tool_name = serialized.get("name", "unknown_tool")
        try:
            arguments = json.loads(input_str)
        except (TypeError, json.JSONDecodeError):
            arguments = input_str
        # Duplicate-call and tool-budget policy is enforced by the platform
        # ToolCallGuardCallback injected in ExecutableAgentRuntime.
        record_trace(
            "tool",
            "call_started",
            tool=tool_name,
            arguments=arguments,
        )
        if self.progress is not None:
            self.progress.update(f"正在调用工具：{tool_name}")
        run_id = kwargs.get("run_id")
        if run_id is not None:
            self._started_at[run_id] = perf_counter()
        return run_id

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        run_id = kwargs.get("run_id")
        started_at = self._started_at.pop(run_id, None)
        if started_at is not None:
            record_trace(
                "tool",
                "call_completed",
                elapsed_seconds=round(perf_counter() - started_at, 3),
            )
            if self.progress is not None:
                self.progress.update("工具调用完成，正在生成答案")


def resolve_tracing_policy() -> TracingPolicy:
    """Load the tracing policy, falling back to safe defaults on bad input."""
    try:
        return TracingPolicy.from_environment()
    except ValueError as exc:
        print(f"警告：Trace 策略配置无效（{exc}），使用默认策略。", file=sys.stderr)
        return TracingPolicy()


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="查询某地当前天气的 LangChain Agent")
    parser.add_argument(
        "location",
        nargs="?",
        help="地点或天气问题，例如：北京、北京未来三天天气、比较北京和上海",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="使用大模型 Agent 处理查询；默认直接调用天气服务",
    )
    parser.add_argument(
        "--list-runs",
        action="store_true",
        help="列出已记录的 Run，可按 --session/--status/--limit 过滤",
    )
    parser.add_argument(
        "--list-threads",
        action="store_true",
        help="列出 Thread 及其最新状态",
    )
    parser.add_argument(
        "--list-checkpoints",
        metavar="THREAD_ID",
        help="列出指定 Thread 的 Checkpoint",
    )
    parser.add_argument(
        "--resume",
        metavar="THREAD_ID",
        help="恢复指定 Thread 上处于暂停状态的运行",
    )
    parser.add_argument(
        "--resume-value",
        metavar="TEXT",
        help="恢复时传递给 interrupt() 的值；省略则以 None 恢复",
    )
    parser.add_argument(
        "--session",
        metavar="SESSION_ID",
        help="按 Thread/Session 过滤 Run",
    )
    parser.add_argument(
        "--status",
        choices=RUN_STATUSES,
        help="按运行状态过滤 Run",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="限制查询返回的条数",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="输出 Run 与 Trace 结构化指标（成功率/失败阶段/延迟分位/调用数/Token/估算成本）",
    )
    parser.add_argument(
        "--purge-traces",
        action="store_true",
        help="按 TRACE_RETENTION_DAYS 清理过期 Trace 文件",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出（配合 --metrics）",
    )
    return parser.parse_args()


def clean_terminal_text(text: str) -> str:
    """Remove Markdown emphasis markers from terminal-facing Agent output."""
    return text.replace("**", "")


def format_weather(weather: dict[str, Any]) -> str:
    """Format the structured weather response without another model request."""
    location = weather["resolved_location"]["display_name"]
    current = weather.get("current", {})
    units = weather.get("units", {})

    def value(field: str) -> str:
        raw_value = current.get(field)
        if raw_value is None:
            return "暂无数据"
        return f"{raw_value}{units.get(field, '')}"

    return "\n".join(
        (
            f"{location} 当前天气",
            f"观测时间：{weather.get('observation_time', '未知')}",
            f"天气：{weather.get('weather_description', '未知')}",
            f"温度：{value('temperature_2m')}，体感：{value('apparent_temperature')}",
            f"湿度：{value('relative_humidity_2m')}，降水：{value('precipitation')}",
            f"风速：{value('wind_speed_10m')}，风向：{value('wind_direction_10m')}",
        )
    )


INTERRUPTED_EXIT_CODE = 3


def format_run(record: RunRecord) -> str:
    """Render one Run record as a single readable line."""
    parts = [
        record.run_id,
        f"status={record.status}",
        f"session={record.session_id}",
        f"runtime={record.runtime_type}",
        f"updated={record.updated_at}",
    ]
    if record.parent_run_id:
        parts.append(f"parent={record.parent_run_id}")
    if record.error_type:
        parts.append(f"error={record.error_type}")
    if record.result is not None and record.result.content:
        summary = record.result.content.replace("\n", " ").strip()
        parts.append(f"content={summary[:80]}")
    return "  ".join(parts)


def print_runs(records: tuple[RunRecord, ...]) -> None:
    """Print Run records or a friendly empty message."""
    if not records:
        print("没有匹配的 Run。")
        return
    for record in records:
        print(format_run(record))


def print_threads(records: tuple[ThreadRecord, ...]) -> None:
    """Print Thread summaries or a friendly empty message."""
    if not records:
        print("没有 Thread。")
        return
    for record in records:
        print(
            f"{record.session_id}  runs={record.run_count}  "
            f"latest={record.latest_run_id}  status={record.latest_status}  "
            f"updated={record.updated_at}"
        )


def print_checkpoints(records: Any) -> None:
    """Print Checkpoint summaries or a friendly empty message."""
    if not records:
        print("没有 Checkpoint。")
        return
    for record in records:
        print(
            f"{record.thread_id}  checkpoint={record.checkpoint_id}  "
            f"ns={record.checkpoint_ns}  parent={record.parent_checkpoint_id or '-'}  "
            f"created={record.created_at}"
        )


def resume_thread(
    thread_id: str,
    resume_value: str | None,
    service: AgentApplicationService,
) -> int:
    """Resume an interrupted thread, creating a new Run on the same thread."""
    parent_run = service.verify_resumable(thread_id)
    settings = Settings.from_environment()
    run_id = str(uuid4())
    metadata: dict[str, Any] = {}
    if resume_value is not None:
        metadata["resume_value"] = resume_value
    if parent_run is not None:
        metadata["parent_run_id"] = parent_run.run_id
    progress = ProgressReporter()
    with progress_context(progress), TraceRecorder(
        run_id=run_id,
        session_id=thread_id,
        model_name=settings.openai_model,
        policy=resolve_tracing_policy(),
    ) as trace:
        trace.record("run", "resume_requested", thread_id=thread_id)
        progress.update("正在恢复 Agent")
        compiled_agent = build_compiled_weather_agent(settings)
        try:
            result = service.resume(
                compiled_agent,
                AgentContext(
                    question="Resume interrupted run",
                    run_id=run_id,
                    session_id=thread_id,
                    metadata=metadata,
                ),
            )
        except Exception as exc:
            trace.record(
                "run",
                "failed",
                error_type=classify_error(exc),
                exception=type(exc).__name__,
            )
            progress.finish("恢复失败")
            print(
                f"错误：恢复执行失败，run_id={run_id}，"
                f"error_type={classify_error(exc)}。",
                file=sys.stderr,
            )
            return 1
        if result.status == "interrupted":
            trace.record("run", "interrupted")
            progress.finish("已暂停")
            print(
                f"运行仍然暂停，thread_id={thread_id}。"
                f"可用 python main.py --resume {thread_id} 再次恢复。",
                file=sys.stderr,
            )
            return INTERRUPTED_EXIT_CODE
        trace.record("run", "answer_ready")
        progress.finish("完成")
        print(clean_terminal_text(result.content))
        return 0


def print_metrics(
    records: tuple[RunRecord, ...],
    *,
    json_output: bool = False,
) -> None:
    """Print Run ledger and trace metrics for version-to-version comparison."""
    run_metrics = summarize_runs(records)
    trace_metrics = summarize_trace_directory(
        TRACE_DIRECTORY,
        pricing=Pricing.from_environment(),
    )
    if json_output:
        print(
            json.dumps(
                {"runs": run_metrics.to_dict(), "traces": trace_metrics.to_dict()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    cost = (
        f"${trace_metrics.estimated_cost_usd:.6f}"
        if trace_metrics.pricing_configured
        else "未配置价格"
    )
    print(
        f"Run 指标：总数 {run_metrics.total}，成功率 {run_metrics.success_rate:.0%}，"
        f"状态 {dict(run_metrics.by_status)}"
    )
    print(f"失败分类：{dict(run_metrics.by_error_category) or '无'}")
    print(
        f"延迟(ms)：p50 {run_metrics.latency.p50_ms}，"
        f"p95 {run_metrics.latency.p95_ms}，max {run_metrics.latency.max_ms}"
        f"（样本 {run_metrics.latency.count}）"
    )
    print(
        f"Trace 指标：runs {trace_metrics.runs}，model {trace_metrics.model_calls}，"
        f"tool {trace_metrics.tool_calls}，http {trace_metrics.http_calls}；"
        f"token 入 {trace_metrics.input_tokens} / 出 {trace_metrics.output_tokens}；"
        f"估算成本 {cost}"
    )
    print(
        f"异常路径：retries {trace_metrics.retries}，"
        f"cancellations {trace_metrics.cancellations}，"
        f"timeouts {trace_metrics.timeouts}，interrupts {trace_metrics.interrupts}，"
        f"resumes {trace_metrics.resumes}"
    )


def run_management_command(arguments: argparse.Namespace) -> int:
    """Handle Thread/Run/Checkpoint queries, metrics and explicit resume."""
    if arguments.purge_traces:
        policy = resolve_tracing_policy()
        removed = purge_expired_traces(TRACE_DIRECTORY, policy)
        print(f"已清理 {removed} 个过期 Trace 文件（保留 {policy.retention_days} 天）。")
        return 0
    db_path = checkpoint_db_path_from_environment()
    try:
        with SQLiteRunRepository(db_path) as run_repository, SQLiteCheckpointRepository(
            db_path
        ) as checkpoint_repository:
            service = AgentApplicationService(
                run_repository=run_repository,
                checkpoint_repository=checkpoint_repository,
            )
            if arguments.list_runs:
                print_runs(
                    service.list_runs(
                        session_id=arguments.session,
                        status=arguments.status,
                        limit=arguments.limit,
                    )
                )
                return 0
            if arguments.list_threads:
                print_threads(service.list_threads(limit=arguments.limit))
                return 0
            if arguments.list_checkpoints is not None:
                print_checkpoints(
                    service.list_checkpoints(
                        arguments.list_checkpoints,
                        limit=arguments.limit,
                    )
                )
                return 0
            if arguments.metrics:
                print_metrics(
                    service.list_runs(
                        session_id=arguments.session,
                        status=arguments.status,
                        limit=arguments.limit,
                    ),
                    json_output=arguments.json,
                )
                return 0
            if arguments.resume is not None:
                return resume_thread(arguments.resume, arguments.resume_value, service)
    except (ConfigurationError, ResumeNotAllowedError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except (KeyError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    return 2


def main() -> int:
    """Run a single weather query or a management command."""
    arguments = parse_arguments()
    if (
        arguments.list_runs
        or arguments.list_threads
        or arguments.list_checkpoints is not None
        or arguments.resume is not None
        or arguments.metrics
        or arguments.purge_traces
    ):
        return run_management_command(arguments)
    interactive_mode = arguments.location is None
    try:
        question = arguments.location or input("请输入你要询问的问题？").strip()
    except EOFError:
        print("未检测到输入，请重新运行程序并输入问题。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    if not question:
        print("问题不能为空。", file=sys.stderr)
        return 2

    # Lets Ctrl+C request cooperative cancellation of the running Agent.
    cancel_event = Event()
    try:
        use_agent = arguments.agent or interactive_mode
        if use_agent:
            settings = Settings.from_environment()
            run_id = str(uuid4())
            session_id = str(uuid4())
            progress = ProgressReporter()
            with progress_context(progress), TraceRecorder(
                run_id=run_id,
                session_id=session_id,
                model_name=settings.openai_model,
                policy=resolve_tracing_policy(),
            ) as trace:
                trace.record("run", "question_received", question=question)
                progress.update("正在初始化 Agent")
                compiled_agent = build_compiled_weather_agent(settings)
                trace.record("agent", "initialized")
                progress.update("正在请求模型分析问题")
                try:
                    with SQLiteRunRepository(
                        settings.agent_checkpoint_db_path
                    ) as run_repository, SQLiteCheckpointRepository(
                        settings.agent_checkpoint_db_path
                    ) as checkpoint_repository:
                        result = AgentApplicationService(
                            run_repository=run_repository,
                            checkpoint_repository=checkpoint_repository,
                        ).run(
                            compiled_agent,
                            AgentContext(
                                question=question,
                                run_id=run_id,
                                session_id=session_id,
                                metadata={
                                    "cancel_event": cancel_event,
                                    "runtime_config": {
                                        "callbacks": [
                                            TimingCallbackHandler(
                                                run_id=run_id,
                                                model_name=settings.openai_model,
                                                progress=progress,
                                            )
                                        ],
                                        "metadata": {
                                            "run_id": run_id,
                                            "session_id": session_id,
                                        },
                                        "recursion_limit": max(
                                            2,
                                            settings.agent_max_turns * 2 + 1,
                                        ),
                                    },
                                    "tool_call_policy": {
                                        "max_tool_calls": settings.agent_max_turns,
                                    },
                                    "event_sink": CallbackEventSink(
                                        lambda event_type, **details: trace.record(
                                            "runtime", event_type, **details
                                        )
                                    ),
                                },
                            ),
                        )
                except Exception as exc:
                    trace.record(
                        "run",
                        "failed",
                        error_type=classify_error(exc),
                        exception=type(exc).__name__,
                    )
                    progress.finish("Agent 失败")
                    print(
                        f"错误：Agent 执行失败，run_id={run_id}，"
                        f"error_type={classify_error(exc)}。"
                        "可检查模型服务，或暂时使用确定性天气查询模式。",
                        file=sys.stderr,
                    )
                    return 1
                if result.status == "interrupted":
                    trace.record("run", "interrupted")
                    progress.finish("已暂停")
                    print(
                        f"运行已暂停，thread_id={session_id}。"
                        f"可用 python main.py --resume {session_id} 恢复。",
                        file=sys.stderr,
                    )
                    return INTERRUPTED_EXIT_CODE
                trace.record("run", "answer_ready")
                progress.finish("完成")
                output = clean_terminal_text(result.content)
        else:
            run_id = str(uuid4())
            session_id = str(uuid4())
            progress = ProgressReporter()
            with progress_context(progress), TraceRecorder(
                run_id=run_id,
                session_id=session_id,
                policy=resolve_tracing_policy(),
            ) as trace:
                trace.record("run", "question_received", question=question)
                try:
                    weather = OpenMeteoWeatherService().get_current_weather(question)
                except WeatherServiceError:
                    progress.finish("查询失败")
                    raise
                trace.record("run", "answer_ready")
                progress.finish("完成")
                output = format_weather(weather)
    except (ConfigurationError, WeatherServiceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        cancel_event.set()
        print(
            "\n已取消。正在请求 Agent 停止；进行中的模型或 HTTP 调用无法中断。",
            file=sys.stderr,
        )
        return 130

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
