"""Run the HTTP service boundary: ``python -m service``."""

from __future__ import annotations

import argparse
import sys

from service.api import API_PREFIX
from service.app import create_server
from service.config import ServiceConfigurationError, ServiceSettings


def parse_arguments() -> argparse.Namespace:
    """Parse service command-line arguments."""
    parser = argparse.ArgumentParser(description="启动 Agent HTTP 服务边界")
    parser.add_argument("--host", help="覆盖 SERVICE_HOST")
    parser.add_argument("--port", type=int, help="覆盖 SERVICE_PORT")
    return parser.parse_args()


def main() -> int:
    """Start the HTTP server and serve until interrupted."""
    arguments = parse_arguments()
    try:
        settings = ServiceSettings.from_environment()
    except ServiceConfigurationError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if arguments.host or arguments.port:
        try:
            settings = ServiceSettings(
                tenants=settings.tenants,
                host=arguments.host or settings.host,
                port=arguments.port or settings.port,
                rate_limit_per_minute=settings.rate_limit_per_minute,
                max_body_bytes=settings.max_body_bytes,
                request_timeout_seconds=settings.request_timeout_seconds,
                audit_path=settings.audit_path,
                allow_public_exposure=settings.allow_public_exposure,
                checkpoint_db_path=settings.checkpoint_db_path,
                tracing_policy=settings.tracing_policy,
            )
        except ServiceConfigurationError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
    server = create_server(settings)
    print(
        f"服务已启动：http://{settings.host}:{server.server_port}{API_PREFIX}"
        f"（租户 {len(settings.tenants)} 个，限流 {settings.rate_limit_per_minute}/分钟）"
    )
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
