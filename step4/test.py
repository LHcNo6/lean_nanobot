"""Tests for Step 4 — Tool base class, ToolResult, ToolRegistry, EchoTool."""

import asyncio
import unittest

from step4.tool import Tool, ToolResult, ToolRegistry
from step4.tools.echo import EchoTool


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------

class TestToolResult(unittest.TestCase):
    def test_str_value(self):
        r = ToolResult("hello")
        self.assertEqual(str(r), "hello")
        self.assertEqual(r, "hello")

    def test_error_flag(self):
        r = ToolResult("something bad", is_error=True)
        self.assertTrue(r.is_error)

    def test_error_classmethod(self):
        r = ToolResult.error("oh no")
        self.assertTrue(r.is_error)
        self.assertEqual(str(r), "oh no")

    def test_default_not_error(self):
        r = ToolResult("ok")
        self.assertFalse(r.is_error)

    def test_empty_default(self):
        r = ToolResult()
        self.assertEqual(str(r), "")
        self.assertFalse(r.is_error)


# ---------------------------------------------------------------------------
# Tool ABC
# ---------------------------------------------------------------------------

class TestToolABC(unittest.TestCase):
    def test_cannot_instantiate(self):
        with self.assertRaises(TypeError):
            Tool()  # type: ignore[abstract]

    def test_incomplete_subclass(self):
        with self.assertRaises(TypeError):
            class _(Tool):               # missing name, description, parameters
                pass
            _()

    def test_to_schema(self):
        tool = EchoTool()
        schema = tool.to_schema()
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "echo")
        self.assertIn("parameters", schema["function"])


# ---------------------------------------------------------------------------
# EchoTool
# ---------------------------------------------------------------------------

class TestEchoTool(unittest.TestCase):
    def setUp(self):
        self.tool = EchoTool()

    def test_name(self):
        self.assertEqual(self.tool.name, "echo")

    def test_description(self):
        self.assertTrue(len(self.tool.description) > 0)

    def test_parameters_have_type(self):
        params = self.tool.parameters
        self.assertEqual(params.get("type"), "object")

    def test_parameters_have_properties(self):
        params = self.tool.parameters
        self.assertIn("text", params.get("properties", {}))

    def test_parameters_have_required(self):
        params = self.tool.parameters
        self.assertIn("text", params.get("required", []))

    def test_to_schema(self):
        schema = self.tool.to_schema()
        func = schema["function"]
        self.assertEqual(func["name"], "echo")
        self.assertIn("text", func["parameters"]["properties"])


class TestEchoToolAsync(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = EchoTool()

    async def test_execute(self):
        r = await self.tool.execute(text="hello")
        self.assertEqual(str(r), "Echo: hello")
        self.assertFalse(r.is_error)

    async def test_execute_empty(self):
        r = await self.tool.execute()
        self.assertEqual(str(r), "Echo: ")


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class TestToolRegistry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.tool = EchoTool()
        self.registry.register(self.tool)

    def test_register_and_get(self):
        t = self.registry.get("echo")
        self.assertIs(t, self.tool)

    def test_has(self):
        self.assertTrue(self.registry.has("echo"))
        self.assertFalse(self.registry.has("nonexistent"))

    def test_get_definitions(self):
        defs = self.registry.get_definitions()
        self.assertEqual(len(defs), 1)
        self.assertEqual(defs[0]["function"]["name"], "echo")

    async def test_execute(self):
        r = await self.registry.execute("echo", text="hi")
        self.assertEqual(str(r), "Echo: hi")

    async def test_execute_not_found(self):
        r = await self.registry.execute("nonexistent", foo="bar")
        self.assertTrue(r.is_error)
        self.assertIn("not found", str(r).lower())

    def test_unregister(self):
        self.registry.unregister("echo")
        self.assertIsNone(self.registry.get("echo"))
        self.assertEqual(len(self.registry.get_definitions()), 0)

    def test_register_overwrites(self):
        class OtherTool(Tool):
            name = "echo"
            description = "other"
            parameters = {}
            async def execute(self, **kwargs):
                return ToolResult("other")
        other = OtherTool()
        self.registry.register(other)
        self.assertIs(self.registry.get("echo"), other)


if __name__ == "__main__":
    asyncio.run(unittest.main())
