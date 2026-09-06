"""A pydantic-ai model that runs the local `claude` binary on the subscription.

Why this exists: the dev loop for the review agent should cost nothing per token.
Every iteration on a prompt or a tool payload is a full run against a real pack,
and at API rates that was a dollar a go. The Claude Pro/Max subscription is
already paid for, and `claude -p` reaches it. The published adapter
(`pydantic-ai-claude-code`) was written for pydantic-ai 1.x and does not load
against 2.x, so trd carries its own — it is short, and it needs nothing installed
beyond the binary.

How it works. pydantic-ai drives the agent loop and hands this class one
request at a time: the conversation so far, the tools on offer, and the output
tool the answer must come back through. `claude -p` has no function-calling
API, so the tools are put in the system prompt as a catalogue and the reply is
constrained with `--json-schema` to a list of tool calls — the binary's own
structured-output mode validates it before it comes back. Each request is a
fresh process: the transcript is re-rendered in full every turn rather than
resumed, because a stateless turn is easier to reason about than a session id,
and on the subscription the resent tokens cost nothing.

What it deliberately does not do:

- It never lets the binary use its own tools (`--tools ""`), load MCP servers
  (`--strict-mcp-config`), or read settings and CLAUDE.md files
  (`--setting-sources ""`, a scratch working directory). The model gets trd's
  read-only tool surface and nothing else, the same as through the API.
- It strips ANTHROPIC_API_KEY from the child's environment. With a key present
  the binary bills the key, silently, and the run would cost exactly what this
  adapter exists to avoid.
- It records what the binary reports — tokens, cache reads, the nominal cost —
  so a run through it is measured the same way as one through the API.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai._instrumentation import get_instructions
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage

from trd.errors import TrdError

DEFAULT_MODEL_NAME = "opus"
DEFAULT_TIMEOUT_S = 600

# Environment the child must not inherit. The key would switch billing to the
# API; the CLAUDECODE markers make a nested binary think it is inside a session.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")

PROTOCOL = """
You work through tools. You cannot run code, read files or browse; the only
tools that exist are the ones catalogued below, and you reach them by replying
with a JSON object of the form {"calls": [{"tool": <name>, "args": {...}}, ...]}
and nothing else. Every call in one reply runs in parallel and the results come
back in the next message. To finish, call the tool named `{output_tool}` with the
answer as its arguments; that is the only way to answer, and once you have what
you need, do so.

Tools:
"""


def _schema_for(tools: list[ToolDefinition]) -> dict[str, Any]:
    """The reply shape, enforced by the binary: a list of calls to known tools."""
    return {
        "type": "object",
        "properties": {
            "calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "enum": [t.name for t in tools]},
                        "args": {"type": "object"},
                    },
                    "required": ["tool", "args"],
                },
            }
        },
        "required": ["calls"],
    }


def _catalogue(tools: list[ToolDefinition]) -> str:
    lines = []
    for t in tools:
        lines.append(
            f"### {t.name}\n{t.description or ''}\nargs schema: "
            f"{json.dumps(t.parameters_json_schema, separators=(',', ':'))}"
        )
    return "\n\n".join(lines)


def render_transcript(messages: list[ModelMessage]) -> str:
    """The conversation so far as text, oldest first, for a binary with no
    message API. Tool results are the JSON pydantic-ai would have sent."""
    out: list[str] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, SystemPromptPart):
                    continue  # carried in --system-prompt
                if isinstance(part, UserPromptPart):
                    content = part.content if isinstance(part.content, str) else str(part.content)
                    out.append(f"[user]\n{content}")
                elif isinstance(part, ToolReturnPart):
                    out.append(
                        f"[result of {part.tool_name} #{part.tool_call_id}]\n"
                        f"{part.model_response_str()}"
                    )
                elif isinstance(part, RetryPromptPart):
                    out.append(f"[error]\n{part.model_response()}")
        elif isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    out.append(
                        f"[you called {part.tool_name} #{part.tool_call_id}]\n"
                        f"{part.args_as_json_str()}"
                    )
                elif isinstance(part, TextPart):
                    out.append(f"[you said]\n{part.content}")
    out.append("[user]\nYour next reply: tool calls, or the answer through the output tool.")
    return "\n\n".join(out)


def _usage_from(report: dict[str, Any]) -> RequestUsage:
    usage = report.get("usage") or {}
    read = int(usage.get("cache_read_input_tokens") or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    uncached = int(usage.get("input_tokens") or 0)
    return RequestUsage(
        input_tokens=uncached + read + write,
        cache_read_tokens=read,
        cache_write_tokens=write,
        output_tokens=int(usage.get("output_tokens") or 0),
    )


@dataclass(init=False)
class ClaudeCodeModel(Model):
    """`claude -p` as a pydantic-ai model. Model name is the binary's alias or id."""

    _model_name: str = field(repr=False)
    binary: str = field(default="claude", repr=False)
    timeout_s: float = field(default=DEFAULT_TIMEOUT_S, repr=False)
    _calls: int = field(default=0, repr=False)

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        *,
        binary: str = "claude",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        settings: ModelSettings | None = None,
    ) -> None:
        super().__init__(settings=settings)
        self._model_name = model_name or DEFAULT_MODEL_NAME
        self.binary = binary
        self.timeout_s = timeout_s
        self._calls = 0

    @property
    def model_name(self) -> str:
        return f"claude-code:{self._model_name}"

    @property
    def system(self) -> str:
        return "claude-code"

    @property
    def provider(self) -> None:
        return None

    # ------------------------------------------------------------ the request

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        model_settings, params = self.prepare_request(model_settings, model_request_parameters)
        tools = [*params.declared_function_tools, *params.output_tools]
        output_tool = params.output_tools[0].name if params.output_tools else "final_result"
        system = (
            (get_instructions(messages, params) or "")
            + "\n"
            + PROTOCOL.replace("{output_tool}", output_tool)
            + _catalogue(tools)
        )
        report = await self._run(system, render_transcript(messages), _schema_for(tools))
        parts: list[ModelResponsePart] = []
        structured = report.get("structured_output") or {}
        for i, call in enumerate(structured.get("calls") or []):
            self._calls += 1
            parts.append(
                ToolCallPart(
                    tool_name=str(call.get("tool")),
                    args=call.get("args") or {},
                    tool_call_id=f"cc_{self._calls}_{i}",
                )
            )
        if not parts:
            # No structured calls: hand the text back and let pydantic-ai ask
            # again, the same way it would for any model that answered in prose.
            parts.append(TextPart(content=str(report.get("result") or "")))
        return ModelResponse(
            parts=parts,
            usage=_usage_from(report),
            model_name=self.model_name,
            provider_name=self.system,
        )

    async def _run(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """One turn: one process. Overridden in tests with a canned report."""
        if shutil.which(self.binary) is None:
            raise TrdError(
                f"'{self.binary}' is not on PATH. 'claude-code:' runs the Claude Code binary "
                "on the subscription; install it or point TRD_AI_MODEL at a provider."
            )
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
        argv = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
            "--tools",
            "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--model",
            self._model_name,
            "--system-prompt",
            system,
        ]
        with tempfile.TemporaryDirectory(prefix="trd-claude-code-") as cwd:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), timeout=self.timeout_s
                )
            except TimeoutError:
                proc.kill()
                raise TrdError(f"claude -p did not answer within {self.timeout_s:.0f}s") from None
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[:500]
            raise TrdError(f"claude -p exited {proc.returncode}: {detail}")
        try:
            report = json.loads(stdout.decode())
        except ValueError:
            raise TrdError(
                f"claude -p returned something other than JSON: {stdout[:200]!r}"
            ) from None
        if report.get("is_error"):
            raise TrdError(f"claude -p reported an error: {str(report.get('result'))[:500]}")
        return report
