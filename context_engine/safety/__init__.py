"""Safety helpers — prompt-injection sanitiser + future content filters.

Day 23 ships only the prompt-injection sanitiser. Phase 6 may add a PII
redactor and a profanity filter; both belong here because they share the
"untrusted customer text → LLM prompt" pipeline.
"""

from context_engine.safety.prompt_injection import (
    InjectionFlag,
    SanitizedText,
    sanitize_for_prompt,
)

__all__ = ["InjectionFlag", "SanitizedText", "sanitize_for_prompt"]
