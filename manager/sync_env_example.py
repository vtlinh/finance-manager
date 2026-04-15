"""Sync .env -> .env.example by replacing values with placeholders.
Called automatically by a Claude Code PostToolUse hook.
Reads the hook's stdin JSON to check which file was edited.
"""
import json
import os
import sys

PLACEHOLDERS = {
    "MONARCH_EMAIL": "your@email.com",
    "MONARCH_PASSWORD": "yourpassword",
    "ANTHROPIC_API_KEY": "your-api-key-here",
}


def placeholder(key: str) -> str:
    return PLACEHOLDERS.get(key, f"your-{key.lower().replace('_', '-')}-here")


def sync() -> None:
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(script_dir, ".env")
    example_path = os.path.join(script_dir, ".env.example")

    lines = []
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "=" in line and not line.startswith("#"):
                key = line.split("=", 1)[0].strip()
                lines.append(f"{key}={placeholder(key)}")
            else:
                lines.append(line)

    with open(example_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(".env.example synced.")


def main() -> None:
    # When called from a hook, stdin contains the hook's JSON payload
    if not sys.stdin.isatty():
        try:
            data = json.load(sys.stdin)
            file_path = data.get("tool_input", {}).get("file_path", "").replace("\\", "/")
            if not (file_path.endswith("/.env") or file_path == ".env"):
                return
        except (json.JSONDecodeError, AttributeError):
            pass  # Called directly (not from hook) — run unconditionally

    sync()


if __name__ == "__main__":
    main()
