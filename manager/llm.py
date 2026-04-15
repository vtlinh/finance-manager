import anthropic

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def llm_structured(prompt: str, schema: dict, tool_name: str = "output") -> dict:
    """Call the LLM and return structured output as a Python dict.

    Uses tool use with tool_choice={"type": "tool"} to guarantee the model
    returns data matching *schema* (a JSON Schema object with type "object").
    Returns the tool's input dict — never raises a JSON parse error.
    """
    tool = {
        "name": tool_name,
        "description": "Return the structured output",
        "input_schema": schema,
    }
    with _get_client().messages.stream(
        model="claude-opus-4-6",
        max_tokens=128000,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    for block in message.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input

    raise ValueError(
        f"No tool_use block '{tool_name}' in LLM response "
        f"(got block types: {[b.type for b in message.content]})"
    )
