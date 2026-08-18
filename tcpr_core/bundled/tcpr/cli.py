"""Command-line adapter for TCPR's three public operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .core_api import CoreService, FileKV


def _source(path: Path, primary_key: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"data": path, "filename": path.name}
    if primary_key:
        payload["primary_key"] = primary_key
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tcpr")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".tcpr-state"),
        help="local persistent state directory (default: .tcpr-state)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_state_override(command_parser: argparse.ArgumentParser) -> None:
        # Also accept --state-dir after the subcommand while preserving the
        # global value when it was supplied before the subcommand.
        command_parser.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    build_index_parser = sub.add_parser("build-index", help="build and activate a dynamic-attribute index")
    add_state_override(build_index_parser)
    build_index_parser.add_argument("file", type=Path)
    build_index_parser.add_argument("--primary-key", default="")

    build_database_parser = sub.add_parser("build-database", help="build and activate a database snapshot")
    add_state_override(build_database_parser)
    build_database_parser.add_argument("file", type=Path)
    build_database_parser.add_argument("index_id")
    build_database_parser.add_argument("--primary-key", default="")

    search_parser = sub.add_parser("search", help="search persisted index/database snapshots")
    add_state_override(search_parser)
    search_parser.add_argument("query_json")
    search_parser.add_argument("index_id")
    search_parser.add_argument("database_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    service = CoreService(FileKV(args.state_dir))
    if args.command == "build-index":
        print(json.dumps({"index_id": service.build_index(_source(args.file, args.primary_key))}, ensure_ascii=False))
        return 0
    if args.command == "build-database":
        print(json.dumps({"database_id": service.build_database(_source(args.file, args.primary_key), args.index_id)}, ensure_ascii=False))
        return 0
    if args.command == "search":
        query_path = Path(args.query_json)
        query = query_path.read_text(encoding="utf-8") if query_path.is_file() else args.query_json
        print(json.dumps(service.search(query, args.index_id, args.database_id), ensure_ascii=False, indent=2, default=list))
        return 0
    raise AssertionError("unhandled TCPR command")


if __name__ == "__main__":
    raise SystemExit(main())
