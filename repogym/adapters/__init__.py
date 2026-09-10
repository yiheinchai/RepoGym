"""Adapters connect coding agents to RepoGym's capture layer.

- claude_code: Claude Code hooks (SessionStart / UserPromptSubmit / PostToolUse / Stop / SessionEnd)
- codex: OpenAI Codex CLI `notify` handler (turn-complete events) and experimental hooks
- wrap: generic before/after wrapper for any command-line agent
"""
