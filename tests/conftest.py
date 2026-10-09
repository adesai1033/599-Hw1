"""Shared test doubles: a scripted chat model and a tool-call message helper."""
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedChatModel(BaseChatModel):
    script: list[AIMessage]
    calls: list = Field(default_factory=list)
    bound_tools: list = Field(default_factory=list)
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        message = self.script[self.cursor]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = list(tools)
        return self


def call(name, symbol=None, call_id="c1"):
    args = {} if symbol is None else {"symbol": symbol}
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}])
