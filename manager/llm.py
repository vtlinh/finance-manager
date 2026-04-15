import json
from pathlib import Path

import anthropic

_DIR = Path(__file__).parent


def strip_code_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ``` or ``` ... ```) if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text[text.index("\n") + 1:]  # drop the opening ``` line
        fence_end = text.find("```")        # truncate at closing fence (and any trailing text)
        if fence_end != -1:
            text = text[:fence_end]
    return text.strip()


def llm_prompt(prompt: str):
    client = anthropic.Anthropic()
    with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=128000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
        message = stream.get_final_message()
        text = ''.join(b.text for b in message.content if b.type == "text")
        if not text:
            raise ValueError(f"No text block in LLM response (got block types: {[b.type for b in message.content]})")
        text = strip_code_fences(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            with open(_DIR / ".debug", "w", encoding="utf-8") as f:
                f.write(text)
            raise
