from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from typing import Any

from . import __version__


LOCATOR_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^#\s]+)#(?P<number>\d+)$")
SENSITIVE_FLAGS = {"--body", "--body-file", "--message", "--with-msg"}
VALID_LOG_LEVELS = {"debug": 10, "info": 20, "error": 30}


@dataclass
class CommandResult:
    command: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class ToolExecutionError(RuntimeError):
    pass


class StderrLogger:
    def __init__(self, level: str) -> None:
        normalized = level.lower()
        if normalized not in VALID_LOG_LEVELS:
            raise ValueError(f"invalid log level: {level}")
        self.level = normalized

    def _enabled(self, level: str) -> bool:
        return VALID_LOG_LEVELS[level] >= VALID_LOG_LEVELS[self.level]

    def log(self, level: str, message: str) -> None:
        if not self._enabled(level):
            return
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{timestamp}] {level.upper()} {message}", file=sys.stderr, flush=True)

    def debug(self, message: str) -> None:
        self.log("debug", message)

    def info(self, message: str) -> None:
        self.log("info", message)

    def error(self, message: str) -> None:
        self.log("error", message)


LOGGER = StderrLogger(os.environ.get("FJ_MCP_LOG_LEVEL", "info"))


class FjRunner:
    def __init__(self, fj_bin: str, neutral_cwd: str, default_host: str | None, logger: StderrLogger) -> None:
        self.fj_bin = fj_bin
        self.neutral_cwd = neutral_cwd
        self.default_host = default_host
        self.logger = logger

    def run(
        self,
        args: list[str],
        *,
        host: str | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        command = [self.fj_bin]
        resolved_host = host or self.default_host
        if resolved_host:
            command.extend(["--host", resolved_host])
        command.extend(["--style", "minimal"])
        command.extend(args)

        resolved_cwd = cwd or self.neutral_cwd
        self.logger.info(
            f"running fj command cwd={resolved_cwd} command={format_command_for_logs(command)}"
        )
        completed = subprocess.run(
            command,
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        self.logger.info(
            "fj command finished "
            f"exit_code={completed.returncode} stdout_len={len(completed.stdout)} stderr_len={len(completed.stderr)}"
        )
        if completed.stderr.strip():
            self.logger.debug(f"fj stderr={completed.stderr.strip()}")
        return CommandResult(
            command=command,
            cwd=resolved_cwd,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _json_schema(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    additional_properties: bool = False,
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = required
    return schema


TOOLS: list[dict[str, Any]] = [
    {
        "name": "discover_pull_requests",
        "description": "Search pull requests in a Forgejo repository through fj.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string", "description": "Forgejo host. Optional if the server was started with --default-host."},
                "repo": {"type": "string", "description": "Repository in owner/name format."},
                "query": {"type": "string", "description": "Free-text search query."},
                "labels": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "One or more labels.",
                },
                "creator": {"type": "string"},
                "assignee": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
            },
            required=["repo"],
        ),
    },
    {
        "name": "view_pull_request",
        "description": "View a pull request body, comments, labels, files, or commits.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "pull_request": {
                    "type": "string",
                    "description": "Pull request locator in owner/repo#123 format.",
                },
                "view": {
                    "type": "string",
                    "enum": ["body", "comments", "comment", "labels", "files", "commits"],
                    "default": "body",
                },
                "comment_index": {"type": "integer", "minimum": 1},
                "oneline": {"type": "boolean", "description": "Only used when view=commits."},
            },
            required=["pull_request"],
        ),
    },
    {
        "name": "create_pull_request",
        "description": "Create a pull request. Use workdir/use_current_repo for local branch context when needed.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "repo": {"type": "string", "description": "Target repository in owner/name format."},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "base": {"type": "string"},
                "head": {"type": "string"},
                "autofill": {"type": "boolean"},
                "web": {"type": "boolean"},
                "agit": {"type": "boolean"},
                "remote": {"type": "string", "description": "Git remote name when relying on local repo context."},
                "use_current_repo": {"type": "boolean"},
                "workdir": {"type": "string", "description": "Working directory to run fj from."},
            }
        ),
    },
    {
        "name": "close_pull_request",
        "description": "Close a pull request without merging it.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "pull_request": {
                    "type": "string",
                    "description": "Pull request locator in owner/repo#123 format.",
                },
                "message": {"type": "string", "description": "Optional closing comment."},
            },
            required=["pull_request"],
        ),
    },
    {
        "name": "merge_pull_request",
        "description": "Merge a pull request with the selected strategy.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "pull_request": {
                    "type": "string",
                    "description": "Pull request locator in owner/repo#123 format.",
                },
                "method": {
                    "type": "string",
                    "enum": ["merge", "rebase", "rebase-merge", "squash", "manual"],
                },
                "delete_branch": {"type": "boolean"},
                "title": {"type": "string"},
                "message": {"type": "string"},
            },
            required=["pull_request"],
        ),
    },
    {
        "name": "approve_pull_request",
        "description": "Approve a pull request through the Forgejo HTTP API. Requires a token in the configured environment variable.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "pull_request": {
                    "type": "string",
                    "description": "Pull request locator in owner/repo#123 format.",
                },
                "body": {"type": "string", "description": "Optional approval message."},
            },
            required=["pull_request"],
        ),
    },
    {
        "name": "discover_repositories",
        "description": "List repositories from the authenticated user, a specific user, or an organization.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "owner_type": {
                    "type": "string",
                    "enum": ["current_user", "user", "org"],
                    "default": "current_user",
                },
                "owner": {"type": "string", "description": "Required when owner_type=org. Optional for owner_type=user."},
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "sort": {"type": "string", "enum": ["name", "modified", "created", "stars", "forks"]},
                "starred": {"type": "boolean"},
            }
        ),
    },
    {
        "name": "view_repository",
        "description": "View repository information.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "repo": {"type": "string", "description": "Repository in owner/name format."},
            },
            required=["repo"],
        ),
    },
    {
        "name": "discover_issues",
        "description": "Search issues in a Forgejo repository through fj.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "repo": {"type": "string", "description": "Repository in owner/name format."},
                "query": {"type": "string"},
                "labels": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                },
                "creator": {"type": "string"},
                "assignee": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
            },
            required=["repo"],
        ),
    },
    {
        "name": "view_issue",
        "description": "View an issue body, comments, or one comment.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "issue": {"type": "string", "description": "Issue locator in owner/repo#123 format."},
                "view": {"type": "string", "enum": ["body", "comments", "comment"], "default": "body"},
                "comment_index": {"type": "integer", "minimum": 1},
            },
            required=["issue"],
        ),
    },
    {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "repo": {"type": "string", "description": "Repository in owner/name format."},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "template": {"type": "string"},
                "no_template": {"type": "boolean"},
                "web": {"type": "boolean"},
            },
            required=["repo", "title"],
        ),
    },
    {
        "name": "comment_on_issue",
        "description": "Add a comment to an issue.",
        "inputSchema": _json_schema(
            {
                "host": {"type": "string"},
                "issue": {"type": "string", "description": "Issue locator in owner/repo#123 format."},
                "body": {"type": "string"},
            },
            required=["issue", "body"],
        ),
    },
]


def normalize_labels(labels: Any) -> str | None:
    if labels is None:
        return None
    if isinstance(labels, str):
        return labels
    if isinstance(labels, list):
        values = [str(label).strip() for label in labels if str(label).strip()]
        return ",".join(values) if values else None
    raise ToolExecutionError("labels must be a string or array of strings")


def parse_locator(locator: str) -> tuple[str, str, int]:
    match = LOCATOR_RE.match(locator.strip())
    if not match:
        raise ToolExecutionError("expected locator in owner/repo#123 format")
    return (
        match.group("owner"),
        match.group("repo"),
        int(match.group("number")),
    )


def require_string(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError(f"{key} is required")
    return value.strip()


def maybe_add_flag(command: list[str], flag: str, enabled: Any) -> None:
    if enabled:
        command.append(flag)


def maybe_add_option(command: list[str], flag: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            command.extend([flag, value])
        return
    command.extend([flag, str(value)])


def format_command_for_logs(command: list[str]) -> str:
    sanitized: list[str] = []
    redact_next = False
    redact_last_arg = False
    for index, token in enumerate(command):
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        if token in SENSITIVE_FLAGS:
            sanitized.append(token)
            redact_next = True
            continue
        if token in {"issue", "pr"} and index + 1 < len(command) and command[index + 1] == "comment":
            sanitized.append(token)
            redact_last_arg = True
            continue
        sanitized.append(token)

    if redact_last_arg and len(sanitized) >= 1:
        sanitized[-1] = "<redacted>"
    return " ".join(shlex_quote(part) for part in sanitized)


def shlex_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=+-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


class FjMcpService:
    def __init__(self, runner: FjRunner, approval_token_env: str, current_dir: str, logger: StderrLogger) -> None:
        self.runner = runner
        self.approval_token_env = approval_token_env
        self.current_dir = current_dir
        self.logger = logger

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        handlers = {
            "discover_pull_requests": self.discover_pull_requests,
            "view_pull_request": self.view_pull_request,
            "create_pull_request": self.create_pull_request,
            "close_pull_request": self.close_pull_request,
            "merge_pull_request": self.merge_pull_request,
            "approve_pull_request": self.approve_pull_request,
            "discover_repositories": self.discover_repositories,
            "view_repository": self.view_repository,
            "discover_issues": self.discover_issues,
            "view_issue": self.view_issue,
            "create_issue": self.create_issue,
            "comment_on_issue": self.comment_on_issue,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ToolExecutionError(f"unknown tool: {name}")
        self.logger.info(
            f"tool call started name={name} arg_keys={sorted(arguments.keys())}"
        )
        payload = handler(arguments)
        self.logger.info(
            f"tool call finished name={name} is_error={payload['is_error']}"
        )
        return payload["is_error"], json.dumps(payload["data"], indent=2, ensure_ascii=False)

    def _run_fj(
        self,
        command: list[str],
        arguments: dict[str, Any],
        *,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        host = arguments.get("host")
        if host is not None and (not isinstance(host, str) or not host.strip()):
            raise ToolExecutionError("host must be a non-empty string when provided")
        result = self.runner.run(command, host=host, cwd=cwd)
        return {"is_error": result.exit_code != 0, "data": result.to_payload()}

    def discover_pull_requests(self, arguments: dict[str, Any]) -> dict[str, Any]:
        repo = require_string(arguments, "repo")
        command = ["pr", "search", "--repo", repo]
        maybe_add_option(command, "--labels", normalize_labels(arguments.get("labels")))
        maybe_add_option(command, "--creator", arguments.get("creator"))
        maybe_add_option(command, "--assignee", arguments.get("assignee"))
        maybe_add_option(command, "--state", arguments.get("state"))
        query = arguments.get("query")
        if query is not None:
            if not isinstance(query, str):
                raise ToolExecutionError("query must be a string when provided")
            command.append(query)
        return self._run_fj(command, arguments)

    def view_pull_request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        locator = require_string(arguments, "pull_request")
        view_name = arguments.get("view", "body")
        command = ["pr", "view", locator]
        if view_name == "comments":
            command.append("comments")
        elif view_name == "comment":
            comment_index = arguments.get("comment_index")
            if not isinstance(comment_index, int) or comment_index < 1:
                raise ToolExecutionError("comment_index must be a positive integer when view=comment")
            command.extend(["comment", str(comment_index)])
        elif view_name == "labels":
            command.append("labels")
        elif view_name == "files":
            command.append("files")
        elif view_name == "commits":
            command.append("commits")
            if arguments.get("oneline"):
                command.append("--oneline")
        elif view_name != "body":
            raise ToolExecutionError("unsupported pull request view")
        return self._run_fj(command, arguments)

    def create_pull_request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        command = ["pr"]
        remote = arguments.get("remote")
        if remote is not None:
            if not isinstance(remote, str) or not remote.strip():
                raise ToolExecutionError("remote must be a non-empty string when provided")
            command.extend(["-R", remote])
        command.append("create")
        maybe_add_option(command, "--base", arguments.get("base"))
        maybe_add_option(command, "--head", arguments.get("head"))
        maybe_add_option(command, "--body", arguments.get("body"))
        maybe_add_option(command, "--repo", arguments.get("repo"))
        maybe_add_flag(command, "--autofill", arguments.get("autofill"))
        maybe_add_flag(command, "--web", arguments.get("web"))
        maybe_add_flag(command, "--agit", arguments.get("agit"))
        title = arguments.get("title")
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                raise ToolExecutionError("title must be a non-empty string when provided")
            command.append(title)

        use_current_repo = bool(arguments.get("use_current_repo"))
        workdir = arguments.get("workdir")
        resolved_cwd: str | None = None
        if workdir is not None:
            if not isinstance(workdir, str) or not workdir.strip():
                raise ToolExecutionError("workdir must be a non-empty string when provided")
            resolved_cwd = workdir
        elif use_current_repo:
            resolved_cwd = self.current_dir
        return self._run_fj(command, arguments, cwd=resolved_cwd)

    def close_pull_request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        locator = require_string(arguments, "pull_request")
        command = ["pr", "close", locator]
        maybe_add_option(command, "--with-msg", arguments.get("message"))
        return self._run_fj(command, arguments)

    def merge_pull_request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        locator = require_string(arguments, "pull_request")
        command = ["pr", "merge", locator]
        maybe_add_option(command, "--method", arguments.get("method"))
        maybe_add_flag(command, "--delete", arguments.get("delete_branch"))
        maybe_add_option(command, "--title", arguments.get("title"))
        maybe_add_option(command, "--message", arguments.get("message"))
        return self._run_fj(command, arguments)

    def approve_pull_request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        host = arguments.get("host") or self.runner.default_host
        if not isinstance(host, str) or not host.strip():
            raise ToolExecutionError("host is required for approve_pull_request unless the server has a default host")

        token = os.environ.get(self.approval_token_env)
        if not token:
            raise ToolExecutionError(
                f"missing approval token: set environment variable {self.approval_token_env}"
            )

        owner, repo, number = parse_locator(require_string(arguments, "pull_request"))
        body = {"event": "APPROVE"}
        if arguments.get("body") is not None:
            if not isinstance(arguments["body"], str):
                raise ToolExecutionError("body must be a string when provided")
            body["body"] = arguments["body"]

        base_url = normalize_host(host)
        url = (
            f"{base_url}/api/v1/repos/{urllib.parse.quote(owner)}/"
            f"{urllib.parse.quote(repo)}/pulls/{number}/reviews"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"token {token}",
            },
        )
        self.logger.info(f"submitting PR approval request url={url}")
        try:
            with urllib.request.urlopen(request) as response:
                raw_body = response.read().decode("utf-8")
                parsed_body = json.loads(raw_body) if raw_body else None
                self.logger.info(f"approval request succeeded status={response.status} url={url}")
                return {
                    "is_error": False,
                    "data": {
                        "url": url,
                        "status": response.status,
                        "response": parsed_body,
                    },
                }
        except urllib.error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed_body = json.loads(raw_body) if raw_body else None
            except json.JSONDecodeError:
                parsed_body = raw_body
            self.logger.error(f"approval request failed status={exc.code} url={url}")
            return {
                "is_error": True,
                "data": {
                    "url": url,
                    "status": exc.code,
                    "response": parsed_body,
                },
            }

    def discover_repositories(self, arguments: dict[str, Any]) -> dict[str, Any]:
        owner_type = arguments.get("owner_type", "current_user")
        page = arguments.get("page", 1)
        if not isinstance(page, int) or page < 1:
            raise ToolExecutionError("page must be a positive integer")

        if owner_type == "org":
            owner = require_string(arguments, "owner")
            command = ["org", "repo", "list", "--page", str(page), owner]
        elif owner_type in {"current_user", "user"}:
            command = ["user", "repos", "--page", str(page)]
            maybe_add_flag(command, "--starred", arguments.get("starred"))
            maybe_add_option(command, "--sort", arguments.get("sort"))
            owner = arguments.get("owner")
            if owner_type == "user" and owner:
                if not isinstance(owner, str) or not owner.strip():
                    raise ToolExecutionError("owner must be a non-empty string when provided")
                command.append(owner)
        else:
            raise ToolExecutionError("owner_type must be one of: current_user, user, org")
        return self._run_fj(command, arguments)

    def view_repository(self, arguments: dict[str, Any]) -> dict[str, Any]:
        repo = require_string(arguments, "repo")
        return self._run_fj(["repo", "view", repo], arguments)

    def discover_issues(self, arguments: dict[str, Any]) -> dict[str, Any]:
        repo = require_string(arguments, "repo")
        command = ["issue", "search", "--repo", repo]
        maybe_add_option(command, "--labels", normalize_labels(arguments.get("labels")))
        maybe_add_option(command, "--creator", arguments.get("creator"))
        maybe_add_option(command, "--assignee", arguments.get("assignee"))
        maybe_add_option(command, "--state", arguments.get("state"))
        query = arguments.get("query")
        if query is not None:
            if not isinstance(query, str):
                raise ToolExecutionError("query must be a string when provided")
            command.append(query)
        return self._run_fj(command, arguments)

    def view_issue(self, arguments: dict[str, Any]) -> dict[str, Any]:
        locator = require_string(arguments, "issue")
        view_name = arguments.get("view", "body")
        command = ["issue", "view", locator]
        if view_name == "comments":
            command.append("comments")
        elif view_name == "comment":
            comment_index = arguments.get("comment_index")
            if not isinstance(comment_index, int) or comment_index < 1:
                raise ToolExecutionError("comment_index must be a positive integer when view=comment")
            command.extend(["comment", str(comment_index)])
        elif view_name != "body":
            raise ToolExecutionError("unsupported issue view")
        return self._run_fj(command, arguments)

    def create_issue(self, arguments: dict[str, Any]) -> dict[str, Any]:
        repo = require_string(arguments, "repo")
        title = require_string(arguments, "title")
        command = ["issue", "create", "--repo", repo]
        maybe_add_option(command, "--body", arguments.get("body"))
        maybe_add_option(command, "--template", arguments.get("template"))
        maybe_add_flag(command, "--no-template", arguments.get("no_template"))
        maybe_add_flag(command, "--web", arguments.get("web"))
        command.append(title)
        return self._run_fj(command, arguments)

    def comment_on_issue(self, arguments: dict[str, Any]) -> dict[str, Any]:
        issue = require_string(arguments, "issue")
        body = require_string(arguments, "body")
        return self._run_fj(["issue", "comment", issue, body], arguments)


def normalize_host(host: str) -> str:
    raw = host.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.netloc:
        parsed = urllib.parse.urlsplit(f"https://{raw}")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def encode_tool_result(is_error: bool, text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class McpServer:
    def __init__(self, service: FjMcpService, logger: StderrLogger) -> None:
        self.service = service
        self.logger = logger

    def handle_request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        self.logger.info(f"received MCP message method={method} id={request_id}")

        if method == "initialize":
            return success_response(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fj-mcp", "version": __version__},
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return success_response(request_id, {})
        if method == "tools/list":
            return success_response(request_id, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params", {})
            tool_name = params.get("name")
            arguments = params.get("arguments", {}) or {}
            if not isinstance(tool_name, str):
                return error_response(request_id, -32602, "tools/call requires a string tool name")
            if not isinstance(arguments, dict):
                return error_response(request_id, -32602, "tools/call arguments must be an object")
            try:
                is_error, text = self.service.call_tool(tool_name, arguments)
                return success_response(request_id, encode_tool_result(is_error, text))
            except ToolExecutionError as exc:
                self.logger.error(f"tool call failed name={tool_name} error={exc}")
                return success_response(request_id, encode_tool_result(True, str(exc)))
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"tool call crashed name={tool_name} error={exc}")
                return error_response(request_id, -32603, f"tool execution failed: {exc}")
        if request_id is None:
            return None
        self.logger.error(f"method not found method={method} id={request_id}")
        return error_response(request_id, -32601, f"method not found: {method}")


def read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None if not headers else None
        if line in {b"\r\n", b"\n"}:
            break
        key, _, value = line.decode("utf-8").partition(":")
        headers[key.lower().strip()] = value.strip()

    if "content-length" not in headers:
        raise RuntimeError("missing Content-Length header")

    length = int(headers["content-length"])
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def write_message(message: dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    LOGGER.debug(
        f"sent MCP message id={message.get('id')} has_result={'result' in message} has_error={'error' in message}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forgejo MCP server backed by fj.")
    parser.add_argument(
        "--default-host",
        default=os.environ.get("FJ_MCP_DEFAULT_HOST"),
        help="Default Forgejo host, for example git.skivent.co",
    )
    parser.add_argument(
        "--approval-token-env",
        default=os.environ.get("FJ_MCP_APPROVAL_TOKEN_ENV", "FORGEJO_TOKEN"),
        help="Environment variable that stores the Forgejo token used for PR approvals.",
    )
    parser.add_argument(
        "--fj-bin",
        default=os.environ.get("FJ_BIN", "fj"),
        help="Path to the fj executable.",
    )
    parser.add_argument(
        "--neutral-cwd",
        default=os.environ.get("FJ_MCP_NEUTRAL_CWD", tempfile.gettempdir()),
        help="Working directory used for fj commands that should not depend on the current repository.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("FJ_MCP_LOG_LEVEL", "info"),
        choices=sorted(VALID_LOG_LEVELS),
        help="stderr log verbosity.",
    )
    return parser.parse_args(argv)


def run_stdio_server(server: McpServer, logger: StderrLogger) -> int:
    logger.info("stdio server loop started")
    while True:
        try:
            message = read_message()
            if message is None:
                logger.info("stdin closed, shutting down")
                return 0
            response = server.handle_request(message)
            if response is not None:
                write_message(response)
        except Exception:  # noqa: BLE001
            logger.error("fatal exception in stdio loop")
            traceback.print_exc(file=sys.stderr)
            return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global LOGGER
    LOGGER = StderrLogger(args.log_level)
    LOGGER.info(
        "starting fj-mcp "
        f"version={__version__} default_host={args.default_host or '<unset>'} "
        f"fj_bin={args.fj_bin} neutral_cwd={args.neutral_cwd}"
    )
    runner = FjRunner(
        fj_bin=args.fj_bin,
        neutral_cwd=args.neutral_cwd,
        default_host=args.default_host,
        logger=LOGGER,
    )
    service = FjMcpService(
        runner=runner,
        approval_token_env=args.approval_token_env,
        current_dir=os.getcwd(),
        logger=LOGGER,
    )
    server = McpServer(service, LOGGER)
    return run_stdio_server(server, LOGGER)
