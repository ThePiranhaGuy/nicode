#!/usr/bin/env python3
"""nicode - Enhanced TUI coding agent with Azure & NVIDIA NIM brains."""

import json
import os
import re
import subprocess

# import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# --- Dependencies ---
# uv pip install prompt_toolkit requests python-dotenv
# Optional: uv pip install ddgs  (for web search)

# --- ANSI Colors & TUI Helpers ---

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
BLUE = "\033[34m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"


def separator():
    """Draw a line matching terminal width."""
    width = os.get_terminal_size().columns
    return f"{DIM}{'─' * min(width, 100)}{RESET}"


def render_markdown(text):
    """Convert **bold** markdown to ANSI bold."""
    if not text:
        return ""
    return re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", text)


def prompt_input(prompt_text, history=None):
    """Get input with readline-like features via prompt_toolkit."""
    try:
        from prompt_toolkit import prompt as Prompt
        from prompt_toolkit.completion import PathCompleter
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.history import InMemoryHistory

        # Create history if not provided
        if history is None:
            history = InMemoryHistory()

        # Path completer for Tab
        try:
            # Newer prompt_toolkit versions
            path_completer = PathCompleter(expanduser=True)
        except TypeError:
            # Older prompt_toolkit versions
            path_completer = PathCompleter(
                expanduser=True, get_paths=lambda: [os.getcwd()]
            )

        user_input = Prompt(
            ANSI(prompt_text),
            history=history,
            completer=path_completer,
            enable_history_search=True,
            mouse_support=False,
        )

        return user_input

    except ImportError:
        # Fallback to regular input
        print(
            f"{YELLOW}⚠ prompt_toolkit not installed — using basic input "
            f"(no history, key bindings, or tab completion){RESET}"
        )
        print(f"{DIM}  Install with: uv pip install prompt_toolkit{RESET}")
        return input(prompt_text)


# --- HTTP Helpers ---


def _extract_api_error(response):
    """Best-effort extraction of the error message from an error JSON body."""
    try:
        error_data = response.json()
        if "error" in error_data:
            return error_data["error"].get("message", error_data["error"])
        if "message" in error_data:
            return error_data["message"]
    except (KeyError, ValueError):
        pass
    return response.text


def _back_off_and_retry(attempt, response=None, error=None):
    """Sleep with exponential back-off, honoring an optional Retry-After header."""
    if response is not None:
        retry_after = response.headers.get("retry-after")
        try:
            wait_time = int(retry_after) if retry_after else 2**attempt
        except (ValueError, TypeError):
            wait_time = 2**attempt
        print(
            f"{YELLOW}⚠ Error {response.status_code}{RESET}. "
            f"Retrying in {wait_time}s..."
        )
    else:
        wait_time = 2**attempt
        print(f"{RED}✗ Network error: {error}{RESET}. Retrying in {wait_time}s...")
    time.sleep(wait_time)


def request_with_retry(
    url, headers, payload, max_retries=10, is_azure=False, azure_api_version=None
):
    """POST with retry on rate limits (429), server errors (5xx), and network failures."""
    params = (
        {"api-version": azure_api_version} if is_azure and azure_api_version else None
    )
    for attempt in range(max_retries):
        try:
            response = requests.post(
                url, headers=headers, json=payload, timeout=120, params=params
            )
        except requests.exceptions.RequestException as e:
            _back_off_and_retry(attempt, error=e)
            continue

        if response.status_code == 429 or response.status_code >= 500:
            _back_off_and_retry(attempt, response=response)
            continue

        if response.status_code >= 400:
            raise ApiError(response.status_code, _extract_api_error(response), url)

        return response

    raise RuntimeError(f"Request failed after {max_retries} retries")


class ApiError(Exception):
    """Custom API error with helpful messages."""

    def __init__(self, status_code, message, url):
        self.status_code = status_code
        self.url = url
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")

    def hint(self):
        """Return helpful hint based on error."""
        if self.status_code == 400:
            return "Bad request - check your API params and message format."
        elif self.status_code == 401:
            return "Authentication failed - verify your API key."
        elif self.status_code == 403:
            return "Access forbidden - check permissions."
        elif self.status_code == 404:
            return "Resource not found - check your endpoint URL."
        elif self.status_code == 429:
            return "Rate limited - wait a moment and try again."
        elif self.status_code >= 500:
            return "Server error - check the API status page."
        return "Check API documentation for details."


# --- Exceptions ---


class AgentStop(Exception):
    """Raised when the agent should stop processing."""

    pass


# --- Brain Response Types ---


class ToolCall:
    """A tool invocation request from the brain."""

    def __init__(self, id, name, args):
        self.id = id
        self.name = name
        self.args = args


class Thought:
    """Standardized response from any Brain."""

    def __init__(self, text=None, tool_calls=None, raw_content=None, thinking=None):
        self.text = text
        self.tool_calls = tool_calls or []
        self.raw_content = raw_content
        self.thinking = thinking


# --- Memory Class ---


class Memory:
    """Persistent scratchpad for the agent."""

    def __init__(self, path=".nicode/memory.md"):
        self.path = path
        self._ensure_exists()
        self.content = self._load()

    def _ensure_exists(self):
        """Create memory file with default content if needed."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if not os.path.exists(self.path):
            default = "I am Nicode, a helpful coding assistant.\n"
            with open(self.path, "w") as f:
                f.write(default)

    def _load(self):
        """Load content from disk."""
        with open(self.path, "r") as f:
            return f.read()

    def save(self, content):
        """Update memory content and persist to disk."""
        self.content = content
        with open(self.path, "w") as f:
            f.write(content)


# --- Tool Context ---


class ToolContext:
    """What tools need to know about the agent's state."""

    def __init__(self, memory=None):
        self.memory = memory


# --- Brain Interface ---


class Brain:
    """Base class for LLM providers."""

    model = None
    deployment = None

    context_limit = 1_000_000
    last_input_tokens = 0

    def think(self, conversation):
        # Abstracted here defined for individual brains.
        raise NotImplementedError

    def _parse_response(self, data):
        """Convert API response format to Thought."""
        text_parts = []
        tool_calls = []
        thinking = None

        for block in data:
            if block["type"] == "thinking":
                thinking = block["thinking"]
            elif block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append(
                    ToolCall(id=block["id"], name=block["name"], args=block["input"])
                )

        return Thought(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            raw_content=data,
            thinking=thinking,
        )


class NvidiaNIM(Brain):
    """NVIDIA NIM API (OpenAI-compatible through NVIDIA's gateway).

    Required env vars:
      NVIDIA_NIM_API_KEY - Your NVIDIA NIM API key
      NVIDIA_NIM_MODEL   - Model name (default: "nvidia/llama-3.3-nemotron-70b-instruct")
      NVIDIA_NIM_URL     - Optional custom endpoint
    """

    context_limit = 512_000

    def __init__(self, memory=None, tools=None):
        self.memory = memory
        self.tools = tools or []
        self.system = None

        self.api_key = os.getenv("NVIDIA_NIM_API_KEY")
        if not self.api_key:
            raise ValueError("NVIDIA_NIM_API_KEY not found in .env")

        self.model = os.getenv(
            "NVIDIA_NIM_MODEL", "nvidia/llama-3.3-nemotron-70b-instruct"
        )
        self.url = os.getenv(
            "NVIDIA_NIM_URL", "https://integrate.api.nvidia.com/v1/chat/completions"
        )

        self._openai_tools = self._make_tool_entries(tools)

    def _make_tool_entries(self, tools):
        """Create OpenAI-format tool definitions."""
        entries = []
        for i, t in enumerate(tools or []):
            entries.append(
                {
                    "type": "function",
                    "id": f"call_{i}",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get(
                            "input_schema", {"type": "object", "properties": {}}
                        ),
                    },
                }
            )
        return entries

    def _to_openai_messages(self, conversation):
        """Convert nicode conversation format to OpenAI messages format."""
        msgs = []
        for msg in conversation:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                msgs.append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = []
                tool_calls = []

                for block in content:
                    if block.get("type") == "tool_result":
                        text_parts.append(f"[TOOL RESULT]: {block['content']}")
                    elif block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        args = block["input"]
                        if isinstance(args, dict):
                            args = json.dumps(args)
                        tool_calls.append(
                            {
                                "id": block["id"],
                                "type": "function",
                                "function": {"name": block["name"], "arguments": args},
                            }
                        )

                msg_obj = {"role": role}
                msg_obj["content"] = "\n".join(text_parts) if text_parts else None
                if tool_calls:
                    msg_obj["tool_calls"] = tool_calls
                msgs.append(msg_obj)
            else:
                msgs.append({"role": role, "content": str(content)})
        return msgs

    def _parse_response(self, data):
        """Convert OpenAI chat completions response to Thought."""
        choice = data["choices"][0]
        msg = choice.get("message", {})
        content = msg.get("content", "")

        tool_calls = []
        raw_content = [{"type": "text", "text": content or ""}]

        for tc in msg.get("tool_calls", []):
            func = tc["function"]
            args = func.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}

            tool_calls.append(ToolCall(id=tc["id"], name=func["name"], args=args))
            raw_content.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": func["name"],
                    "input": args,
                }
            )

        return Thought(
            text=content if content else None,
            tool_calls=tool_calls,
            raw_content=raw_content,
            thinking=None,
        )

    def think(self, conversation):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

        messages = self._to_openai_messages(conversation)
        if self.system:
            messages.insert(0, {"role": "system", "content": self.system})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0,
        }

        if self._openai_tools:
            payload["tools"] = [
                {"type": "function", "function": t["function"]}
                for t in self._openai_tools
            ]
            payload["tool_choice"] = "auto"

        response = request_with_retry(self.url, headers, payload)
        data = response.json()
        self.last_input_tokens = data.get("usage", {}).get("prompt_tokens", 0)

        return self._parse_response(data)


class AzureOpenAI(Brain):
    """Azure OpenAI API with tool/function-calling support.

    Required env vars:
      AZURE_OPENAI_API_KEY
      AZURE_OPENAI_ENDPOINT
      AZURE_OPENAI_DEPLOYMENT
      AZURE_OPENAI_API_VERSION (default: 2024-06-01)
    """

    context_limit = 128_000

    def __init__(self, memory=None, tools=None):
        self.memory = memory
        self.tools = tools or []
        self.system = None

        self.api_key = os.getenv("AZURE_OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("AZURE_OPENAI_API_KEY not found in .env")

        self.resource = os.getenv("AZURE_OPENAI_ENDPOINT")
        if not self.resource:
            raise ValueError("AZURE_OPENAI_ENDPOINT not found in .env")

        self.deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
        if not self.deployment:
            raise ValueError("AZURE_OPENAI_DEPLOYMENT not found in .env")

        self.api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")

        self._openai_tools = self._make_tool_entries(tools)

        self.url = (
            f"{self.resource.rstrip('/')}"
            f"/openai/deployments/{self.deployment}"
            f"/chat/completions"
        )

    def _make_tool_entries(self, tools):
        entries = []
        for i, t in enumerate(tools or []):
            entries.append(
                {
                    "type": "function",
                    "id": f"call_{i}",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get(
                            "input_schema", {"type": "object", "properties": {}}
                        ),
                    },
                }
            )
        return entries

    def _to_openai_messages(self, conversation):
        """Convert nicode conversation format to Azure OpenAI messages."""
        msgs = []
        for msg in conversation:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                msgs.append({"role": role, "content": content})
            elif isinstance(content, list):
                openai_content = []
                tool_calls = []

                for block in content:
                    if block.get("type") == "tool_result":
                        openai_content.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block["tool_use_id"],
                                "content": block["content"],
                                "status": "success",
                            }
                        )
                    elif block.get("type") == "text":
                        openai_content.append(
                            {"type": "text", "text": block.get("text", "")}
                        )
                    elif block.get("type") == "tool_use":
                        args = block["input"]
                        if isinstance(args, dict):
                            args = json.dumps(args)
                        tool_calls.append(
                            {
                                "id": block["id"],
                                "type": "function",
                                "function": {"name": block["name"], "arguments": args},
                            }
                        )

                msg_obj = {
                    "role": role,
                    "content": openai_content if openai_content else None,
                }
                if tool_calls:
                    msg_obj["tool_calls"] = tool_calls
                msgs.append(msg_obj)
            else:
                msgs.append({"role": role, "content": str(content)})
        return msgs

    def _parse_response(self, data):
        """Convert Azure OpenAI response to Thought."""
        choice = data["choices"][0]
        msg = choice.get("message", {})
        content = msg.get("content", "")
        thinking = msg.get("thinking")

        tool_calls = []
        raw_content = [{"type": "text", "text": content or ""}]

        if thinking:
            raw_content.append({"type": "thinking", "thinking": thinking})

        for tc in msg.get("tool_calls", []):
            func = tc["function"]
            args = func.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}

            tool_calls.append(ToolCall(id=tc["id"], name=func["name"], args=args))
            raw_content.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": func["name"],
                    "input": args,
                }
            )

        return Thought(
            text=content if content else None,
            tool_calls=tool_calls,
            raw_content=raw_content,
            thinking=thinking,
        )

    def think(self, conversation):
        headers = {"api-key": self.api_key, "content-type": "application/json"}

        messages = self._to_openai_messages(conversation)
        if self.system:
            messages.insert(0, {"role": "system", "content": self.system})

        payload = {
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0,
        }

        if self._openai_tools:
            payload["tools"] = [
                {"type": "function", "function": t["function"]}
                for t in self._openai_tools
            ]
            payload["tool_choice"] = "auto"

        thinking_param = os.getenv("AZURE_THINKING_ENABLED", "false").lower()
        if thinking_param == "true":
            payload["thinking"] = {"type": "enabled", "budget_tokens": 10000}

        response = request_with_retry(
            self.url,
            headers,
            payload,
            is_azure=True,
            azure_api_version=self.api_version,
        )
        data = response.json()
        self.last_input_tokens = data.get("usage", {}).get("prompt_tokens", 0)

        return self._parse_response(data)


# --- Tool Classes ---


class ReadFile:
    """Reads a file from the filesystem."""

    name = "read_file"
    plan_safe = True
    description = "Reads a file from the filesystem. Use this to examine code."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The path to the file"}
        },
        "required": ["path"],
    }

    def execute(self, context, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            numbered_lines = [f"{i + 1} | {line}" for i, line in enumerate(lines)]
            return "".join(numbered_lines)
        except FileNotFoundError:
            return f"Error: File not found: {path}"
        except PermissionError:
            return f"Error: Permission denied: {path}"
        except Exception as e:
            return f"Error reading file: {e}"


class WriteFile:
    """Writes content to a file."""

    name = "write_file"
    plan_safe = False
    requires_permission = True
    description = "Writes content to a file."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The path to the file"},
            "content": {"type": "string", "description": "The full content to write"},
            "confirm": {
                "type": "boolean",
                "description": "Confirm overwrite (optional)",
            },
        },
        "required": ["path", "content"],
    }

    def execute(self, context, path, content, confirm=False):
        # Check if file exists and confirm overwrite
        if os.path.exists(path) and not confirm:
            return f"CONFIRM: File '{path}' exists. Use confirm=true to overwrite."
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully wrote {len(content)} characters to {path}"
        except PermissionError:
            return f"Error: Permission denied writing to {path}"
        except Exception as e:
            return f"Error writing file: {e}"


class WritePlan:
    """Saves a plan to PLAN.md."""

    name = "write_plan"
    plan_safe = True
    description = "Saves a plan to PLAN.md. Use this to outline your approach before making changes."
    input_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The plan content in markdown"}
        },
        "required": ["content"],
    }

    def execute(self, context, content, path=None):
        # `path` is accepted but ignored — WritePlan always writes to PLAN.md.
        # This tolerates LLMs that hallucinate a path argument.
        try:
            with open("PLAN.md", "w", encoding="utf-8") as f:
                f.write(content)
            return f"Plan saved to PLAN.md ({len(content)} chars)"
        except Exception as e:
            return f"Error saving plan: {e}"


class EditFile:
    """Replaces text in a file (surgical edit)."""

    name = "edit_file"
    plan_safe = False
    requires_permission = True
    description = "Replaces specific text in a file. Use for surgical edits instead of rewriting entire files."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file"},
            "old_text": {
                "type": "string",
                "description": "Exact text to find and replace",
            },
            "new_text": {"type": "string", "description": "Text to replace it with"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def execute(self, context, path, old_text, new_text):
        if not os.path.exists(path):
            return f"Error: File not found: {path}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if old_text not in content:
                return f"Error: '{old_text[:50]}...' not found in {path}"
            count = content.count(old_text)
            if count > 1:
                return f"Error: '{old_text[:50]}...' appears {count} times. Make it unique."
            new_content = content.replace(old_text, new_text, 1)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return f"Successfully edited {path}"
        except PermissionError:
            return f"Error: Permission denied editing {path}"
        except Exception as e:
            return f"Error editing file: {e}"


class ListFiles:
    """Lists files in the project structure."""

    name = "list_files"
    plan_safe = True
    description = "Lists all files in the project structure. Useful to understand the project layout."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The root path (default '.')"}
        },
    }

    def execute(self, context, path="."):
        try:
            file_list = []
            for root, dirs, files in os.walk(path):
                dirs[:] = [
                    d
                    for d in dirs
                    if d
                    not in {".git", "__pycache__", "venv", ".nicode", "node_modules"}
                ]
                level = root.replace(path, "").count(os.sep)
                indent = " " * 4 * level
                file_list.append(f"{indent}{os.path.basename(root)}/")
                for f in files:
                    file_list.append(f"{' ' * 4 * (level + 1)}{f}")
            return "\n".join(file_list) if file_list else "No files found."
        except PermissionError:
            return f"Error: Permission denied accessing {path}"
        except Exception as e:
            return f"Error listing files: {e}"


class SearchCodebase:
    """Searches for a string in all files."""

    name = "search_codebase"
    plan_safe = True
    description = "Searches the entire codebase for a text string. Useful to find where functions or variables are defined."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The string to search for"},
            "path": {"type": "string", "description": "The root path (default '.')"},
        },
        "required": ["query"],
    }

    def execute(self, context, query, path="."):
        results = []
        try:
            for root, dirs, files in os.walk(path):
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in {".git", "__pycache__", "venv", ".nicode"}
                ]
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        with open(
                            file_path, "r", encoding="utf-8", errors="ignore"
                        ) as f:
                            for i, line in enumerate(f, 1):
                                if query.lower() in line.lower():
                                    results.append(f"{file_path}:{i}: {line.strip()}")
                    except Exception:
                        continue
            if not results:
                return f"No matches for '{query}'."
            return f"Found {len(results)} matches:\n" + "\n".join(results[:50])
        except Exception as e:
            return f"Error searching: {e}"


class SaveMemory:
    """Updates the agent's internal memory/scratchpad."""

    name = "save_memory"
    plan_safe = True
    description = "Updates your internal memory/scratchpad. Use this to remember user preferences."
    input_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The full text to save."}
        },
        "required": ["content"],
    }

    def execute(self, context, content):
        if context.memory is None:
            return "Error: Memory not available"
        context.memory.save(content)
        return f"Memory updated ({len(content)} chars)"


class RunCommand:
    """Executes shell commands."""

    name = "run_command"
    plan_safe = False
    requires_permission = True
    description = "Executes a terminal command. Use this to run scripts, tests, or install packages."
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run (e.g., 'python test.py')",
            }
        },
        "required": ["command"],
    }
    dangerous_patterns = [
        "rm -rf",
        "dd if=",
        ":(){:|:&};:",
        "chmod -R 777",
        "> /dev/",
        "mkfs",
    ]

    def execute(self, context, command):
        # Check for potentially dangerous commands
        for pattern in self.dangerous_patterns:
            if pattern.lower() in command.lower():
                return f"CONFIRM_DANGEROUS: '{pattern}' detected. Use confirm=true if intentional."

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("NICODE_TIMEOUT", "30")),
                cwd=os.getcwd(),
            )
            output = ""
            if result.stdout:
                output += f"STDOUT:\n{result.stdout}"
            if result.stderr:
                output += f"STDERR:\n{result.stderr}"
            if not output:
                output = "(No output)"
            if result.returncode != 0:
                output += f"\n(Exit code: {result.returncode})"
            return output.strip()
        except subprocess.TimeoutExpired:
            return "Error: Command timed out."
        except Exception as e:
            return f"Error executing command: {e}"


class SearchWeb:
    """Searches the internet using DuckDuckGo."""

    name = "search_web"
    plan_safe = True
    description = "Searches the internet for current information. Use when you need knowledge beyond your training data."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The search query"}},
        "required": ["query"],
    }

    def execute(self, context, query):
        try:
            from ddgs import DDGS
        except ImportError:
            return "Error: ddgs not installed. Run: uv pip install ddgs"
        try:
            results = DDGS().text(query, max_results=5)
            if not results:
                return "No results found."
            formatted = []
            for r in results:
                formatted.append(f"**{r['title']}**\n{r['href']}\n{r['body']}\n")
            return "\n".join(formatted)
        except Exception as e:
            return f"Error searching web: {e}"


# --- Tool Helpers ---


def get_tool(tools, name):
    return next((t for t in tools if t.name == name), None)


def tool_definitions(tools):
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


# --- Tools List ---

tools = [
    ReadFile(),
    WritePlan(),
    SaveMemory(),
    ListFiles(),
    SearchCodebase(),
    SearchWeb(),
    WriteFile(),
    EditFile(),
    RunCommand(),
]


# --- Agent Class ---


class Agent:
    """A coding agent with tools, memory, and enhanced UX."""

    def __init__(self, brain, tools, memory=None, mode="plan", ask_permission=True):
        self.brain = brain
        self.tools = list(tools)
        self.memory = memory
        self.mode = mode
        self.ask_permission = ask_permission
        self.conversation = []
        self.cmd_history = []  # Session-only command history
        self.brain.tools = self._tools_for_mode()
        self.brain.system = self._build_system_prompt()

    def _build_system_prompt(self):
        parts = []
        # Optional external prompt template (sibling of nicode.py).
        # Loaded only if present, so absence is a no-op.
        prompt_path = os.path.join(os.path.dirname(__file__), "SYSTEM_PROMPT.md")
        if os.path.isfile(prompt_path):
            try:
                with open(prompt_path, encoding="utf-8") as f:
                    parts.append(f.read().rstrip())
            except OSError:
                pass
        if self.memory is not None:
            parts.append(self.memory.content)
        if self.mode == "plan":
            parts.append(
                "You are in PLAN mode. You cannot write code files. Use write_plan to save your plans."
            )
        return "\n\n".join(parts)

    def _tools_for_mode(self):
        if self.mode == "act":
            return tool_definitions(self.tools)
        return tool_definitions([t for t in self.tools if t.plan_safe])

    def handle_input(self, user_input):
        """Handle user input. Returns output string, raises AgentStop to quit."""
        stripped = user_input.strip()

        if stripped == "/q":
            raise AgentStop()

        if stripped == "/c":
            self.conversation = []
            return f"{GREEN}✓{RESET} Conversation cleared"

        if stripped == "/h" or stripped == "/history":
            return self._show_history()

        if stripped == "/help":
            return self._show_help()

        if stripped.startswith("/export"):
            return self._export_conversation(stripped)

        if stripped.startswith("/mode"):
            return self._handle_mode_command(stripped)

        if not stripped:
            return ""

        # Add to session history
        self.cmd_history.append(stripped)

        self.conversation.append({"role": "user", "content": stripped})

        try:
            return self._agentic_loop()
        except ApiError as e:
            self.conversation.pop()
            return f"{RED}✗ API Error:{RESET} {e}\n{DIM}Hint: {e.hint()}{RESET}"
        except Exception as e:
            self.conversation.pop()
            return f"{RED}✗ Error:{RESET} {e}"

    def _handle_mode_command(self, user_input):
        parts = user_input.strip().split()
        if len(parts) > 1 and parts[1] == "act":
            self.mode = "act"
            self.brain.tools = self._tools_for_mode()
            self.brain.system = self._build_system_prompt()
            return f"{YELLOW}⚠️ ACT MODE{RESET} - Writing enabled"
        else:
            self.mode = "plan"
            self.brain.tools = self._tools_for_mode()
            self.brain.system = self._build_system_prompt()
            return f"{GREEN}🛡️ PLAN MODE{RESET} - Read-only"

    def _show_history(self):
        if not self.cmd_history:
            return f"{DIM}No command history{RESET}"
        lines = ["Command History:"]
        for i, cmd in enumerate(self.cmd_history[-20:], 1):  # Last 20
            cmd_preview = cmd[:60] + "..." if len(cmd) > 60 else cmd
            lines.append(f"  {i}. {cmd_preview}")
        if len(self.cmd_history) > 20:
            lines.append(f"  ... ({len(self.cmd_history) - 20} more)")
        return "\n".join(lines)

    def _show_help(self):
        mode_display = (
            f"{GREEN}PLAN (read-only){RESET}"
            if self.mode == "plan"
            else f"{YELLOW}ACT (write enabled){RESET}"
        )
        permission_status = f"{GREEN}enabled{RESET}" if self.ask_permission else f"{YELLOW}disabled{RESET}"
        help_text = (
            f"{BOLD}Available Commands:{RESET}\n"
            f"  /q, /quit           Quit nicode\n"
            f"  /c, /clear          Clear conversation\n"
            f"  /h, /history        Show command history (session)\n"
            f"  /mode [plan|act]    Switch between read-only and write mode\n"
            f"  /brain [azure|nvidia] Switch LLM backend\n"
            f"  /export [file]      Export conversation as markdown\n"
            f"  /help               Show this help\n"
            f"\n"
            f"{BOLD}Input (history & completion):{RESET}\n"
            f"  ↑/↓                 Previous/next command\n"
            f"  Ctrl+W              Delete word (default)\n"
            f"  Tab                 Complete file paths\n"
            f"\n"
            f"{BOLD}Current Mode:{RESET} {mode_display}\n"
            f"\n"
            f"{BOLD}Safety:{RESET}\n"
            f"  • Permission prompts: {permission_status}\n"
            f"  • Dangerous commands (e.g., rm -rf) are flagged\n"
            f"\n"
            f"{BOLD}Tips:{RESET}\n"
            f"  • In PLAN mode, use /mode act to enable writing\n"
            f"  • /export exports conversation as markdown\n"
        )
        return render_markdown(help_text)

    def _export_conversation(self, user_input):
        parts = user_input.strip().split(maxsplit=1)
        default_file = f"conversation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        filename = parts[1] if len(parts) > 1 else default_file

        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"# Nicode Conversation ({self.brain.model if self.brain.model else self.brain.deployment})\n\n")
                f.write(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"**Mode:** {self.mode}\n\n")
                f.write("---\n\n")

                for i, msg in enumerate(self.conversation):
                    role = msg["role"].capitalize()
                    content = msg["content"]

                    if isinstance(content, list):
                        for block in content:
                            if block.get("type") == "tool_use":
                                f.write(f"### Tool: {block['name']}\n")
                                f.write(
                                    f"```\n{json.dumps(block.get('input', {}), indent=2)}\n```\n\n"
                                )
                            elif block.get("type") == "tool_result":
                                f.write(f"**Result:**\n```\n{block['content'][:500]}")
                                if len(block["content"]) > 500:
                                    f.write("...\n```\n")
                                else:
                                    f.write("\n```\n")
                            elif block.get("type") == "text":
                                f.write(f"### {role}\n\n{block.get('text', '')}\n\n")
                                f.write("---\n\n")
                    elif isinstance(content, str) and content:
                        f.write(f"### {role}\n\n{content}\n\n")
                        f.write("---\n\n")

            return f"{GREEN}✓{RESET} Exported to {filename}"
        except Exception as e:
            return f"{RED}✗ Export failed:{RESET} {e}"

    def _agentic_loop(self):
        """Process brain responses, executing tools until done."""
        max_iterations = 50

        for _iteration in range(max_iterations):
            thought = self.brain.think(self.conversation)

            # Display thinking
            if thought.thinking:
                lines = thought.thinking.strip().split("\n")[:5]
                for i, line in enumerate(lines):
                    prefix = f"{DIM}💭 " if i == 0 else f"{DIM}    "
                    print(f"{prefix}{line}{RESET}")

            # Compact if approaching context limit
            if self.brain.last_input_tokens > self.brain.context_limit * 0.75:
                self._compact_conversation()

            # Store assistant response
            self.conversation.append(
                {"role": "assistant", "content": thought.raw_content}
            )

            # Print text response
            if thought.text:
                print(f"\n{CYAN}▸{RESET} {render_markdown(thought.text)}")

            if not thought.tool_calls:
                break

            # Execute tools
            tool_results = []
            for tool_call in thought.tool_calls:
                tool_name = tool_call.name
                tool_args = tool_call.args

                # Preview
                if isinstance(tool_args, dict) and tool_args:
                    preview = str(list(tool_args.values())[0])[:45]
                else:
                    preview = str(tool_args)[:45]
                print(
                    f"\n{GREEN}▸ {tool_name.capitalize()}{RESET}({DIM}{preview}...{RESET})"
                )

                result = self._execute_tool(tool_name, tool_args)

                # Result preview
                lines = result.split("\n")
                preview = lines[0][:55]
                if len(lines) > 1:
                    preview += f" ... +{len(lines) - 1}"
                elif len(lines[0]) > 55:
                    preview += "..."
                print(f"  {DIM}└─ {preview}{RESET}")

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": result,
                    }
                )

            self.conversation.append({"role": "user", "content": tool_results})
        else:
            print(f"{YELLOW}⚠ Too many iterations (stopped){RESET}")

        return ""

    def _compact_conversation(self):
        """Summarize old messages to stay within context limits.

        Keeps the most recent KEEP_RECENT messages verbatim and summarizes
        everything older into a single context-summary message.
        """
        print(f"{DIM}(Compacting conversation...){RESET}")

        # Keep recent messages intact; summarize the rest.
        KEEP_RECENT = 6
        if len(self.conversation) <= KEEP_RECENT:
            return  # Nothing to compact

        to_summarize = self.conversation[:-KEEP_RECENT]
        to_keep = self.conversation[-KEEP_RECENT:]

        history = "\n".join(
            f"{m['role']}: {str(m['content'])[:400]}" for m in to_summarize
        )

        prompt = [
            {
                "role": "user",
                "content": (
                    "Summarize this conversation. Focus on: accomplishments, "
                    "current task, key decisions, and any pending tool results.\n\n"
                    f"{history}"
                ),
            }
        ]

        saved_tools = self.brain.tools
        self.brain.tools = []
        try:
            thought = self.brain.think(prompt)
        finally:
            self.brain.tools = saved_tools

        # Replace old messages with a summary, then keep recent messages intact.
        summary_msg = {
            "role": "user",
            "content": f"Previous context summary: {thought.text}",
        }
        ack_msg = {
            "role": "assistant",
            "content": "Understood. Continuing with the summarized context.",
        }

        self.conversation = [summary_msg, ack_msg] + to_keep

    def _execute_tool(self, name, args):
        tool = get_tool(self.tools, name)
        if tool is None:
            return f"Error: Tool '{name}' not found"

        # Ask for permission if the tool requires it and ask_permission is enabled
        if self.ask_permission and getattr(tool, "requires_permission", False):
            # Format arguments for display, truncating large values
            if isinstance(args, dict):
                arg_parts = []
                for k, v in args.items():
                    if isinstance(v, str) and len(v) > 100:
                        v_display = v[:97] + "..."
                    else:
                        v_display = v
                    arg_parts.append(f"{k}={v_display!r}")
                args_str = ", ".join(arg_parts)
            else:
                args_str = str(args)[:200]

            prompt = (
                f"\n{YELLOW}⚠ Permission Request{RESET}\n"
                f"Tool: {name}\n"
                f"Arguments: {args_str}\n"
                f"Allow execution? (y/n): "
            )
            try:
                response = input(prompt).strip().lower()
                if response not in ("y", "yes", ""):
                    return "Permission denied by user."
            except (EOFError, KeyboardInterrupt):
                return "Permission denied (interrupted)."

        try:
            context = ToolContext(memory=self.memory)
            return tool.execute(context, **args)
        except TypeError as e:
            return f"Error: Invalid arguments - {e}"


# --- Main Loop ---


def create_brain(brain_type, memory, tools):
    if brain_type == "nvidia":
        return NvidiaNIM(memory=memory, tools=tools)
    elif brain_type == "azure":
        return AzureOpenAI(memory=memory, tools=tools)
    else:
        raise ValueError(
            f"Unknown brain type: '{brain_type}'. Use 'azure' or 'nvidia'."
        )


def main():
    memory = Memory()
    brain_type = os.getenv("NICODE_BRAIN", "azure").lower()
    brain = create_brain(brain_type, memory, tool_definitions(tools))
    # Enable/disable permission prompts via NICODE_ASK_PERMISSION (default: true)
    ask_perm_val = os.getenv("NICODE_ASK_PERMISSION", "true").lower()
    ask_permission = ask_perm_val in ("1", "true", "yes", "y")
    agent = Agent(brain=brain, tools=tools, memory=memory, mode="plan", ask_permission=ask_permission)

    # Initialize input history
    try:
        from prompt_toolkit.history import InMemoryHistory

        input_history = InMemoryHistory()
    except ImportError:
        input_history = None

    # Header
    brain_label = "NVIDIA NIM" if brain_type == "nvidia" else "Azure"
    model_name = brain.model if brain_type == "nvidia" else brain.deployment

    print(separator())
    print(
        f"{BOLD}nicode{RESET} {DIM}|{RESET} {brain_label} {DIM}({model_name}){RESET}"
    )
    print(separator())
    print(f"{DIM}Type /help for commands | Ctrl+C to exit{RESET}")
    print()

    while True:
        try:
            prompt_text = f"{BOLD}{BLUE}❯{RESET} "
            user_input = prompt_input(prompt_text, input_history)
            print(separator())

            # Brain switching
            if user_input.strip().startswith("/brain"):
                parts = user_input.split()
                if len(parts) > 1:
                    new_type = parts[1].lower()
                    if new_type in ("azure", "nvidia"):
                        brain_type = new_type
                        brain = create_brain(
                            brain_type,
                            memory,
                            tool_definitions(agent._tools_for_mode()),
                        )
                        agent.brain = brain
                        agent.brain.system = agent._build_system_prompt()
                        brain_label = (
                            "NVIDIA NIM" if brain_type == "nvidia" else "Azure"
                        )
                        model_name = (
                            brain.model if brain_type == "nvidia" else brain.deployment
                        )
                        print(f"{GREEN}✓ Switched to {brain_label}{RESET}\n")
                        print(separator())
                        print(
                            f"{BOLD}nicode{RESET} {DIM}|{RESET} {brain_label} {DIM}({model_name}){RESET}"
                        )
                        print(separator())
                    else:
                        print(
                            f"{RED}Unknown brain:{RESET} {new_type} (use azure or nvidia)\n"
                        )
                else:
                    print(f"{DIM}Current: {brain_label}{RESET}\n")
                continue

            output = agent.handle_input(user_input)
            if output:
                print(f"\n{output}\n")

        except AgentStop:
            print(f"\n{GREEN}Goodbye!{RESET}")
            break
        except KeyboardInterrupt:
            print(f"\n{GREEN}Goodbye!{RESET}")
            break


if __name__ == "__main__":
    main()
