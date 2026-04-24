import asyncio
import json
import os
import shutil
import time

import aiohttp


def install_anthropic(ctx):
    from llms.main import OpenAiCompatible

    class AnthropicProvider(OpenAiCompatible):
        """Anthropic Provider using Anthropic API and API Pricing"""

        sdk = "@ai-sdk/anthropic"

        def __init__(self, **kwargs):
            if "api" not in kwargs:
                kwargs["api"] = "https://api.anthropic.com/v1"
            super().__init__(**kwargs)

            # Anthropic uses x-api-key header instead of Authorization
            if self.api_key:
                self.headers = self.headers.copy()
                if "Authorization" in self.headers:
                    del self.headers["Authorization"]
                self.headers["x-api-key"] = self.api_key

            if "anthropic-version" not in self.headers:
                self.headers = self.headers.copy()
                self.headers["anthropic-version"] = "2023-06-01"
            self.chat_url = f"{self.api}/messages"

        async def chat(self, chat, context=None):
            chat["model"] = self.provider_model(chat["model"]) or chat["model"]

            chat = await self.process_chat(chat, provider_id=self.id)

            # Transform OpenAI format to Anthropic format
            anthropic_request = {
                "model": chat["model"],
                "messages": [],
            }

            # Extract system message (Anthropic uses top-level 'system' parameter)
            system_messages = []
            for message in chat.get("messages", []):
                if message.get("role") == "system":
                    content = message.get("content", "")
                    if isinstance(content, str):
                        system_messages.append(content)
                    elif isinstance(content, list):
                        for item in content:
                            if item.get("type") == "text":
                                system_messages.append(item.get("text", ""))

            if system_messages:
                anthropic_request["system"] = "\n".join(system_messages)

            # Transform messages (exclude system messages)
            for message in chat.get("messages", []):
                if message.get("role") == "system":
                    continue

                if message.get("role") == "tool":
                    # Convert OpenAI tool response to Anthropic tool_result
                    tool_call_id = message.get("tool_call_id")
                    content = ctx.to_content(message.get("content", ""))
                    if not isinstance(content, (str, list)):
                        content = str(content)

                    tool_result = {"type": "tool_result", "tool_use_id": tool_call_id, "content": content}

                    # Anthropic requires tool results to be in a user message
                    # Check if the last message was a user message, if so append to it
                    if anthropic_request["messages"] and anthropic_request["messages"][-1]["role"] == "user":
                        anthropic_request["messages"][-1]["content"].append(tool_result)
                    else:
                        anthropic_request["messages"].append({"role": "user", "content": [tool_result]})
                    continue

                anthropic_message = {"role": message.get("role"), "content": []}

                # Handle interleaved thinking (must always be a list if present)
                if "thinking" in message and message["thinking"]:
                    anthropic_message["content"].append({"type": "thinking", "thinking": message["thinking"]})

                content = message.get("content", "")
                if isinstance(content, str):
                    if anthropic_message["content"] or message.get("tool_calls"):
                        # If we have thinking or tools, we must use blocks for text
                        if content:
                            anthropic_message["content"].append({"type": "text", "text": content})
                    else:
                        anthropic_message["content"] = content
                elif isinstance(content, list):
                    for item in content:
                        if item.get("type") == "text":
                            anthropic_message["content"].append({"type": "text", "text": item.get("text", "")})
                        elif item.get("type") == "image_url" and "image_url" in item:
                            # Transform OpenAI image_url format to Anthropic format
                            image_url = item["image_url"].get("url", "")
                            if image_url.startswith("data:"):
                                # Extract media type and base64 data
                                parts = image_url.split(";base64,", 1)
                                if len(parts) == 2:
                                    media_type = parts[0].replace("data:", "")
                                    base64_data = parts[1]
                                    anthropic_message["content"].append(
                                        {
                                            "type": "image",
                                            "source": {"type": "base64", "media_type": media_type, "data": base64_data},
                                        }
                                    )

                # Handle tool_calls
                if "tool_calls" in message and message["tool_calls"]:
                    # specific check for content being a string and not empty, because we might have converted it above
                    if isinstance(anthropic_message["content"], str):
                        anthropic_message["content"] = []
                        if content:
                            anthropic_message["content"].append({"type": "text", "text": content})

                    for tool_call in message["tool_calls"]:
                        function = tool_call.get("function", {})
                        tool_use = {
                            "type": "tool_use",
                            "id": tool_call.get("id"),
                            "name": function.get("name"),
                            "input": json.loads(function.get("arguments", "{}")),
                        }
                        anthropic_message["content"].append(tool_use)

                anthropic_request["messages"].append(anthropic_message)

            # Handle max_tokens (required by Anthropic, uses max_tokens not max_completion_tokens)
            if "max_completion_tokens" in chat:
                anthropic_request["max_tokens"] = chat["max_completion_tokens"]
            elif "max_tokens" in chat:
                anthropic_request["max_tokens"] = chat["max_tokens"]
            else:
                # Anthropic requires max_tokens, set a default
                anthropic_request["max_tokens"] = 4096

            # Copy other supported parameters
            if "temperature" in chat:
                anthropic_request["temperature"] = chat["temperature"]
            if "top_p" in chat:
                anthropic_request["top_p"] = chat["top_p"]
            if "top_k" in chat:
                anthropic_request["top_k"] = chat["top_k"]
            if "stop" in chat:
                anthropic_request["stop_sequences"] = chat["stop"] if isinstance(chat["stop"], list) else [chat["stop"]]
            if "stream" in chat:
                anthropic_request["stream"] = chat["stream"]
            if "tools" in chat:
                anthropic_tools = []
                for tool in chat["tools"]:
                    if tool.get("type") == "function":
                        function = tool.get("function", {})
                        anthropic_tool = {
                            "name": function.get("name"),
                            "description": function.get("description"),
                            "input_schema": function.get("parameters"),
                        }
                        anthropic_tools.append(anthropic_tool)
                if anthropic_tools:
                    anthropic_request["tools"] = anthropic_tools
            if "tool_choice" in chat:
                anthropic_request["tool_choice"] = chat["tool_choice"]

            if "response_format" in chat:
                response_format = chat["response_format"]
                if response_format.get("type") == "json_schema":
                    json_schema = response_format.get("json_schema", {})
                    if "schema" in json_schema:
                        anthropic_request["output_config"] = {
                            "format": {
                                "type": "json_schema",
                                "schema": json_schema["schema"],
                            }
                        }

            ctx.log(f"POST {self.chat_url}")
            ctx.log(json.dumps(anthropic_request, indent=2))

            async with aiohttp.ClientSession() as session:
                started_at = time.time()
                async with session.post(
                    self.chat_url,
                    headers=self.headers,
                    data=json.dumps(anthropic_request),
                    timeout=ctx.get_client_timeout(),
                ) as response:
                    return ctx.log_json(
                        self.to_response(await self.response_json(response), chat, started_at, context=context)
                    )

        def to_response(self, response, chat, started_at, context=None):
            """Convert Anthropic response format to OpenAI-compatible format."""
            if context is not None:
                context["providerResponse"] = response
            # Transform Anthropic response to OpenAI format
            ret = {
                "id": response.get("id", ""),
                "object": "chat.completion",
                "created": int(started_at),
                "model": response.get("model", ""),
                "choices": [],
                "usage": {},
            }

            # Transform content blocks to message content
            content_parts = []
            thinking_parts = []
            tool_calls = []

            for block in response.get("content", []):
                if block.get("type") == "text":
                    content_parts.append(block.get("text", ""))
                elif block.get("type") == "thinking":
                    # Store thinking blocks separately (some models include reasoning)
                    thinking_parts.append(block.get("thinking", ""))
                elif block.get("type") == "tool_use":
                    tool_call = {
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": block.get("name"),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                    tool_calls.append(tool_call)

            # Combine all text content
            message_content = "\n".join(content_parts) if content_parts else ""

            # Create the choice object
            choice = {
                "index": 0,
                "message": {"role": "assistant", "content": message_content},
                "finish_reason": response.get("stop_reason", "stop"),
            }

            # Add thinking as metadata if present
            if thinking_parts:
                choice["message"]["thinking"] = "\n".join(thinking_parts)

            # Add tool_calls if present
            if tool_calls:
                choice["message"]["tool_calls"] = tool_calls

            ret["choices"].append(choice)

            # Transform usage
            if "usage" in response:
                usage = response["usage"]
                ret["usage"] = {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                }

            # Add metadata
            if "metadata" not in ret:
                ret["metadata"] = {}
            ret["metadata"]["duration"] = int(time.time() - started_at)

            if chat is not None and "model" in chat:
                cost = self.model_cost(chat["model"])
                if cost and "input" in cost and "output" in cost:
                    ret["metadata"]["pricing"] = f"{cost['input']}/{cost['output']}"

            return ret

    ctx.add_provider(AnthropicProvider)

    class AnthropicProviderCli(OpenAiCompatible):
        """Anthropic Provider using local claude CLI to make use of an existing Claude Code subscription"""

        sdk = "@ai-sdk/anthropic-cli"

        def __init__(self, **kwargs):
            if "api" not in kwargs:
                kwargs["api"] = "https://api.anthropic.com/v1"
            super().__init__(**kwargs)
            self.chat_url = f"{self.api}/messages"
            # Disable tool calling as CLI doesn't support it
            for model in self.models.values():
                model["tool_call"] = False
            # print(f"Anthropic models = {json.dumps(self.models, indent=2)}")

        def validate(self, **kwargs):
            if not shutil.which("claude"):
                return f"Provider '{self.name}' requires 'claude' in PATH"
            return None

        async def chat(self, chat, context=None):
            chat["model"] = self.provider_model(chat["model"]) or chat["model"]

            chat = await self.process_chat(chat, provider_id=self.id)

            # Extract system prompt from messages
            system_messages = []
            for message in chat.get("messages", []):
                if message.get("role") == "system":
                    content = message.get("content", "")
                    if isinstance(content, str):
                        system_messages.append(content)
                    elif isinstance(content, list):
                        for item in content:
                            if item.get("type") == "text":
                                system_messages.append(item.get("text", ""))

            # Build the user prompt from non-system messages
            prompt_parts = []
            for message in chat.get("messages", []):
                if message.get("role") == "system":
                    continue
                role = message.get("role", "user")
                content = message.get("content", "")
                if isinstance(content, list):
                    text_parts = []
                    for item in content:
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                    content = "\n".join(text_parts)
                if content:
                    if role == "assistant":
                        prompt_parts.append(f"[assistant]: {content}")
                    else:
                        prompt_parts.append(content)
            prompt = "\n\n".join(prompt_parts)

            # Build claude CLI command
            cmd = [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--no-session-persistence",
            ]

            # Map model name to claude CLI model aliases
            model = chat.get("model", "")
            if model:
                cmd.extend(["--model", model])

            # Add system prompt if present
            if system_messages:
                system_prompt = "\n".join(system_messages)
                cmd.extend(["--system-prompt", system_prompt])

            # Add max tokens budget if specified
            max_tokens = chat.get("max_completion_tokens") or chat.get("max_tokens")
            if max_tokens:
                cmd.extend(["--max-budget-usd", str(max_tokens)])

            ctx.log(f"Running: claude -p ... --output-format json --model {model}")
            ctx.log(f"Prompt: {prompt[:200]}{'...' if len(prompt) > 200 else ''}")

            started_at = time.time()

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    error_msg = (
                        stderr.decode("utf-8", errors="replace").strip()
                        if stderr
                        else f"claude exited with code {process.returncode}"
                    )
                    raise Exception(f"claude CLI error: {error_msg}")

                # Parse the JSON response from claude CLI
                output = stdout.decode("utf-8", errors="replace").strip()
                # Remove any trailing ANSI escape sequences
                if output.endswith("\x1b[<u"):
                    output = output[:-4]
                output = output.strip()

                cli_response = json.loads(output)

            except json.JSONDecodeError as e:
                raise Exception(f"Failed to parse claude CLI JSON output: {e}")
            except FileNotFoundError:
                raise Exception("claude CLI not found in PATH")

            # Convert claude CLI response to OpenAI-compatible format
            return ctx.log_json(self.to_cli_response(cli_response, chat, started_at, context=context))

        def to_cli_response(self, cli_response, chat, started_at, context=None):
            """Convert claude CLI JSON response to OpenAI-compatible format."""
            if context is not None:
                context["providerResponse"] = cli_response

            is_error = cli_response.get("is_error", False)
            result_text = cli_response.get("result", "")

            ret = {
                "id": cli_response.get("session_id", ""),
                "object": "chat.completion",
                "created": int(started_at),
                "model": chat.get("model", ""),
                "choices": [],
                "usage": {},
            }

            # Create the choice object
            choice = {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result_text,
                },
                "finish_reason": "error" if is_error else "stop",
            }
            ret["choices"].append(choice)

            # Transform usage from claude CLI format
            usage = cli_response.get("usage", {})
            input_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            output_tokens = usage.get("output_tokens", 0)
            ret["usage"] = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }

            # Add metadata
            ret["metadata"] = {
                "duration": cli_response.get("duration_ms", int((time.time() - started_at) * 1000)),
            }

            # Add cost if available
            if "total_cost_usd" in cli_response:
                ret["metadata"]["cost_usd"] = cli_response["total_cost_usd"]

            if chat is not None and "model" in chat:
                cost = self.model_cost(chat["model"])
                if cost and "input" in cost and "output" in cost:
                    ret["metadata"]["pricing"] = f"{cost['input']}/{cost['output']}"

            return ret

    ctx.add_provider(AnthropicProviderCli)
