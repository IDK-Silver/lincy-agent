"""Standalone executable entry point for the native Codex proxy."""

from __future__ import annotations

import argparse
from dataclasses import replace
import sys

import uvicorn

from chat_agent.core.config import load_app_timezone
from chat_agent.timezone_utils import configure_runtime_timezone

from .app import create_app
from .settings import CodexProxySettings


def build_serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-proxy")
    parser.add_argument("--host", help="Bind host")
    parser.add_argument("--port", type=int, help="Bind port")
    return parser


def run_serve(args: argparse.Namespace) -> None:
    configure_runtime_timezone(load_app_timezone())
    settings = CodexProxySettings.from_env()
    if args.host:
        settings = replace(settings, host=args.host)
    if args.port:
        settings = replace(settings, port=args.port)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="warning",
    )


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "serve":
        argv = argv[1:]
    args = build_serve_parser().parse_args(argv)
    run_serve(args)


if __name__ == "__main__":
    main()
