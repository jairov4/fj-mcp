from __future__ import annotations

import json
import unittest

from fj_mcp.server import (
    CommandResult,
    FjMcpService,
    FjRunner,
    McpServer,
    StderrLogger,
    TOOLS,
    format_command_for_logs,
    normalize_host,
    parse_locator,
)


class FakeRunner(FjRunner):
    def __init__(self) -> None:
        super().__init__(fj_bin="fj", neutral_cwd="/tmp", default_host="git.example.com", logger=StderrLogger("error"))
        self.calls: list[tuple[list[str], str | None, str | None]] = []

    def run(self, args: list[str], *, host: str | None = None, cwd: str | None = None):  # type: ignore[override]
        self.calls.append((args, host, cwd))
        return CommandResult(
            command=["fj", "--style", "minimal", *args],
            cwd=cwd or self.neutral_cwd,
            exit_code=0,
            stdout="ok\n",
            stderr="",
        )


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = FakeRunner()
        self.service = FjMcpService(
            self.runner,
            approval_token_env="MISSING_TOKEN",
            current_dir="/work/repo",
            logger=StderrLogger("error"),
        )

    def test_parse_locator(self) -> None:
        self.assertEqual(parse_locator("owner/repo#42"), ("owner", "repo", 42))

    def test_normalize_host(self) -> None:
        self.assertEqual(normalize_host("git.example.com"), "https://git.example.com")
        self.assertEqual(normalize_host("https://git.example.com/"), "https://git.example.com")

    def test_discover_pull_requests_builds_expected_command(self) -> None:
        self.service.discover_pull_requests(
            {
                "repo": "owner/repo",
                "labels": ["bug", "urgent"],
                "creator": "alice",
                "assignee": "bob",
                "state": "open",
                "query": "memory leak",
            }
        )
        args, host, cwd = self.runner.calls[-1]
        self.assertEqual(
            args,
            [
                "pr",
                "search",
                "--repo",
                "owner/repo",
                "--labels",
                "bug,urgent",
                "--creator",
                "alice",
                "--assignee",
                "bob",
                "--state",
                "open",
                "memory leak",
            ],
        )
        self.assertIsNone(host)
        self.assertIsNone(cwd)

    def test_create_pull_request_uses_local_cwd_when_requested(self) -> None:
        self.service.create_pull_request({"title": "Test", "use_current_repo": True})
        _, _, cwd = self.runner.calls[-1]
        self.assertEqual(cwd, "/work/repo")

    def test_approve_pull_request_requires_token(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing approval token"):
            self.service.approve_pull_request({"pull_request": "owner/repo#1"})

    def test_format_command_for_logs_redacts_sensitive_values(self) -> None:
        formatted = format_command_for_logs(
            ["fj", "--host", "git.example.com", "issue", "comment", "owner/repo#1", "secret body"]
        )
        self.assertIn("<redacted>", formatted)
        self.assertNotIn("secret body", formatted)


class McpServerTests(unittest.TestCase):
    def test_tools_list_contains_required_tools(self) -> None:
        tool_names = {tool["name"] for tool in TOOLS}
        self.assertTrue(
            {
                "discover_pull_requests",
                "create_pull_request",
                "close_pull_request",
                "merge_pull_request",
                "approve_pull_request",
                "discover_repositories",
                "create_issue",
                "discover_issues",
                "comment_on_issue",
            }.issubset(tool_names)
        )

    def test_initialize_response(self) -> None:
        runner = FakeRunner()
        service = FjMcpService(runner, approval_token_env="MISSING_TOKEN", current_dir="/repo", logger=StderrLogger("error"))
        server = McpServer(service, StderrLogger("error"))
        response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert response is not None
        self.assertEqual(response["result"]["serverInfo"]["name"], "fj-mcp")

    def test_tool_error_is_returned_as_tool_result(self) -> None:
        runner = FakeRunner()
        service = FjMcpService(runner, approval_token_env="MISSING_TOKEN", current_dir="/repo", logger=StderrLogger("error"))
        server = McpServer(service, StderrLogger("error"))
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "comment_on_issue", "arguments": {"issue": "owner/repo#1"}},
            }
        )
        assert response is not None
        result = response["result"]
        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("body is required", text)

    def test_tools_list_is_json_serializable(self) -> None:
        json.dumps(TOOLS)


if __name__ == "__main__":
    unittest.main()
