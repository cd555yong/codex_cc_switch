#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenAI到Codex格式转换模块
支持流式和非流式输出
"""

import json
import time
import os
import uuid
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse

try:
    from config_manager import get_config_manager
    _config_mgr = get_config_manager()
except ImportError:
    _config_mgr = None


class OpenAIToCodex:
    """OpenAI格式到Codex格式的转换器"""
    
    # Codex完整instructions（必需，不可简化）
    CODEX_INSTRUCTIONS = """You are Codex, based on GPT-5. You are running as a coding agent in the Codex CLI on a user's computer.

## General

- The arguments to `shell` will be passed to execvp(). Most terminal commands should be prefixed with ["bash", "-lc"].
- Always set the `workdir` param when using the shell function. Do not use `cd` unless absolutely necessary.
- When searching for text or files, prefer using `rg` or `rg --files` respectively because `rg` is much faster than alternatives like `grep`. (If the `rg` command is not found, then use alternatives.)

## Editing constraints

- Default to ASCII when editing or creating files. Only introduce non-ASCII or other Unicode characters when there is a clear justification and the file already uses them.
- Add succinct code comments that explain what is going on if code is not self-explanatory. You should not add comments like "Assigns the value to the variable", but a brief comment might be useful ahead of a complex code block that the user would otherwise have to spend time parsing out. Usage of these comments should be rare.
- You may be in a dirty git worktree.
    * NEVER revert existing changes you did not make unless explicitly requested, since these changes were made by the user.
    * If asked to make a commit or code edits and there are unrelated changes to your work or changes that you didn't make in those files, don't revert those changes.
    * If the changes are in files you've touched recently, you should read carefully and understand how you can work with the changes rather than reverting them.
    * If the changes are in unrelated files, just ignore them and don't revert them.
- While you are working, you might notice unexpected changes that you didn't make. If this happens, STOP IMMEDIATELY and ask the user how they would like to proceed.

## Plan tool

When using the planning tool:
- Skip using the planning tool for straightforward tasks (roughly the easiest 25%).
- Do not make single-step plans.
- When you made a plan, update it after having performed one of the sub-tasks that you shared on the plan.

## Codex CLI harness, sandboxing, and approvals

The Codex CLI harness supports several different configurations for sandboxing and escalation approvals that the user can choose from.

Filesystem sandboxing defines which files can be read or written. The options for `sandbox_mode` are:
- **read-only**: The sandbox only permits reading files.
- **workspace-write**: The sandbox permits reading files, and editing files in `cwd` and `writable_roots`. Editing files in other directories requires approval.
- **danger-full-access**: No filesystem sandboxing - all commands are permitted.

Network sandboxing defines whether network can be accessed without approval. Options for `network_access` are:
- **restricted**: Requires approval
- **enabled**: No approval needed

Approvals are your mechanism to get user consent to run shell commands without the sandbox. Possible configuration options for `approval_policy` are
- **untrusted**: The harness will escalate most commands for user approval, apart from a limited allowlist of safe "read" commands.
- **on-failure**: The harness will allow all commands to run in the sandbox (if enabled), and failures will be escalated to the user for approval to run again without the sandbox.
- **on-request**: Commands will be run in the sandbox by default, and you can specify in your tool call if you want to escalate a command to run without sandboxing. (Note that this mode is not always available. If it is, you'll see parameters for it in the `shell` command description.)
- **never**: This is a non-interactive mode where you may NEVER ask the user for approval to run commands. Instead, you must always persist and work around constraints to solve the task for the user. You MUST do your utmost best to finish the task and validate your work before yielding. If this mode is paired with `danger-full-access`, take advantage of it to deliver the best outcome for the user. Further, in this mode, your default testing philosophy is overridden: Even if you don't see local patterns for testing, you may add tests and scripts to validate your work. Just remove them before yielding.

When you are running with `approval_policy == on-request`, and sandboxing enabled, here are scenarios where you'll need to request approval:
- You need to run a command that writes to a directory that requires it (e.g. running tests that write to /var)
- You need to run a GUI app (e.g., open/xdg-open/osascript) to open browsers or files.
- You are running sandboxed and need to run a command that requires network access (e.g., installing packages)
- If you run a command that is important to solving the user's query, but it fails because of sandboxing, rerun the command with approval. ALWAYS proceed to use the `with_escalated_permissions` and `justification` parameters - do not message the user before requesting approval for the command.
- You are about to take a potentially destructive action such as an `rm` or `git reset` that the user did not explicitly ask for
- (for all of these, you should weigh alternative paths that do not require approval)

When `sandbox_mode` is set to read-only, you'll need to request approval for any command that isn't a read.

You will be told what filesystem sandboxing, network sandboxing, and approval mode are active in a developer or user message. If you are not told about this, assume that you are running with workspace-write, network sandboxing enabled, and approval on-failure.

Although they introduce friction to the user because your work is paused until the user responds, you should leverage them when necessary to accomplish important work. If the completing the task requires escalated permissions, Do not let these settings or the sandbox deter you from attempting to accomplish the user's task unless it is set to "never", in which case never ask for approvals.

When requesting approval to execute a command that will require escalated privileges:
  - Provide the `with_escalated_permissions` parameter with the boolean value true
  - Include a short, 1 sentence explanation for why you need to enable `with_escalated_permissions` in the justification parameter

## Special user requests

- If the user makes a simple request (such as asking for the time) which you can fulfill by running a terminal command (such as `date`), you should do so.
- If the user asks for a "review", default to a code review mindset: prioritise identifying bugs, risks, behavioural regressions, and missing tests. Findings must be the primary focus of the response - keep summaries or overviews brief and only after enumerating the issues. Present findings first (ordered by severity with file/line references), follow with open questions or assumptions, and offer a change-summary only as a secondary detail. If no findings are discovered, state that explicitly and mention any residual risks or testing gaps.

## Presenting your work and final message

You are producing plain text that will later be styled by the CLI. Follow these rules exactly. Formatting should make results easy to scan, but not feel mechanical. Use judgment to decide how much structure adds value.

- Default: be very concise; friendly coding teammate tone.
- Ask only when needed; suggest ideas; mirror the user's style.
- For substantial work, summarize clearly; follow final‑answer formatting.
- Skip heavy formatting for simple confirmations.
- Don't dump large files you've written; reference paths only.
- No "save/copy this file" - User is on the same machine.
- Offer logical next steps (tests, commits, build) briefly; add verify steps if you couldn't do something.
- For code changes:
  * Lead with a quick explanation of the change, and then give more details on the context covering where and why a change was made. Do not start this explanation with "summary", just jump right in.
  * If there are natural next steps the user may want to take, suggest them at the end of your response. Do not make suggestions if there are no natural next steps.
  * When suggesting multiple options, use numeric lists for the suggestions so the user can quickly respond with a single number.
- The user does not command execution outputs. When asked to show the output of a command (e.g., `git show`), relay the important details in your answer or summarize the key lines so the user understands the result.

### Final answer structure and style guidelines

- Plain text; CLI handles styling. Use structure only when it helps scanability.
- Headers: optional; short Title Case (1-3 words) wrapped in **…**; no blank line before the first bullet; add only if they truly help.
- Bullets: use - ; merge related points; keep to one line when possible; 4–6 per list ordered by importance; keep phrasing consistent.
- Monospace: backticks for commands/paths/env vars/code ids and inline examples; use for literal keyword bullets; never combine with **.
- Code samples or multi-line snippets should be wrapped in fenced code blocks; add a language hint whenever obvious.
- Structure: group related bullets; order sections general → specific → supporting; for subsections, start with a bolded keyword bullet, then items; match complexity to the task.
- Tone: collaborative, concise, factual; present tense, active voice; self‑contained; no "above/below"; parallel wording.
- Don'ts: no nested bullets/hierarchies; no ANSI codes; don't cram unrelated keywords; keep keyword lists short—wrap/reformat if long; avoid naming formatting styles in answers.
- Adaptation: code explanations → precise, structured with code refs; simple tasks → lead with outcome; big changes → logical walkthrough + rationale + next actions; casual one-offs → plain sentences, no headers/bullets.
- File References: When referencing files in your response, make sure to include the relevant start line and always follow the below rules:
  * Use inline code to make file paths clickable.
  * Each reference should have a stand alone path. Even if it's the same file.
  * Accepted: absolute, workspace‑relative, a/ or b/ diff prefixes, or bare filename/suffix.
  * Line/column (1‑based, optional): :line[:column] or #Lline[Ccolumn] (column defaults to 1).
  * Do not use URIs like file://, vscode://, or https://.
  * Do not provide range of lines
  * Examples: src/app.ts, src/app.ts:42, b/server/index.js#L10, C:\\\\repo\\\\project\\\\main.rs:12:5"""
    
    @staticmethod
    def convert_request(openai_request: Dict[str, Any], codex_config: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        将OpenAI格式请求转换为Codex格式
        
        Args:
            openai_request: OpenAI格式的请求数据
            codex_config: Codex配置（包含base_url和key）
            
        Returns:
            Tuple[Dict[str, Any], Dict[str, Any]]: (转换后的Codex请求, headers字典)
        """
        # 提取messages
        openai_messages = openai_request.get("messages", [])
        
        # 构建环境上下文
        env_context = {
            'cwd': os.path.abspath('.'),
            'approval_policy': 'on-request',
            'sandbox_mode': 'workspace-write',
            'network_access': 'enabled',
            'shell': 'powershell.exe' if os.name == 'nt' else 'bash'
        }
        
        env_text = f"<environment_context>\n  <cwd>{env_context['cwd']}</cwd>\n  <approval_policy>{env_context['approval_policy']}</approval_policy>\n  <sandbox_mode>{env_context['sandbox_mode']}</sandbox_mode>\n  <network_access>{env_context['network_access']}</network_access>\n  <shell>{env_context['shell']}</shell>\n</environment_context>"
        
        # 构建Codex格式的input
        codex_input = []
        
        # 第一条消息：环境上下文
        codex_input.append({
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": env_text
            }]
        })
        
        # 转换OpenAI messages
        for msg in openai_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            if role == "system":
                # system消息跳过（已包含在instructions中）
                continue
            elif role == "user":
                codex_input.append({
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": content if isinstance(content, str) else str(content)
                    }]
                })
            elif role == "assistant":
                codex_input.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": content if isinstance(content, str) else str(content)
                    }]
                })
        
        # 构建完整的Codex请求
        codex_request = {
            "model": "gpt-5-codex",
            "instructions": OpenAIToCodex.CODEX_INSTRUCTIONS,
            "input": codex_input,
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "description": "Runs a shell command and returns its output.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "The command to execute"
                            },
                            "justification": {
                                "type": "string",
                                "description": "Only set if with_escalated_permissions is true."
                            },
                            "timeout_ms": {
                                "type": "number",
                                "description": "The timeout for the command in milliseconds"
                            },
                            "with_escalated_permissions": {
                                "type": "boolean",
                                "description": "Whether to request escalated permissions"
                            },
                            "workdir": {
                                "type": "string",
                                "description": "The working directory to execute the command in"
                            }
                        },
                        "required": ["command"],
                        "additionalProperties": False
                    },
                    "strict": False
                },
                {
                    "type": "function",
                    "name": "update_plan",
                    "description": "Updates the task plan.",
                    "strict": False,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "explanation": {"type": "string"},
                            "plan": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "status": {
                                            "type": "string",
                                            "description": "One of: pending, in_progress, completed"
                                        },
                                        "step": {"type": "string"}
                                    },
                                    "required": ["step", "status"],
                                    "additionalProperties": False
                                }
                            }
                        },
                        "required": ["plan"],
                        "additionalProperties": False
                    }
                },
                {
                    "type": "function",
                    "name": "view_image",
                    "description": "Attach a local image to the conversation context.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Local filesystem path to an image file"
                            }
                        },
                        "required": ["path"],
                        "additionalProperties": False
                    },
                    "strict": False
                }
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": str(uuid.uuid4())
        }
        
        # 构建headers
        session_id = str(uuid.uuid4())
        
        # 从base_url提取host
        base_url = codex_config.get('base_url', '')
        parsed_url = urlparse(base_url if base_url.startswith('http') else f"https://{base_url}")
        actual_host = parsed_url.netloc
        
        headers = {
            "authorization": f"Bearer {codex_config.get('key', '')}",
            "version": "0.42.0",
            "openai-beta": "responses=experimental",
            "conversation_id": session_id,
            "session_id": session_id,
            "accept": "text/event-stream",
            "content-type": "application/json",
            "user-agent": "codex_cli_rs/0.42.0 (Windows 10.0.19045; x86_64) unknown",
            "originator": "codex_cli_rs",
            "host": actual_host
        }
        
        return codex_request, headers
    
    @staticmethod
    def convert_response_chunk(codex_chunk: str) -> Optional[str]:
        """
        转换Codex SSE流式响应块为OpenAI格式
        
        Args:
            codex_chunk: Codex SSE格式的一行数据（如：data: {...}）
            
        Returns:
            Optional[str]: 转换后的OpenAI SSE格式数据，或None（如果跳过）
        """
        if not codex_chunk.strip():
            return None
            
        if not codex_chunk.startswith('data: '):
            return None
        
        data = codex_chunk[6:]  # 移除'data: '前缀
        
        if data == '[DONE]':
            return 'data: [DONE]'
        
        try:
            codex_data = json.loads(data)
            event_type = codex_data.get("type", "")
            
            current_id = f"chatcmpl-{int(time.time())}"
            created = int(time.time())
            
            # 转换不同类型的事件
            if event_type == "response.created":
                openai_data = {
                    "id": current_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": "gpt-5-codex",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None
                    }]
                }
            elif event_type == "response.output_text.delta":
                openai_data = {
                    "id": current_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": "gpt-5-codex",
                    "choices": [{
                        "index": 0,
                        "delta": {"content": codex_data.get("delta", "")},
                        "finish_reason": None
                    }]
                }
            elif event_type == "response.completed" or event_type == "response.done":
                openai_data = {
                    "id": current_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": "gpt-5-codex",
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop"
                    }]
                }
            else:
                return None  # 跳过其他事件类型
            
            return f"data: {json.dumps(openai_data, ensure_ascii=False)}"
            
        except json.JSONDecodeError:
            return None
    
    @staticmethod
    def convert_response_full(codex_response: str) -> Dict[str, Any]:
        """
        转换Codex完整响应（非流式）为OpenAI格式
        
        Args:
            codex_response: Codex SSE格式的完整响应
            
        Returns:
            Dict[str, Any]: OpenAI格式的完整响应
        """
        # 解析SSE格式，提取所有content
        content = ""
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        
        for line in codex_response.split('\n'):
            if line.startswith('data: '):
                data = line[6:]
                if data == '[DONE]':
                    break
                try:
                    codex_data = json.loads(data)
                    if codex_data.get("type") == "response.output_text.delta":
                        content += codex_data.get("delta", "")
                    elif codex_data.get("type") == "response.completed":
                        codex_usage = codex_data.get("response", {}).get("usage", {})
                        usage = {
                            "prompt_tokens": codex_usage.get("input_tokens", 0),
                            "completion_tokens": codex_usage.get("output_tokens", 0),
                            "total_tokens": codex_usage.get("total_tokens", 0)
                        }
                except json.JSONDecodeError:
                    continue
        
        openai_response = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gpt-5-codex",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "finish_reason": "stop"
            }],
            "usage": usage
        }
        
        return openai_response


# 便捷函数
def convert_openai_to_codex_request(openai_request: Dict[str, Any], codex_config: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    转换OpenAI请求为Codex格式
    
    Args:
        openai_request: OpenAI格式请求
        codex_config: Codex配置
        
    Returns:
        Tuple[Dict[str, Any], Dict[str, Any]]: (Codex请求, headers)
    """
    return OpenAIToCodex.convert_request(openai_request, codex_config)


def convert_codex_to_openai_chunk(codex_chunk: str) -> Optional[str]:
    """
    转换Codex SSE块为OpenAI格式
    
    Args:
        codex_chunk: Codex SSE数据块
        
    Returns:
        Optional[str]: OpenAI SSE数据块或None
    """
    return OpenAIToCodex.convert_response_chunk(codex_chunk)


def convert_codex_to_openai_full(codex_response: str) -> Dict[str, Any]:
    """
    转换Codex完整响应为OpenAI格式
    
    Args:
        codex_response: Codex完整响应
        
    Returns:
        Dict[str, Any]: OpenAI格式响应
    """
    return OpenAIToCodex.convert_response_full(codex_response)


if __name__ == "__main__":
    # 简单测试
    print("OpenAI到Codex转换器")
    print("=" * 60)
    
    # 测试请求转换
    test_request = {
        "model": "gpt-4",
        "messages": [
            {"role": "user", "content": "Hello, test"}
        ],
        "stream": True
    }
    
    test_config = {
        "base_url": "https://ai.chat6.me/codex/v1",
        "key": "test-key"
    }
    
    codex_req, headers = convert_openai_to_codex_request(test_request, test_config)
    
    print("测试请求转换：")
    print(f"Instructions长度: {len(codex_req['instructions'])} 字符")
    print(f"Input消息数量: {len(codex_req['input'])}")
    print(f"Tools数量: {len(codex_req['tools'])}")
    print(f"Headers: {list(headers.keys())}")
    print("\n转换器就绪！")
