#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenAI接口格式检测与转换模块
用于检测输入请求是否为OpenAI格式，并将其转换为Claude格式
"""
import json
import time
from typing import Dict, Any, List, Optional, Tuple, Union

# 直接使用exact_test.py中验证成功的配置
# 注意：此API key不再使用，authorization头由代理服务器统一管理
EXACT_TEST_API_KEY = "sk-hMWBokQmved4C8TEeh1aOKokDGkKPkq8"
EXACT_TEST_BASE_URL = "https://api.packycode.com"

# 配置现在从config_manager动态加载
# 导入config_manager
try:
    from config_manager import get_config_manager
    _config_mgr = get_config_manager()
except ImportError:
    _config_mgr = None
    print("[警告] 无法导入config_manager,使用默认硬编码配置")

# 默认配置(仅在config_manager不可用时使用)
_DEFAULT_CODEX_CONFIG = {
    "base_url": "https://fizzlycode.com/openai",
    "key": "cr_2e941f6ad99a0a76a554e492a4ae9bdbb5d877c91507dedbcd894615f79f97be",
    "type": "codex",
    "name": "Codex直连"
}

_DEFAULT_OPENAI_TO_CLAUDE_CONFIG = {
    "base_url": "http://47.181.223.190:3688/api",
    "key": "cr_d7d91143104f9ea62ad1c9d770b61209e3e2498c5b9b6be3bc2f6f321dfce6d9",
    "type": "openai_to_claude",
    "name": "OpenAI转Claude专用"
}


def get_codex_direct_config() -> Dict[str, str]:
    """获取Codex直连代理所需的基础配置"""
    if _config_mgr:
        return _config_mgr.get_codex_config()
    return dict(_DEFAULT_CODEX_CONFIG)


def get_openai_to_claude_config() -> Dict[str, str]:
    """获取OpenAI转Claude专用配置"""
    if _config_mgr:
        return _config_mgr.get_openai_to_claude_config()
    return dict(_DEFAULT_OPENAI_TO_CLAUDE_CONFIG)


class OpenAIToClaude:
    """OpenAI格式到Claude格式的转换器"""
    
    # OpenAI到Claude的模型映射 - 支持思考与不思考两种模式
    MODEL_MAPPING = {
        # 标准模式（不思考）
        "gpt-4": "claude-sonnet-4-20250514",
        "gpt-4-turbo": "claude-sonnet-4-20250514", 
        "gpt-3.5-turbo": "claude-sonnet-4-20250514",
        "claude-sonnet-4": "claude-sonnet-4-20250514",
        "claude-sonnet-3.5": "claude-sonnet-4-20250514",
        "claude-haiku": "claude-sonnet-4-20250514",
        
        # 直接指定的模型名
        "claude-sonnet-4-20250514": "claude-sonnet-4-20250514",  # 不思考模式
        "claude-sonnet-4-20250514-thinking": "claude-sonnet-4-20250514",  # 思考模式
        "claude-sonnet-4-5-20250929": "claude-sonnet-4-5-20250929",  # 保持原模型不转换
        "claude-opus-4-1-20250805": "claude-opus-4-1-20250805",  # 保持原模型不转换
    }
    
    @staticmethod
    def _should_enable_thinking(original_model: str) -> bool:
        """
        根据模型名称判断是否应该启用思考功能
        
        Args:
            original_model: 原始模型名称
            
        Returns:
            bool: True表示启用思考，False表示不启用
        """
        # 如果模型名包含 "-thinking" 后缀，启用思考功能
        return original_model.endswith("-thinking")
    
    @staticmethod
    def get_successful_headers(enable_thinking: bool = False):
        """获取exact_test.py中验证成功的sonnet-4请求头配置"""
        # 从config_manager动态获取超时设置
        if _config_mgr:
            timeout_settings = _config_mgr.get_timeout_settings()
            stainless_timeout = str(int(timeout_settings.get('streaming_read_timeout', 600)))
        else:
            stainless_timeout = '600'
        
        base_headers = {
            'connection': 'keep-alive',
            'accept': 'application/json',
            'x-stainless-retry-count': '0',
            'x-stainless-timeout': stainless_timeout,
            'x-stainless-lang': 'js',
            'x-stainless-package-version': '0.55.1',
            'x-stainless-os': 'Windows',
            'x-stainless-arch': 'x64',
            'x-stainless-runtime': 'node',
            'x-stainless-runtime-version': 'v22.17.0',
            'anthropic-dangerous-direct-browser-access': 'true',
            'anthropic-version': '2023-06-01',
            'x-app': 'cli',
            'user-agent': 'claude-cli/1.0.77 (external, cli)',
            'content-type': 'application/json',
            'x-stainless-helper-method': 'stream',
            'accept-language': '*',
            'sec-fetch-mode': 'cors',
            'accept-encoding': 'gzip, deflate'
        }
        
        # 根据是否启用思考功能设置不同的 anthropic-beta 头
        if enable_thinking:
            # 启用思考功能的beta参数（包含interleaved-thinking）
            base_headers['anthropic-beta'] = 'claude-code-20250219,interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14'
        else:
            # 标准模式的beta参数（不包含思考功能）
            base_headers['anthropic-beta'] = 'fine-grained-tool-streaming-2025-05-14'
        
        return base_headers
    
    @staticmethod
    def is_openai_request(request_data: Dict[str, Any]) -> bool:
        """
        检测请求是否为OpenAI格式
        
        Args:
            request_data: 请求数据字典
            
        Returns:
            bool: True表示是OpenAI格式，False表示不是
        """
        # 检查必需的OpenAI字段
        if not isinstance(request_data, dict):
            return False
            
        # 强制性检查：必须有model和messages字段
        if "model" not in request_data or "messages" not in request_data:
            return False
            
        # OpenAI特有参数检查（任何一个存在就是强指标）
        openai_specific_params = [
            "frequency_penalty", "presence_penalty", "logit_bias", 
            "best_of", "n", "user"
        ]
        has_openai_params = any(param in request_data for param in openai_specific_params)
        
        # 检查messages格式是否为OpenAI标准
        is_openai_messages = OpenAIToClaude._is_openai_messages_format(request_data.get("messages", []))
        
        # 如果messages是OpenAI格式（content为字符串），直接认为是OpenAI格式
        # 无论model名称是什么，只要消息格式是OpenAI的就需要转换
        if is_openai_messages:
            return True
        
        # OpenAI格式的其他标识特征
        openai_indicators = [
            # 检查模型名称是否为OpenAI格式
            OpenAIToClaude._is_openai_model_name(request_data.get("model", "")),
            
            # OpenAI常用参数存在性检查
            "temperature" in request_data or "top_p" in request_data,
            
            # OpenAI特有参数存在
            has_openai_params,
            
            # 检查是否有Claude格式的特征（反向检查）
            not OpenAIToClaude._has_claude_format_features(request_data)
        ]
        
        # 如果有OpenAI特有参数，直接认为是OpenAI格式
        if has_openai_params:
            return True
            
        # 否则至少满足2个其他条件才认为是OpenAI格式
        return sum(openai_indicators) >= 2
    
    @staticmethod
    def _is_openai_messages_format(messages: List[Dict[str, Any]]) -> bool:
        """检查messages是否为OpenAI格式"""
        if not isinstance(messages, list) or len(messages) == 0:
            return False
            
        for msg in messages:
            if not isinstance(msg, dict):
                return False
            # OpenAI格式：content通常是字符串，Claude格式：content通常是数组
            if "content" in msg and isinstance(msg["content"], str):
                return True
                
        return False
    
    @staticmethod
    def _is_openai_model_name(model: str) -> bool:
        """检查模型名称是否为OpenAI格式"""
        if not isinstance(model, str):
            return False
            
        openai_patterns = ["gpt-", "text-", "davinci", "curie", "babbage", "ada"]
        return any(pattern in model.lower() for pattern in openai_patterns)
    
    @staticmethod
    def _has_claude_format_features(request_data: Dict[str, Any]) -> bool:
        """检查是否有Claude格式的特征"""
        if not isinstance(request_data, dict):
            return False
        
        # Claude特有的字段
        claude_specific_fields = ["system", "anthropic_version", "thinking"]
        has_claude_fields = any(field in request_data for field in claude_specific_fields)
        
        # Claude格式的消息结构检查
        messages = request_data.get("messages", [])
        if isinstance(messages, list) and messages:
            for msg in messages:
                if isinstance(msg, dict) and "content" in msg:
                    content = msg["content"]
                    # Claude格式：content通常是数组
                    if isinstance(content, list):
                        for content_block in content:
                            if isinstance(content_block, dict) and content_block.get("type") in ["text", "image"]:
                                return True
        
        # Claude模型名称检查
        model = request_data.get("model", "")
        if isinstance(model, str):
            claude_patterns = ["claude-", "anthropic"]
            if any(pattern in model.lower() for pattern in claude_patterns):
                return True
        
        return has_claude_fields
    
    @staticmethod
    def convert_request(openai_request: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        将OpenAI格式请求转换为Claude格式，使用exact_test.py验证成功的格式
        支持思考与不思考模式的自动切换
        
        Args:
            openai_request: OpenAI格式的请求数据
            
        Returns:
            Tuple[Dict[str, Any], Dict[str, Any]]: (转换后的Claude格式请求数据, 请求头字典)
        """
        # 基于exact_test.py中成功的请求格式构建Claude请求
        claude_request = {}
        
        # 1. 转换模型名称并检测思考模式 - 优先使用验证成功的sonnet-4模型
        original_model = openai_request.get("model", "gpt-4")
        
        # 检测是否启用思考功能
        enable_thinking = OpenAIToClaude._should_enable_thinking(original_model)
        
        # 获取对应的Claude模型名称（去掉-thinking后缀）
        claude_request["model"] = OpenAIToClaude.MODEL_MAPPING.get(
            original_model, 
            "claude-sonnet-4-20250514"  # 默认使用exact_test.py中验证成功的sonnet-4模型
        )
        
        # 生成对应的请求头（包含思考模式设置）
        headers = OpenAIToClaude.get_successful_headers(enable_thinking=enable_thinking)
        
        # 2. 设置max_tokens - OpenAI方式默认为32000，有用户输入就用用户的
        requested_max_tokens = openai_request.get("max_tokens", 32000)  # 默认32000
        # 直接使用用户设置或默认值，不限制最大值
        claude_request["max_tokens"] = requested_max_tokens
        
        # 3. 转换messages格式
        openai_messages = openai_request.get("messages", [])
        claude_request["messages"] = OpenAIToClaude._convert_messages(openai_messages)
        
        # 4. 转换system消息为system参数 - 强制添加Claude Code身份验证
        system_content = OpenAIToClaude._extract_system_message(openai_messages)
        
        # 强制添加 Claude Code 身份验证信息，这是API服务端验证的关键！
        claude_code_system = "You are Claude Code, Anthropic's official CLI for Claude."
        
        if system_content:
            # 如果用户提供了system消息，将其与Claude Code身份验证合并
            combined_system = f"{claude_code_system}\n\n{system_content}"
        else:
            # 如果没有用户system消息，只使用Claude Code身份验证
            combined_system = claude_code_system
        
        # 使用exact_test.py中sonnet-4的system格式
        claude_request["system"] = [
            {
                "type": "text",
                "text": combined_system,
                "cache_control": {"type": "ephemeral"}
            }
        ]
        
        # 5. 处理temperature - 思考模式下必须为1，否则使用用户设置
        if enable_thinking:
            # 思考模式下强制设置temperature=1（Claude API限制）
            claude_request["temperature"] = 1
        elif "temperature" in openai_request:
            claude_request["temperature"] = openai_request["temperature"] 
        else:
            claude_request["temperature"] = 1  # exact_test.py中sonnet-4使用temperature=1
            
        # 6. 处理流式请求 - 强制使用流式，就像exact_test.py中sonnet-4配置一样
        # exact_test.py中sonnet-4默认使用stream=True，这样能避开API key限制
        claude_request["stream"] = True  # 强制使用流式，这是exact_test.py的成功关键
        
        # 7. 添加exact_test.py中的metadata格式（可选）
        claude_request["metadata"] = {
            "user_id": "user_openai_adapter_session"
        }
        
        # 8. 处理thinking模式 - 关键：添加thinking参数来显式启用思考功能
        if enable_thinking:
            claude_request["thinking"] = {
                "type": "enabled", 
                "budget_tokens": 30000  # 为思考过程分配足够的token预算
            }
        
        # 9. 过滤掉Claude不支持的OpenAI参数
        # 这些参数会导致Claude API返回500错误，需要明确过滤掉：
        # - frequency_penalty, presence_penalty, logit_bias, user, stop, n, best_of
        
        return claude_request, headers
    
    @staticmethod
    def _convert_messages(openai_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """转换messages格式"""
        claude_messages = []
        
        for msg in openai_messages:
            if msg.get("role") == "system":
                # system消息会被单独处理，这里跳过
                continue
                
            claude_msg = {
                "role": msg.get("role", "user")
            }
            
            # 转换content格式
            content = msg.get("content", "")
            if isinstance(content, str):
                # OpenAI格式：字符串 -> Claude格式：数组
                claude_msg["content"] = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                # 已经是数组格式，检查是否符合Claude格式
                processed_content = []
                for item in content:
                    if isinstance(item, dict):
                        if "type" in item and "text" in item:
                            # 已经是Claude格式
                            processed_content.append(item)
                        elif "text" in item:
                            # 添加type字段
                            processed_content.append({"type": "text", "text": item["text"]})
                        else:
                            # 未知格式，转为文本
                            processed_content.append({"type": "text", "text": str(item)})
                    else:
                        # 非字典格式，转为文本
                        processed_content.append({"type": "text", "text": str(item)})
                claude_msg["content"] = processed_content
            else:
                # 其他格式转为字符串
                claude_msg["content"] = [{"type": "text", "text": str(content)}]
                
            claude_messages.append(claude_msg)
        
        return claude_messages
    
    @staticmethod
    def _extract_system_message(messages: List[Dict[str, Any]]) -> Optional[str]:
        """提取system消息内容"""
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    # 如果content是数组，提取text内容
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    return "\n".join(text_parts)
        return None
    
    @staticmethod
    def convert_response(claude_response: Union[Dict[str, Any], str]) -> Dict[str, Any]:
        """
        将Claude格式响应转换为OpenAI格式
        
        Args:
            claude_response: Claude格式的响应数据
            
        Returns:
            Dict[str, Any]: 转换后的OpenAI格式响应数据
        """
        if isinstance(claude_response, str):
            try:
                claude_response = json.loads(claude_response)
            except json.JSONDecodeError:
                # 如果不能解析为JSON，创建一个简单的响应
                return {
                    "id": "chatcmpl-adapter",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "gpt-4",
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant", 
                            "content": claude_response
                        },
                        "finish_reason": "stop"
                    }],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0
                    }
                }
        
        # 检查是否为流式响应
        if claude_response.get("type") in ["message_start", "content_block_start", "content_block_delta", "message_delta", "message_stop", "content_block_stop"]:
            return OpenAIToClaude._convert_stream_chunk(claude_response)
        
        # 转换完整响应（非流式）
        if claude_response.get("type") == "message":
            openai_response = {
                "id": f"chatcmpl-{claude_response.get('id', 'adapter')}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": claude_response.get("model", "gpt-4"),
                "choices": [],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0, 
                    "total_tokens": 0
                }
            }
            
            # 提取内容
            content = ""
            if "content" in claude_response:
                for content_block in claude_response["content"]:
                    if content_block.get("type") == "text":
                        content += content_block.get("text", "")
            
            openai_response["choices"].append({
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "finish_reason": OpenAIToClaude._convert_stop_reason(claude_response.get("stop_reason"))
            })
            
            # 转换usage信息
            if "usage" in claude_response:
                usage = claude_response["usage"]
                openai_response["usage"] = {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                }
            
            return openai_response
        
        # 如果不是标准的Claude响应格式，尝试通用转换
        return {
            "id": "chatcmpl-adapter",
            "object": "chat.completion", 
            "created": int(time.time()),
            "model": "gpt-4",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": str(claude_response)
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }
    
    # 类变量用于跟踪thinking状态 - 使用字典以支持并发请求
    _thinking_states = {}
    
    @staticmethod
    def _convert_stream_chunk(claude_chunk: Dict[str, Any]) -> Dict[str, Any]:
        """转换流式响应块，支持thinking模式转换"""
        import time
        
        # 获取消息ID用于状态跟踪
        message_id = claude_chunk.get('message', {}).get('id', 'default')
        
        openai_chunk = {
            "id": f"chatcmpl-{message_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "gpt-4",
            "choices": []
        }
        
        # 处理不同类型的流式数据
        chunk_type = claude_chunk.get("type")
        
        if chunk_type == "message_start":
            # 重置该消息的thinking状态
            OpenAIToClaude._thinking_states[message_id] = False
            openai_chunk["choices"].append({
                "index": 0,
                "delta": {"role": "assistant"},
                "finish_reason": None
            })
            
        elif chunk_type == "content_block_start":
            content_block = claude_chunk.get("content_block", {})
            if content_block.get("type") == "thinking":
                # thinking块开始 - 输出<think>标记
                openai_chunk["choices"].append({
                    "index": 0,
                    "delta": {"content": "<think>"},
                    "finish_reason": None
                })
                OpenAIToClaude._thinking_states[message_id] = True
            elif content_block.get("type") == "text":
                # text块开始 - 如果之前在thinking中，输出</think>结束标记
                if OpenAIToClaude._thinking_states.get(message_id, False):
                    openai_chunk["choices"].append({
                        "index": 0,
                        "delta": {"content": "</think>\n\n"},
                        "finish_reason": None
                    })
                    OpenAIToClaude._thinking_states[message_id] = False
                    
        elif chunk_type == "content_block_delta":
            delta = claude_chunk.get("delta", {})
            if delta.get("type") == "thinking_delta":
                # thinking内容直接输出 - 与正文相同的格式
                openai_chunk["choices"].append({
                    "index": 0,
                    "delta": {"content": delta.get("thinking", "")},
                    "finish_reason": None
                })
            elif delta.get("type") == "text_delta":
                # 正文内容输出
                openai_chunk["choices"].append({
                    "index": 0,
                    "delta": {"content": delta.get("text", "")},
                    "finish_reason": None
                })
                    
        elif chunk_type == "message_stop":
            # 消息结束 - 如果还在thinking状态，补充结束标记
            if OpenAIToClaude._thinking_states.get(message_id, False):
                openai_chunk["choices"].append({
                    "index": 0,
                    "delta": {"content": "</think>"},
                    "finish_reason": None
                })
            
            openai_chunk["choices"].append({
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            })
            
            # 清理状态
            OpenAIToClaude._thinking_states.pop(message_id, None)
        
        return openai_chunk
    
    @staticmethod
    def _convert_stop_reason(claude_stop_reason: Optional[str]) -> str:
        """转换停止原因"""
        mapping = {
            "end_turn": "stop",
            "max_tokens": "length",
            "stop_sequence": "stop"
        }
        return mapping.get(claude_stop_reason, "stop")


# 便捷函数
def detect_and_convert_request(request_data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], Dict[str, Any]]:
    """
    检测并转换请求格式
    
    Args:
        request_data: 原始请求数据
        
    Returns:
        Tuple[bool, Dict[str, Any], Dict[str, Any]]: (是否为OpenAI格式, 转换后的数据, 请求头)
    """
    adapter = OpenAIToClaude()
    is_openai = adapter.is_openai_request(request_data)
    
    if is_openai:
        converted, headers = adapter.convert_request(request_data)
        return True, converted, headers
    else:
        # 非OpenAI格式，返回默认标准头
        default_headers = adapter.get_successful_headers(enable_thinking=False)
        return False, request_data, default_headers


def convert_response_to_openai(claude_response: Union[Dict[str, Any], str]) -> Dict[str, Any]:
    """
    将Claude响应转换为OpenAI格式
    
    Args:
        claude_response: Claude格式响应
        
    Returns:
        Dict[str, Any]: OpenAI格式响应
    """
    adapter = OpenAIToClaude()
    return adapter.convert_response(claude_response)


if __name__ == "__main__":
    # 测试代码
    print("OpenAI到Claude格式转换器测试")
    
    # 测试案例1：基本OpenAI格式请求
    openai_request1 = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "你是一个有用的助手"},
            {"role": "user", "content": "你好"}
        ],
        "temperature": 0.7,
        "max_tokens": 1000,
        "stream": True
    }
    
    print("=" * 60)
    print("测试案例1：基本OpenAI格式请求")
    print("原始OpenAI请求:")
    print(json.dumps(openai_request1, indent=2, ensure_ascii=False))
    
    # 检测和转换
    is_openai1, converted1, headers1 = detect_and_convert_request(openai_request1)
    print(f"\n是否为OpenAI格式: {is_openai1}")
    print("转换后的Claude请求:")
    print(json.dumps(converted1, indent=2, ensure_ascii=False))
    
    # 测试案例2：包含问题参数的OpenAI请求（模拟日志中失败的请求）
    openai_request2 = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {"role": "user", "content": "上下文{\n}\n 当前最新问题:你是\n以下是rules:你是编程与逻辑专家，专注于代码分析、算法设计、问题解决。提供精确、高质量的技术方案，代码编写。"}
        ],
        "temperature": 0.1,
        "top_p": 1,
        "frequency_penalty": 0,  # 这个参数导致了500错误
        "presence_penalty": 0,   # 这个参数导致了500错误
        "max_tokens": 30000,
        "stream": True
    }
    
    print("\n" + "=" * 60)
    print("测试案例2：包含问题参数的OpenAI请求（模拟日志失败情况）")
    print("原始OpenAI请求:")
    print(json.dumps(openai_request2, indent=2, ensure_ascii=False))
    
    # 检测和转换
    is_openai2, converted2, headers2 = detect_and_convert_request(openai_request2)
    print(f"\n是否为OpenAI格式: {is_openai2}")
    print("转换后的Claude请求:")
    print(json.dumps(converted2, indent=2, ensure_ascii=False))
    
    # 验证问题参数是否被过滤
    problematic_params = ["frequency_penalty", "presence_penalty"]
    filtered_params = [param for param in problematic_params if param not in converted2]
    print(f"\n已过滤的问题参数: {filtered_params}")
    print(f"转换成功，问题参数已被过滤！" if len(filtered_params) == len(problematic_params) else "警告：某些问题参数未被过滤")
    
    # 测试案例3：模拟日志第4个请求（Claude模型名+OpenAI消息格式）
    openai_request3 = {
        "model": "claude-sonnet-4-20250514", 
        "messages": [
            {"role": "user", "content": "test，请只返回5个字以内的结果"}
        ],
        "max_tokens": 5,
        "stream": True
    }
    
    print("\n" + "=" * 60)
    print("测试案例3：模拟日志第4个请求（Claude模型名+OpenAI消息格式）")
    print("原始请求:")
    print(json.dumps(openai_request3, indent=2, ensure_ascii=False))
    
    # 检测和转换
    is_openai3, converted3, headers3 = detect_and_convert_request(openai_request3)
    print(f"\n是否为OpenAI格式: {is_openai3}")
    print("转换后的Claude请求:")
    print(json.dumps(converted3, indent=2, ensure_ascii=False))
    
    # 验证content是否正确转换为数组格式
    if is_openai3 and isinstance(converted3.get("messages", [{}])[0].get("content"), list):
        print("✅ content已正确转换为Claude数组格式")
    else:
        print("❌ content转换失败，仍为字符串格式")
    
    # 测试案例4：Claude原生格式请求  
    claude_request = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "你好"}]
            }
        ],
        "max_tokens": 1000,
        "system": "你是一个有用的助手"
    }
    
    print("\n" + "=" * 60)
    print("测试案例4：Claude原生格式请求")
    print("原始Claude请求:")
    print(json.dumps(claude_request, indent=2, ensure_ascii=False))
    
    # 检测和转换
    is_openai4, converted4, headers4 = detect_and_convert_request(claude_request)
    print(f"\n是否为OpenAI格式: {is_openai4}")
    print("处理结果:")
    print(json.dumps(converted4, indent=2, ensure_ascii=False))
    print("Claude格式应该保持不变" if not is_openai4 else "警告：Claude格式被误识别为OpenAI格式")
