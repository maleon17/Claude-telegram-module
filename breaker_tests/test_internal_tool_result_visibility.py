"""Regression test for keeping permission denials in Jarvis's context only."""

import importlib.util
import sys

sys.path.insert(0, "/home/mishin/codex-jarvis")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    claude = _load(
        "/home/mishin/claude-jarvis/claude_watcher.py",
        "claude_watcher_visibility_test",
    )
    codex = _load(
        "/home/mishin/codex-jarvis/codex_ask_watcher.py",
        "codex_ask_watcher_visibility_test",
    )

    claude_denial = f"{claude.INTERNAL_TOOL_RESULT_PREFIX} действие заблокировано"
    codex_denial = f"{codex.INTERNAL_TOOL_RESULT_PREFIX} действие заблокировано"
    assert claude._is_internal_tool_result(claude_denial)
    assert claude.INTERNAL_TOOL_RESULT_PREFIX in claude.BASE_PERSONA
    assert claude.INTERNAL_TOOL_RESULT_PREFIX in claude.ANATOLY_PERSONA
    assert codex.INTERNAL_TOOL_RESULT_PREFIX in codex.JARVIS_PROMPT
    assert codex._item_result_blocks({
        "type": "mcp_tool_call", "result": codex_denial,
    }) == []
    print("CLOSED: permission denial stays in the model context, not progress")


if __name__ == "__main__":
    main()
