"""Agent prompts: one ``<agent>.md`` file per prompt, plus the loader.

Usage::

    from app.agents.prompts import load_prompt

    template = load_prompt("writer")
    messages = template.messages(feedback=previous_feedback, platform=..., plan=...)
    version = template.version  # store in agent_steps.output_summary.prompt_version
"""

from app.agents.prompts.loader import (
    PromptError,
    PromptTemplate,
    list_prompts,
    load_prompt,
)

__all__ = ["PromptError", "PromptTemplate", "list_prompts", "load_prompt"]
