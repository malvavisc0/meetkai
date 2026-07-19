"""Tests for the ``brain_query`` tool (agent/tools/brain.py).

Uses lightweight fakes for ``MorphikClient`` and ``KaiAgent`` rather than
the real classes — this module tests the tool's async call contract and the
registration wiring (register_tool + add_tool_workflow), not Morphik's HTTP
layer (covered by ``brain/tests/test_client.py``).
"""

from llama_index.core.tools import FunctionTool

from kai.agent.tools.brain import make_brain_query_tool, register_brain_tool
from kai.brain.client import QueryReference, QueryResult
from kai.brain.config import BRAIN_TOOL_NAME


def _query_result(response: str, references: list[QueryReference] | None = None) -> QueryResult:
    return QueryResult(response=response, references=references or [])


class _FakeMorphikClient:
    """Records calls; returns a canned QueryResult or raises.

    Structurally satisfies the ``_BrainQueryClient`` Protocol in
    ``agent/tools/brain.py`` — only ``query`` is exercised by the tool.
    """

    def __init__(
        self,
        result: QueryResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.result = result if result is not None else _query_result("")
        self.exc = exc
        self.calls: list[dict[str, str]] = []

    async def query(self, *, query: str, workspace: str, mode: str = "mix") -> QueryResult:
        self.calls.append({"query": query, "workspace": workspace, "mode": mode})
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeAgent:
    """Structurally satisfies the ``_BrainToolAgent`` Protocol."""

    def __init__(self) -> None:
        self.registered_tools: list[FunctionTool] = []
        self.workflows: list[str] = []

    def register_tool(self, tool: FunctionTool) -> None:
        self.registered_tools.append(tool)

    def add_tool_workflow(self, workflow: str | None) -> None:
        if workflow is not None:
            self.workflows.append(workflow)


class TestMakeBrainQueryTool:
    def test_tool_name_matches_constant(self):
        client = _FakeMorphikClient(result=_query_result(response="ok"))
        tool = make_brain_query_tool(client, workspace="kai-test")
        assert tool.metadata.name == BRAIN_TOOL_NAME

    async def test_calls_client_with_workspace_and_mode(self):
        client = _FakeMorphikClient(result=_query_result(response="answer"))
        tool = make_brain_query_tool(client, workspace="kai-test")
        result = await tool.acall(query="how do I reset my password?", mode="hybrid")
        assert client.calls == [
            {"query": "how do I reset my password?", "workspace": "kai-test", "mode": "hybrid"}
        ]
        assert "answer" in str(result)

    async def test_default_mode_used_when_invalid(self):
        client = _FakeMorphikClient(result=_query_result(response="answer"))
        tool = make_brain_query_tool(client, workspace="kai-test", default_mode="mix")
        await tool.acall(query="q", mode="not-a-real-mode")
        assert client.calls[0]["mode"] == "mix"

    async def test_empty_query_returns_error_without_calling_client(self):
        client = _FakeMorphikClient(result=_query_result(response="answer"))
        tool = make_brain_query_tool(client, workspace="kai-test")
        result = await tool.acall(query="   ")
        assert "Error" in str(result)
        assert client.calls == []

    async def test_no_relevant_info(self):
        client = _FakeMorphikClient(result=_query_result(response=""))
        tool = make_brain_query_tool(client, workspace="kai-test")
        result = await tool.acall(query="anything")
        assert "no relevant information" in str(result).lower()

    async def test_references_appended_as_sources(self):
        client = _FakeMorphikClient(
            result=_query_result(
                response="30 days",
                references=[
                    QueryReference(file_path="refund-policy.pdf"),
                    QueryReference(file_path="refund-policy.pdf"),
                    QueryReference(file_path="faq.md"),
                ],
            )
        )
        tool = make_brain_query_tool(client, workspace="kai-test")
        result = await tool.acall(query="what is the refund window?")
        text = str(result)
        assert "30 days" in text
        assert "refund-policy.pdf" in text
        assert "faq.md" in text
        # deduped: refund-policy.pdf should appear once in the Sources list
        assert text.count("refund-policy.pdf") == 1

    async def test_client_exception_surfaced_as_error_string(self):
        client = _FakeMorphikClient(exc=RuntimeError("boom"))
        tool = make_brain_query_tool(client, workspace="kai-test")
        result = await tool.acall(query="q")
        assert "Error" in str(result)
        assert "boom" in str(result)


class TestRegisterBrainTool:
    def test_registers_tool_and_sets_workflow(self):
        agent = _FakeAgent()
        client = _FakeMorphikClient(result=_query_result(response="ok"))
        register_brain_tool(
            agent,
            client,
            workspace="kai-test",
            instruction="how to do X from section Y",
            mandatory=True,
        )
        assert len(agent.registered_tools) == 1
        assert agent.registered_tools[0].metadata.name == BRAIN_TOOL_NAME
        assert len(agent.workflows) == 1
        assert "MUST" in agent.workflows[0]
        assert "how to do X from section Y" in agent.workflows[0]

    def test_registers_general_awareness_even_without_instruction(self):
        agent = _FakeAgent()
        client = _FakeMorphikClient(result=_query_result(response="ok"))
        register_brain_tool(agent, client, workspace="kai-test")
        assert agent.workflows[0]  # never empty
        assert BRAIN_TOOL_NAME in agent.workflows[0]
