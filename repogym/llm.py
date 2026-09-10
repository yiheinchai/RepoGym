"""Optional LLM enrichment: rewrite raw engineer prompts into a clean, solution-free task.

Off by default. Enable with `repogym config set llm.enabled true` or `repogym build --llm`.
Requires the `llm` extra (`pip install repogym[llm]`) and Anthropic credentials in the
environment (ANTHROPIC_API_KEY, or an `ant auth login` profile).

Note for privacy-conscious deployments: this sends prompts and the *source* diff to the model
provider. Everything else in RepoGym stays on your machines.
"""
from __future__ import annotations

import json
from typing import List, Optional

DEFAULT_MODEL = "claude-opus-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short imperative title, <= 80 chars"},
        "problem_statement": {
            "type": "string",
            "description": "A self-contained issue-style description of what needs to change and how success is "
                           "observed. Must not reveal the implementation, file-level edits, or test names.",
        },
        "difficulty": {"type": "string", "enum": ["trivial", "easy", "medium", "hard"]},
        "category": {"type": "string", "enum": ["bugfix", "feature", "refactor", "test", "docs", "infra", "perf", "other"]},
        "leaks_solution_in_prompt": {"type": "boolean",
                                     "description": "True if the engineer's raw prompt already spelled out the exact code change."},
    },
    "required": ["title", "problem_statement", "difficulty", "category", "leaks_solution_in_prompt"],
    "additionalProperties": False,
}

SYSTEM = (
    "You turn a software engineer's conversation with a coding agent into a reinforcement-learning task. "
    "You are given the engineer's raw prompts and the diff they ended up with. Write the task the way a good "
    "issue reporter would: describe the observed problem or desired behaviour and how to tell it is done. "
    "Do not describe the implementation, name the changed functions, or mention the tests that were added. "
    "Keep everything that is genuinely part of the request (constraints, style, scope). Write in the same "
    "language as the prompts."
)


def enrich(prompts: List[str], source_patch: str, files: List[str], language: str,
           model: Optional[str] = None) -> Optional[dict]:
    import anthropic

    client = anthropic.Anthropic()
    model = model or DEFAULT_MODEL
    diff_excerpt = source_patch if len(source_patch) < 60_000 else source_patch[:60_000] + "\n...[diff truncated]"
    user = (
        f"Language: {language}\nFiles changed: {', '.join(files[:50])}\n\n"
        "Engineer prompts, in order:\n" + "\n---\n".join(prompts) +
        "\n\nResulting source diff (for your understanding only; do not leak it):\n" + diff_excerpt
    )
    kwargs = dict(
        model=model,
        max_tokens=8000,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    try:
        # Server-side refusal fallback keeps a rare safety decline from dropping the task.
        response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except TypeError:
        response = client.messages.create(**kwargs)
    if response.stop_reason == "refusal":
        return None
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    data["model"] = model
    return data
