import os
import sys
import time

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


def llm_batch_structured(
    prompts: list[tuple[str, str]],
    schema: dict,
    tool_name: str = "output",
    model: str = "claude-opus-4-6",
    poll_interval: int = 30,
) -> dict[str, dict]:
    """Submit multiple prompts and return structured results as a dict.

    In production (USE_BATCH_LLM=1): uses the Message Batches API (~50% cheaper,
    parallel processing) and polls until complete.
    In development (default): runs requests sequentially with llm_structured so
    results arrive immediately without waiting for a batch to end.

    Args:
        prompts: List of (custom_id, prompt) pairs. custom_id must be unique.
        schema: JSON Schema object for the tool's structured output.
        tool_name: Name of the tool used to enforce structured output.
        model: Model to use for all requests.
        poll_interval: Seconds between batch status polls (production only).

    Returns:
        Dict mapping custom_id -> result dict. Failed requests are omitted.
    """
    if not os.environ.get("USE_BATCH_LLM"):
        # Development: call one-by-one for immediate feedback
        results: dict[str, dict] = {}
        for custom_id, prompt in prompts:
            try:
                results[custom_id] = llm_structured(prompt, schema, tool_name)
            except Exception as exc:
                print(f"  Warning: request '{custom_id}' failed: {exc}")
        return results

    # Production: submit a single batch and poll until complete
    tool = {
        "name": tool_name,
        "description": "Return the structured output",
        "input_schema": schema,
    }

    requests = [
        {
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": 4096,
                "tools": [tool],
                "tool_choice": {"type": "tool", "name": tool_name},
                "messages": [{"role": "user", "content": prompt}],
            },
        }
        for custom_id, prompt in prompts
    ]

    client = _get_client()
    batch = client.messages.batches.create(requests=requests)
    print(f"Batch submitted ({len(requests)} requests) — this may take a few minutes.", flush=True)

    is_tty = sys.stdout.isatty()
    dots = 0
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            if is_tty:
                print()  # newline after inline dots
            else:
                print("\r" + "." * dots, flush=True)  # flush final dot line
            break
        dots = dots % 10 + 1
        if is_tty:
            if dots == 1:
                print("\r" + " " * 10 + "\r.", end="", flush=True)
            else:
                print(".", end="", flush=True)
        else:
            # Pipe mode: prefix \r so the server can emit an "update last line" SSE event
            print("\r" + "." * dots, flush=True)
        time.sleep(poll_interval)

    results: dict[str, dict] = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            print(f"  Warning: request '{result.custom_id}' failed ({result.result.type})")
            continue
        for block in result.result.message.content:
            if block.type == "tool_use" and block.name == tool_name:
                results[result.custom_id] = block.input
                break

    return results
