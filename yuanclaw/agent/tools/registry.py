"""Tool registry for dynamic tool management."""

from dataclasses import dataclass
from typing import Any

from yuanclaw.agent.tools.base import Tool


@dataclass(frozen=True)
class ToolExecutionResult:
    """Structured execution status while preserving provider-facing content."""

    content: Any
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def from_content(cls, content: Any) -> "ToolExecutionResult":
        if isinstance(content, str) and content.lstrip().lower().startswith("error"):
            return cls(content=content, error=content)
        if isinstance(content, dict):
            error = content.get("error")
            if isinstance(error, str) and error.strip():
                return cls(content=content, error=error.strip())
        return cls(content=content)


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, params: dict[str, Any]) -> Any:
        """Execute a tool by name with given parameters."""
        return (await self.execute_result(name, params)).content

    async def execute_result(
        self,
        name: str,
        params: dict[str, Any],
    ) -> ToolExecutionResult:
        """Execute a tool and return explicit success/error status."""
        hint = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            error = f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"
            return ToolExecutionResult(content=error, error=error)

        try:
            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)

            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                error = f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
                return ToolExecutionResult(content=error + hint, error=error)
            result = await tool.execute(**params)
            execution = ToolExecutionResult.from_content(result)
            if execution.error:
                return ToolExecutionResult(
                    content=(result + hint) if isinstance(result, str) else result,
                    error=execution.error,
                )
            return execution
        except Exception as e:
            error = f"Error executing {name}: {str(e)}"
            return ToolExecutionResult(content=error + hint, error=error)

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
