import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import sys
import json
import copy
import logging
import time
from datetime import datetime, timedelta
from openai_adapter import detect_and_convert_request, convert_response_to_openai, get_codex_direct_config
import os
import threading
from contextlib import asynccontextmanager
import gzip
import io
from typing import Any, Dict, List, Optional
from enum import Enum
from config_manager import get_config_manager
import uuid

# 导入实时统计管理器
try:
    from token_stats import get_stats_manager
    stats_mgr = get_stats_manager()
except ImportError:
    stats_mgr = None
    print("警告: 无法导入token_stats，实时统计功能将不可用", file=sys.stderr)

# 统一配置管理 - 所有配置从config_manager加载
config_mgr = get_config_manager()

# 配置类型枚举
class ConfigType(Enum):
    API = "api"
    CODEX = "codex"

def _init_status_dict(configs: list) -> dict:
    """通用的状态字典初始化函数"""
    return {i: {"status": "normal", "error_count": 0, "cooldown_until": None} for i in range(len(configs))}

def _get_primary_indices(configs: list) -> List[int]:
    """通用的获取主配置索引函数"""
    return [i for i, cfg in enumerate(configs) if cfg.get("type", "primary") == "primary"]

def _get_backup_indices(configs: list) -> List[int]:
    """通用的获取备用配置索引函数"""
    return [i for i, cfg in enumerate(configs) if cfg.get("type") == "backup"]

def _record_error_core(api_index: int, error_code: int, silent: bool, 
                       status_dict: dict, configs: list, threshold: int, 
                       config_type_name: str) -> Optional[str]:
    """通用的错误记录核心函数"""
    now = datetime.now()
    
    if api_index not in status_dict:
        status_dict[api_index] = {"status": "normal", "error_count": 0, "cooldown_until": None}
    
    status_dict[api_index]["error_count"] += 1
    
    msg = None
    if status_dict[api_index]["error_count"] >= threshold:
        cooldown_seconds = TimeoutConfig.get_api_cooldown_seconds()
        status_dict[api_index]["cooldown_until"] = now + timedelta(seconds=cooldown_seconds)
        status_dict[api_index]["status"] = "warning"
        cooldown_end_time = (now + timedelta(seconds=cooldown_seconds)).strftime('%H:%M:%S')
        msg = f"[{now.strftime('%H:%M:%S')}] {config_type_name} {configs[api_index]['name']} 连续{threshold}次错误，设置{cooldown_seconds//60}分钟冷却(至{cooldown_end_time})"
    else:
        msg = f"[{now.strftime('%H:%M:%S')}] {config_type_name} {configs[api_index]['name']} 错误计数: {status_dict[api_index]['error_count']}/{threshold}，继续使用当前{config_type_name}"
    
    if not silent and msg:
        print(msg)
    return msg

def _init_activation_status_core(configs: list) -> dict:
    """通用的激活状态初始化函数"""
    status = {}
    for i, config in enumerate(configs):
        if config.get('activation_enabled', False):
            status[i] = {
                "retry_count": 0,
                "last_attempt_date": None,
                "activated_today": False,
                "last_attempt_time": None
            }
    return status

# 加载各类配置
API_CONFIGS = config_mgr.get_enabled_api_configs()
CODEX_CONFIGS = config_mgr.get_enabled_codex_configs()
CODEX_DIRECT_CONFIG = config_mgr.get_codex_config()  # 向后兼容，返回第一个启用的配置
OPENAI_TO_CLAUDE_CONFIGS = config_mgr.get_openai_to_claude_configs()
READ_TIMEOUT_RETRY_CONFIGS = config_mgr.get_enabled_retry_configs()
MODEL_CONVERSIONS = config_mgr.get_enabled_model_conversions()

# Codex配置
CODEX_PATH_PREFIX = "openai"
codex_timeout_extra_seconds = 0  # 额外超时秒数（每次失败+60）
codex_success_count = 0  # 连续成功计数
codex_timeout_lock = threading.Lock()  # 保护Codex超时全局变量的线程锁

# Codex KEY轮动状态管理
# codex_current_config_index 会在初始化阶段计算
codex_is_using_backup = False  # 是否正在使用备用Codex KEY
codex_backup_start_time = None  # 开始使用备用Codex KEY的时间
codex_last_primary_check_time = None  # 上次检测主Codex KEY的时间
codex_key_switch_lock = threading.Lock()  # Codex KEY切换的线程锁

# Codex API状态管理
def init_codex_api_status():
    """初始化或同步Codex API状态字典"""
    return _init_status_dict(CODEX_CONFIGS)

codex_api_status = init_codex_api_status()

# 轮动状态管理
last_primary_switch_time = datetime.now()  # 上次主要API切换时间
is_using_backup = False  # 是否正在使用备用API
backup_start_time = None  # 开始使用备用API的时间
last_primary_check_time = None  # 上次检测主API的时间
key_switch_lock = threading.Lock()  # 线程锁，确保切换的安全性

# API状态管理
def init_api_status():
    """初始化或同步API状态字典"""
    return _init_status_dict(API_CONFIGS)

api_status = init_api_status()
current_api_key = ""

current_config_index: int = -1
codex_current_config_index: int = -1

def get_primary_api_indices() -> List[int]:
    return _get_primary_indices(API_CONFIGS)

def get_backup_api_indices() -> List[int]:
    return _get_backup_indices(API_CONFIGS)

def get_first_available_primary_api_index() -> Optional[int]:
    for idx in get_primary_api_indices():
        if is_api_available(idx):
            return idx
    return None

def get_expected_primary_index(current_time: Optional[datetime] = None) -> int:
    """返回优先级最高的主API索引（按配置顺序）"""
    first_available = get_first_available_primary_api_index()
    if first_available is not None:
        return first_available
    primary_indices = get_primary_api_indices()
    if primary_indices:
        return primary_indices[0]
    return 0

def find_primary_api_for_time(current_time: Optional[datetime] = None) -> Optional[int]:
    primary_indices = get_primary_api_indices()
    if not primary_indices:
        return None
    first_available = get_first_available_primary_api_index()
    if first_available is not None:
        return first_available
    return primary_indices[0]

def ensure_current_api_index(current_time: Optional[datetime] = None, reset_backup_state: bool = False) -> None:
    global current_config_index, is_using_backup, backup_start_time, last_primary_check_time, last_primary_switch_time
    if current_time is None:
        current_time = datetime.now()
    if not API_CONFIGS:
        current_config_index = -1
        is_using_backup = False
        backup_start_time = None
        last_primary_check_time = None
        return

    preferred = find_primary_api_for_time(current_time)
    if preferred is None:
        preferred = 0 if API_CONFIGS else -1

    if preferred is not None and preferred < len(API_CONFIGS):
        current_config_index = preferred
        if reset_backup_state or is_using_backup:
            is_using_backup = False
            backup_start_time = None
            last_primary_check_time = None
        last_primary_switch_time = current_time



def refresh_api_runtime_state(reset_backup_state: bool = False) -> None:
    global API_CONFIGS, api_status
    API_CONFIGS = config_mgr.get_enabled_api_configs()
    api_status = init_api_status()
    ensure_current_api_index(datetime.now(), reset_backup_state=reset_backup_state)


def refresh_codex_runtime_state(reset_backup_state: bool = False) -> None:
    global CODEX_CONFIGS, CODEX_DIRECT_CONFIG, codex_api_status
    global codex_current_config_index, codex_is_using_backup, codex_backup_start_time, codex_last_primary_check_time
    CODEX_CONFIGS = config_mgr.get_enabled_codex_configs()
    CODEX_DIRECT_CONFIG = config_mgr.get_codex_config()
    codex_api_status = init_codex_api_status()
    if CODEX_CONFIGS:
        preferred = get_first_available_primary_codex_index()
        if preferred is not None:
            codex_current_config_index = preferred
        else:
            codex_current_config_index = 0
    else:
        codex_current_config_index = -1
    if reset_backup_state:
        codex_is_using_backup = False
        codex_backup_start_time = None
        codex_last_primary_check_time = None


def refresh_openai_runtime_state() -> None:
    global OPENAI_TO_CLAUDE_CONFIGS
    OPENAI_TO_CLAUDE_CONFIGS = config_mgr.get_openai_to_claude_configs()


def refresh_model_conversion_state() -> None:
    global MODEL_CONVERSIONS
    MODEL_CONVERSIONS = config_mgr.get_enabled_model_conversions()


def refresh_retry_configs() -> None:
    global READ_TIMEOUT_RETRY_CONFIGS
    READ_TIMEOUT_RETRY_CONFIGS = config_mgr.get_enabled_retry_configs()


async def refresh_timeout_client() -> None:
    """刷新全局HTTP客户端的超时配置（使超时设置立即生效）"""
    global timeout, non_streaming_timeout, limits, client
    
    # 关闭旧的client实例
    try:
        await client.aclose()
    except Exception as e:
        print(f"关闭旧client时出错: {e}")
    
    # 重新读取超时配置
    timeout = TimeoutConfig.get_streaming_timeout()
    non_streaming_timeout = TimeoutConfig.get_non_streaming_timeout()
    
    # 重新创建连接限制（保持原配置）
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=100)
    
    # 创建新的client实例
    client = httpx.AsyncClient(timeout=timeout, limits=limits)

def get_primary_openai_to_claude_config() -> Dict[str, Any]:
    """获取首选的OpenAI转Claude配置"""
    for cfg in OPENAI_TO_CLAUDE_CONFIGS:
        if cfg.get("enabled", True):
            return cfg
    return OPENAI_TO_CLAUDE_CONFIGS[0] if OPENAI_TO_CLAUDE_CONFIGS else {}

# ========== Codex KEY切换逻辑 ==========
def is_codex_api_available(api_index):
    """检查Codex API是否可用（包括enabled状态和时间使能检查）"""
    if api_index >= len(CODEX_CONFIGS):
        return False
    
    # 获取Codex API配置
    codex_config = CODEX_CONFIGS[api_index]
    
    # 检查是否启用
    if not codex_config.get("enabled", True):
        return False
    
    # 检查时间使能
    time_enabled = codex_config.get("time_enabled", [1, 1, 1, 1, 1, 1, 1])
    if time_enabled:
        now = datetime.now()
        weekday = now.weekday()  # 0=周一, 1=周二, ..., 6=周日
        if weekday < len(time_enabled) and not time_enabled[weekday]:
            # 当前星期几不在使能范围内
            return False
    
    # 检查Codex API状态和冷却时间
    if api_index not in codex_api_status:
        return True
    
    status = codex_api_status[api_index]
    now = datetime.now()
    
    # 检查冷却时间
    if status["cooldown_until"] and now < status["cooldown_until"]:
        return False
    
    # 冷却时间过了，重置状态
    if status["cooldown_until"]:
        codex_api_status[api_index].update({"status": "normal", "error_count": 0, "cooldown_until": None})
        print(f"[{now.strftime('%H:%M:%S')}] Codex {CODEX_CONFIGS[api_index]['name']} 冷却期结束，恢复可用")

    return True  # 所有检查通过，Codex API可用

def get_primary_codex_indices() -> List[int]:
    return _get_primary_indices(CODEX_CONFIGS)

def get_first_available_primary_codex_index() -> Optional[int]:
    for idx in get_primary_codex_indices():
        if is_codex_api_available(idx):
            return idx
    return None

def get_codex_backup_api_indices():
    """获取备用Codex API的索引列表"""
    backup_indices = _get_backup_indices(CODEX_CONFIGS)
    return backup_indices if backup_indices else [len(CODEX_CONFIGS) - 1] if len(CODEX_CONFIGS) > 1 else []

def get_current_codex_config():
    """获取当前应该使用的Codex配置"""
    global codex_current_config_index, codex_is_using_backup, codex_backup_start_time, codex_last_primary_check_time
    
    # 如果没有配置，返回空配置
    if not CODEX_CONFIGS:
        return {"base_url": "", "key": "", "name": "未配置"}
    
    # 如果只有一个配置，直接返回
    if len(CODEX_CONFIGS) == 1:
        return CODEX_CONFIGS[0]
    
    with codex_key_switch_lock:
        now = datetime.now()
        primary_indices = get_primary_codex_indices()
        backup_indices = get_codex_backup_api_indices()

        if codex_current_config_index is None or codex_current_config_index < 0 or codex_current_config_index >= len(CODEX_CONFIGS):
            initial_primary = get_first_available_primary_codex_index()
            if initial_primary is not None:
                codex_current_config_index = initial_primary
            elif primary_indices:
                codex_current_config_index = primary_indices[0]
            else:
                codex_current_config_index = 0

        def _log_codex(message: str) -> None:
            print(f"[{now.strftime('%H:%M:%S')}] {message}")

        available_primary_indices = [idx for idx in primary_indices if is_codex_api_available(idx)]

        if codex_is_using_backup:
            check_interval = TimeoutConfig.get_primary_api_check_interval()
            should_check = False
            if codex_last_primary_check_time is None:
                should_check = True
            elif (now - codex_last_primary_check_time).total_seconds() >= check_interval:
                should_check = True
            if should_check:
                codex_last_primary_check_time = now
                print(f"[{now.strftime('%H:%M:%S')}] 备用Codex KEY使用中，开始{check_interval}秒定时检测主Codex KEY状态...")
                if available_primary_indices:
                    target_idx = available_primary_indices[0]
                    codex_is_using_backup = False
                    codex_backup_start_time = None
                    codex_last_primary_check_time = None
                    codex_current_config_index = target_idx
                    _log_codex(f"优先级调度：主Codex KEY恢复，切回 {CODEX_CONFIGS[target_idx]['name']}")
                    return CODEX_CONFIGS[codex_current_config_index]
                else:
                    print(f"[{now.strftime('%H:%M:%S')}] 主Codex KEY仍不可用，继续使用备用Codex KEY")

            for backup_idx in backup_indices:
                if is_codex_api_available(backup_idx):
                    if codex_current_config_index != backup_idx:
                        codex_backup_start_time = now
                        codex_last_primary_check_time = None
                        _log_codex(f"优先级调度：继续使用备用Codex KEY {CODEX_CONFIGS[backup_idx]['name']}")
                    codex_current_config_index = backup_idx
                    codex_is_using_backup = True
                    return CODEX_CONFIGS[codex_current_config_index]

            print(f"[{now.strftime('%H:%M:%S')}] 警告：所有备用Codex KEY都不可用，继续使用当前Codex KEY")
            return CODEX_CONFIGS[codex_current_config_index]

        if available_primary_indices:
            selected_index = available_primary_indices[0]
            if codex_current_config_index != selected_index:
                _log_codex(f"优先级调度：切换到主Codex KEY {CODEX_CONFIGS[selected_index]['name']}")
            codex_current_config_index = selected_index

            codex_is_using_backup = False
            codex_backup_start_time = None
            codex_last_primary_check_time = None
            return CODEX_CONFIGS[codex_current_config_index]

        for backup_idx in backup_indices:
            if is_codex_api_available(backup_idx):
                if codex_current_config_index != backup_idx or not codex_is_using_backup:
                    _log_codex(f"优先级调度：无可用主Codex KEY，切换到备用Codex KEY {CODEX_CONFIGS[backup_idx]['name']}")
                codex_is_using_backup = True
                codex_backup_start_time = now
                codex_last_primary_check_time = None
                codex_current_config_index = backup_idx
                return CODEX_CONFIGS[codex_current_config_index]

        print(f"[{now.strftime('%H:%M:%S')}] 警告：所有Codex KEY都不可用，继续使用当前Codex KEY")
        return CODEX_CONFIGS[codex_current_config_index]

def get_current_config():
    """获取当前应该使用的API配置"""
    global current_config_index, last_primary_switch_time, is_using_backup, backup_start_time, last_primary_check_time

    with key_switch_lock:
        now = datetime.now()

        if not API_CONFIGS:
            return {"base_url": "", "key": "", "name": "未配置"}

        if current_config_index is None or current_config_index < 0 or current_config_index >= len(API_CONFIGS):
            ensure_current_api_index(now, reset_backup_state=True)
            if current_config_index is None or current_config_index < 0 or current_config_index >= len(API_CONFIGS):
                current_config_index = 0

        primary_indices = get_primary_api_indices()
        backup_indices = get_backup_api_indices()

        def _log_switch(message: str) -> None:
            print(f"[{now.strftime('%H:%M:%S')}] {message}")

        available_primary_indices = [idx for idx in primary_indices if is_api_available(idx)]

        if is_using_backup:
            check_interval = TimeoutConfig.get_primary_api_check_interval()
            should_check = False
            if last_primary_check_time is None:
                should_check = True
            elif (now - last_primary_check_time).total_seconds() >= check_interval:
                should_check = True
            if should_check:
                last_primary_check_time = now
                print(f"[{now.strftime('%H:%M:%S')}] 备用API使用中，开始{check_interval}秒定时检测主API状态...")
                if available_primary_indices:
                    target_idx = available_primary_indices[0]
                    is_using_backup = False
                    backup_start_time = None
                    last_primary_check_time = None
                    last_primary_switch_time = now
                    current_config_index = target_idx
                    _log_switch(f"优先级调度：主API恢复，切回 {API_CONFIGS[target_idx]['name']}")
                    return API_CONFIGS[current_config_index]
                else:
                    print(f"[{now.strftime('%H:%M:%S')}] 主API仍不可用，继续使用备用API")

            for backup_idx in backup_indices:
                if is_api_available(backup_idx):
                    if current_config_index != backup_idx:
                        backup_start_time = now
                        last_primary_check_time = None
                        _log_switch(f"优先级调度：继续使用备用API {API_CONFIGS[backup_idx]['name']}")
                    current_config_index = backup_idx
                    is_using_backup = True
                    return API_CONFIGS[current_config_index]

            # 没有可用的备用API，保持当前索引
            return API_CONFIGS[current_config_index]

        if available_primary_indices:
            selected_index = available_primary_indices[0]
            if current_config_index != selected_index:
                _log_switch(f"优先级调度：切换到主API {API_CONFIGS[selected_index]['name']}")
                last_primary_switch_time = now

            is_using_backup = False
            backup_start_time = None
            last_primary_check_time = None
            current_config_index = selected_index
            return API_CONFIGS[current_config_index]

        # 主API均不可用，尝试使用备用API
        for backup_idx in backup_indices:
            if is_api_available(backup_idx):
                if current_config_index != backup_idx or not is_using_backup:
                    _log_switch(f"优先级调度：无可用主API，切换到备用API {API_CONFIGS[backup_idx]['name']}")
                    last_primary_switch_time = now
                is_using_backup = True
                backup_start_time = now
                last_primary_check_time = None
                current_config_index = backup_idx
                return API_CONFIGS[current_config_index]

        # 没有任何可用API，返回当前配置
        return API_CONFIGS[current_config_index]
def get_current_api_key():
    """获取当前应该使用的API key（保持向后兼容）"""
    config = get_current_config()
    global current_api_key
    current_api_key = config["key"]
    return current_api_key

def get_current_api_info():
    """获取当前API的详细信息，包括使用哪组KEY和还有多久换另一个KEY"""
    config = get_current_config()
    now = datetime.now()
    
    # 检查是否为备用API
    backup_indices = get_backup_api_indices()
    current_index = API_CONFIGS.index(config)
    
    if current_index in backup_indices or is_using_backup:
        base_info = f"使用: {config['name']} (备用API)"
    else:
        primary_indices = get_primary_api_indices()
        priority_rank = primary_indices.index(current_index) + 1 if current_index in primary_indices else current_index + 1
        base_info = f"使用: {config['name']} (主API，优先级#{priority_rank})"
    
    # 添加API冷却状态信息
    cooldown_info = []
    for i, api_config in enumerate(API_CONFIGS):
        if i in api_status and api_status[i]["cooldown_until"]:
            cooldown_until = api_status[i]["cooldown_until"]
            if now < cooldown_until:
                remaining_seconds = int((cooldown_until - now).total_seconds())
                remaining_minutes = remaining_seconds // 60
                remaining_seconds = remaining_seconds % 60
                if remaining_minutes > 0:
                    cooldown_info.append(f"{api_config['name']}冷却中({remaining_minutes}分{remaining_seconds}秒)")
                else:
                    cooldown_info.append(f"{api_config['name']}冷却中({remaining_seconds}秒)")
    
    if cooldown_info:
        return f"{base_info} | {' '.join(cooldown_info)}"
    else:
        return base_info

def get_current_codex_info():
    """获取当前Codex API的详细信息"""
    config = get_current_codex_config()
    now = datetime.now()

    # 检查是否为备用API
    backup_indices = get_codex_backup_api_indices()
    current_index = codex_current_config_index

    if current_index in backup_indices or codex_is_using_backup:
        base_info = f"使用: {config['name']} (备用Codex)"
    else:
        # 主Codex，显示优先级排名
        primary_indices = get_primary_codex_indices()
        priority_rank = primary_indices.index(current_index) + 1 if current_index in primary_indices else current_index + 1
        base_info = f"使用: {config['name']} (主Codex，优先级#{priority_rank})"

    # 添加Codex API冷却状态信息
    cooldown_info = []
    for i, codex_config in enumerate(CODEX_CONFIGS):
        if i in codex_api_status and codex_api_status[i]["cooldown_until"]:
            cooldown_until = codex_api_status[i]["cooldown_until"]
            if now < cooldown_until:
                remaining_seconds = int((cooldown_until - now).total_seconds())
                remaining_minutes = remaining_seconds // 60
                remaining_seconds = remaining_seconds % 60
                if remaining_minutes > 0:
                    cooldown_info.append(f"{codex_config['name']}冷却中({remaining_minutes}分{remaining_seconds}秒)")
                else:
                    cooldown_info.append(f"{codex_config['name']}冷却中({remaining_seconds}秒)")

    if cooldown_info:
        return f"{base_info} | {' '.join(cooldown_info)}"
    else:
        return base_info

def get_openai_to_claude_info():
    """获取OpenAI转Claude专用配置的详细信息"""
    # 获取首选的OpenAI转Claude配置
    enabled_configs = [cfg for cfg in OPENAI_TO_CLAUDE_CONFIGS if cfg.get("enabled", True)]

    if not enabled_configs:
        return "使用: OpenAI转Claude (未配置)"

    # 获取第一个启用的配置
    config = enabled_configs[0]
    config_name = config.get("name", "OpenAI转Claude")

    # 计算优先级排名
    priority_rank = enabled_configs.index(config) + 1 if config in enabled_configs else 1

    base_info = f"使用: {config_name} (#2 OpenAI转Claude专用，优先级#{priority_rank})"

    # 显示URL信息
    base_url = config.get("base_url", "")
    if base_url:
        base_info += f" ✓ 已启用\n🔗 {base_url}"
        key_preview = config.get("key", "")[:20] if config.get("key") else ""
        if key_preview:
            base_info += f"\n🔑 {key_preview}..."

    return base_info

USER_KEY_MAPPING = {
    "123": get_current_api_key,  # 用户使用简单key，映射到动态获取的API key
    # 可以添加更多用户key映射
}

# 调试开关 - 可以通过环境变量设置 PROXY_DEBUG=1 来启用详细调试
DEBUG = os.getenv("PROXY_DEBUG", "0") == "1"

# 完整日志记录开关 - 强制启用API输入输出日志
ENABLE_FULL_LOG = True  # 强制启用，记录所有API输入输出
MAX_LOG_SIZE = 3 * 1024 * 1024  # 3MB

# thinking功能开关已移除 - 现在通过参数过滤实现稳定性

def trim_log_file(log_filepath):
    """
    修剪日志文件，保留最近3MB的内容
    """
    try:
        if not os.path.exists(log_filepath):
            return
        
        file_size = os.path.getsize(log_filepath)
        if file_size <= MAX_LOG_SIZE:
            return
        
        # print(f"[日志管理] 日志文件超过{MAX_LOG_SIZE/1024/1024:.1f}MB，正在修剪...")
        
        # 读取最后3MB的内容
        with open(log_filepath, 'rb') as f:
            f.seek(-MAX_LOG_SIZE, 2)  # 从文件末尾向前移动3MB
            content = f.read()
        
        # 找到第一个换行符，确保从完整的一行开始
        first_newline = content.find(b'\n')
        if first_newline != -1:
            content = content[first_newline + 1:]
        
        # 写入修剪后的内容
        with open(log_filepath, 'wb') as f:
            f.write(content)
        
        # print(f"[日志管理] 日志文件修剪完成，剩余{len(content)/1024/1024:.1f}MB")
    except Exception as e:
        print(f"[日志管理] 修剪日志文件出错: {e}", file=sys.stderr)

def record_api_error(api_index, error_code, silent=False):
    """记录API错误"""
    threshold = TimeoutConfig.get_api_error_threshold()
    return _record_error_core(api_index, error_code, silent, api_status, API_CONFIGS, threshold, "API")

def record_codex_error(api_index, error_code, silent=False):
    """记录Codex API错误"""
    threshold = TimeoutConfig.get_codex_error_threshold()
    return _record_error_core(api_index, error_code, silent, codex_api_status, CODEX_CONFIGS, threshold, "Codex")

def get_error_strategy(error_code, error_type="http_status_code"):
    """
    获取错误的处理策略（完全由Web配置控制）
    
    Args:
        error_code: 错误码（HTTP状态码的数字或网络错误类型的字符串）
        error_type: 错误类型 ("http_status_code" 或 "network_error")
        
    Returns:
        strategy: "strategy_retry", "switch_api", "normal_retry", 或 None（不处理）
    """
    strategies = config_mgr.get_error_handling_strategies()
    
    if error_type == "http_status_code":
        http_codes = strategies.get("http_status_codes", {})
        # 先查找特定错误码，如果找不到则使用default默认策略
        strategy = http_codes.get(str(error_code))
        if strategy is None:
            strategy = http_codes.get("default")
        return strategy
    elif error_type == "network_error":
        network_errors = strategies.get("network_errors", {})
        # 先查找特定错误类型，如果找不到则使用default默认策略
        strategy = network_errors.get(error_code)
        if strategy is None:
            strategy = network_errors.get("default")
        return strategy

    return None

def is_api_available(api_index):
    """检查API是否可用（包括enabled状态和时间使能检查）"""

    if api_index >= len(API_CONFIGS):
        return False
    
    # 获取API配置
    api_config = API_CONFIGS[api_index]
    
    # 检查是否启用
    if not api_config.get("enabled", True):
        return False
    
    # 检查时间使能
    time_enabled = api_config.get("time_enabled", [1, 1, 1, 1, 1, 1, 1])
    if time_enabled:
        now = datetime.now()
        weekday = now.weekday()  # 0=周一, 1=周二, ..., 6=周日
        if weekday < len(time_enabled) and not time_enabled[weekday]:
            # 当前星期几不在使能范围内
            return False
    
    # 检查API状态和冷却时间
    if api_index not in api_status:
        return True
    
    status = api_status[api_index]
    now = datetime.now()
    
    # 检查冷却时间
    if status["cooldown_until"] and now < status["cooldown_until"]:
        remaining_seconds = int((status["cooldown_until"] - now).total_seconds())
        remaining_minutes = remaining_seconds // 60
        remaining_seconds = remaining_seconds % 60
        # 不在这里打印，避免日志过多，冷却信息会在get_current_api_info中显示
        return False
    
    # 冷却时间过了，重置状态
    if status["cooldown_until"]:
        api_status[api_index].update({"status": "normal", "error_count": 0, "cooldown_until": None})
        print(f"[{now.strftime('%H:%M:%S')}] API {API_CONFIGS[api_index]['name']} 冷却期结束，恢复可用")

    return True  # 所有检查通过，API可用

_initial_now = datetime.now()
ensure_current_api_index(_initial_now, reset_backup_state=True)

if CODEX_CONFIGS:
    initial_codex = get_first_available_primary_codex_index()
    if initial_codex is not None:
        codex_current_config_index = initial_codex
    else:
        codex_current_config_index = 0
else:
    codex_current_config_index = -1

if current_config_index >= 0 and API_CONFIGS:
    primary_indices = get_primary_api_indices()
    if current_config_index in primary_indices:
        priority_rank = primary_indices.index(current_config_index) + 1
        print(f"[启动] 当前主API: {API_CONFIGS[current_config_index]['name']} (优先级#{priority_rank})")
    else:
        print(f"[启动] 当前使用备用API: {API_CONFIGS[current_config_index]['name']}")
elif not API_CONFIGS:
    print("[启动] 未检测到可用的主API配置，请在后台补充或启用配置")
else:
    print("[启动] 尚未确定主API索引，将在首次请求时自动计算")

def smart_switch_api(current_api_index, error_code):
    """智能切换API - 三层策略（不记录错误，由调用方负责）"""
    global current_config_index, is_using_backup, backup_start_time, last_primary_check_time
    
    with key_switch_lock:
        now = datetime.now()
        
        # 不再在这里记录错误，由调用方负责（避免重复记录）
        # record_api_error(current_api_index, error_code)
        
        threshold = TimeoutConfig.get_api_error_threshold()

        # 检查错误计数是否达到切换阈值
        if api_status[current_api_index]["error_count"] < threshold:
            # 错误次数不足，不切换API，让重试逻辑继续使用当前API
            return False, current_api_index

        print(f"[{now.strftime('%H:%M:%S')}] API {API_CONFIGS[current_api_index]['name']} 连续{threshold}次错误，开始切换...")
        # 如果当前使用的是备用API，检查主API是否已恢复
        if is_using_backup:
            # 如果主API已恢复，切回主API继续执行后续逻辑
            primary_index = get_first_available_primary_api_index()
            if primary_index is not None and is_api_available(primary_index):
                print(f"[{now.strftime('%H:%M:%S')}] 备用API出错，但优先级主API已恢复，尝试切回主API {API_CONFIGS[primary_index]['name']}")
                is_using_backup = False
                backup_start_time = None
                current_config_index = primary_index
                return True, primary_index
            # 如果主API仍不可用，尝试切换到另一个备用API
        
        # 第一层：尝试切换到备用API
        backup_indices = get_backup_api_indices()
        for backup_idx in backup_indices:
            if is_api_available(backup_idx):
                old_api_name = API_CONFIGS[current_config_index]['name']
                is_using_backup = True
                backup_start_time = now
                last_primary_check_time = None
                current_config_index = backup_idx
                print(f"[{now.strftime('%H:%M:%S')}] 错误切换：从 {old_api_name} 切换到备用API {API_CONFIGS[backup_idx]['name']}")
                return True, backup_idx
        
        # 第三层：所有API都在冷却中，强制使用备用API
        # 收集所有API的冷却信息
        cooldown_details = []
        for i, api_config in enumerate(API_CONFIGS):
            if i in api_status and api_status[i]["cooldown_until"] and now < api_status[i]["cooldown_until"]:
                remaining_seconds = int((api_status[i]["cooldown_until"] - now).total_seconds())
                remaining_minutes = remaining_seconds // 60
                remaining_seconds = remaining_seconds % 60
                if remaining_minutes > 0:
                    cooldown_details.append(f"{api_config['name']}({remaining_minutes}分{remaining_seconds}秒)")
                else:
                    cooldown_details.append(f"{api_config['name']}({remaining_seconds}秒)")
        
        cooldown_info = ", ".join(cooldown_details) if cooldown_details else "各API冷却中"
        print(f"[{now.strftime('%H:%M:%S')}] 所有API都在冷却中({cooldown_info})，强制切换到备用API")
        
        is_using_backup = True
        backup_start_time = now
        last_primary_check_time = None  # 重置检测时间，确保立即检测
        
        # 强制使用第一个备用API
        backup_idx = backup_indices[0] if backup_indices else len(API_CONFIGS) - 1
        current_config_index = backup_idx
        print(f"[{now.strftime('%H:%M:%S')}] 强制使用备用API: {API_CONFIGS[backup_idx]['name']}")
        return True, backup_idx

def smart_codex_switch_api(current_api_index, error_code):
    """智能切换Codex API - 三层策略（不记录错误，由调用方负责）"""
    global codex_current_config_index, codex_is_using_backup, codex_backup_start_time, codex_last_primary_check_time
    
    with codex_key_switch_lock:
        now = datetime.now()
        
        # 不再内部记录错误，由调用方负责
        # record_codex_error(current_api_index, error_code)
        codex_threshold = TimeoutConfig.get_codex_error_threshold()

        if codex_api_status[current_api_index]["error_count"] < codex_threshold:
            return False, current_api_index

        print(f"[{now.strftime('%H:%M:%S')}] Codex API {CODEX_CONFIGS[current_api_index]['name']} 连续{codex_threshold}次错误，开始切换...")
        if codex_is_using_backup:
            primary_index = get_first_available_primary_codex_index()
            if primary_index is not None and is_codex_api_available(primary_index):
                print(f"[{now.strftime('%H:%M:%S')}] 备用Codex API出错，但优先级主API已恢复，尝试切回主API {CODEX_CONFIGS[primary_index]['name']}")
                codex_is_using_backup = False
                codex_backup_start_time = None
                codex_current_config_index = primary_index
                return True, primary_index
        
        backup_indices = get_codex_backup_api_indices()
        for backup_idx in backup_indices:
            if is_codex_api_available(backup_idx):
                old_api_name = CODEX_CONFIGS[codex_current_config_index]['name']
                codex_is_using_backup = True
                codex_backup_start_time = now
                codex_last_primary_check_time = None
                codex_current_config_index = backup_idx
                print(f"[{now.strftime('%H:%M:%S')}] 错误切换：从 {old_api_name} 切换到备用Codex API {CODEX_CONFIGS[backup_idx]['name']}")
                return True, backup_idx
        
        cooldown_details = []
        for i, codex_config in enumerate(CODEX_CONFIGS):
            if i in codex_api_status and codex_api_status[i]["cooldown_until"] and now < codex_api_status[i]["cooldown_until"]:
                remaining_seconds = int((codex_api_status[i]["cooldown_until"] - now).total_seconds())
                remaining_minutes = remaining_seconds // 60
                remaining_seconds = remaining_seconds % 60
                if remaining_minutes > 0:
                    cooldown_details.append(f"{codex_config['name']}({remaining_minutes}分{remaining_seconds}秒)")
                else:
                    cooldown_details.append(f"{codex_config['name']}({remaining_seconds}秒)")
        
        cooldown_info = ", ".join(cooldown_details) if cooldown_details else "各Codex API冷却中"
        print(f"[{now.strftime('%H:%M:%S')}] 所有Codex API都在冷却中({cooldown_info})，强制切换到备用API")
        
        codex_is_using_backup = True
        codex_backup_start_time = now
        codex_last_primary_check_time = None
        
        backup_idx = backup_indices[0] if backup_indices else len(CODEX_CONFIGS) - 1
        codex_current_config_index = backup_idx
        print(f"[{now.strftime('%H:%M:%S')}] 强制使用备用Codex API: {CODEX_CONFIGS[backup_idx]['name']}")
        return True, backup_idx

def switch_to_backup_api():
    """切换到备用API（保持向后兼容）"""
    current_api_index = current_config_index
    success, new_index = smart_switch_api(current_api_index, 429)  # 默认429错误
    return success

# 保持向后兼容
switch_to_backup_key = switch_to_backup_api

# 设置完整输入输出日志记录
def setup_full_logger():
    """设置完整输入输出的专用日志记录器"""
    if not ENABLE_FULL_LOG:
        return None
        
    full_logger = logging.getLogger('full_io_log')
    full_logger.setLevel(logging.INFO)
    
    # 避免重复添加处理器
    if not full_logger.handlers:
        try:
            # 使用脚本所在目录的绝对路径
            import os
            script_dir = os.path.dirname(os.path.abspath(__file__))
            log_filename = os.path.join(script_dir, "logs", "api_full_io.log")
            
            print(f"[日志初始化] 日志文件路径: {log_filename}")
            
            # 检查并修剪日志文件
            trim_log_file(log_filename)
            
            file_handler = logging.FileHandler(log_filename, encoding='utf-8', mode='a')
            file_handler.setLevel(logging.INFO)
            
            # 创建格式化器
            formatter = logging.Formatter(
                '%(asctime)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            file_handler.setFormatter(formatter)
            
            full_logger.addHandler(file_handler)
            full_logger.propagate = False  # 防止传播到根日志器
            
            print(f"[日志初始化] 日志记录器初始化成功")
            
        except Exception as e:
            print(f"[日志初始化] 日志记录器初始化失败: {e}", file=sys.stderr)
            return None
    
    return full_logger

# 初始化完整日志记录器
full_logger = setup_full_logger()

# 设置日志文件的绝对路径（用于其他函数引用）
LOG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "api_full_io.log")

def setup_original_data_logger():
    """设置发API前原数据的专用日志记录器"""
    if not ENABLE_FULL_LOG:
        return None
        
    orig_logger = logging.getLogger('original_data_log')
    orig_logger.setLevel(logging.INFO)
    
    # 避免重复添加处理器
    if not orig_logger.handlers:
        try:
            # 使用脚本所在目录的绝对路径
            script_dir = os.path.dirname(os.path.abspath(__file__))
            log_filename = os.path.join(script_dir, "logs", "api_original_data.log")
            
            print(f"[原数据日志初始化] 日志文件路径: {log_filename}")
            
            # 检查并修剪日志文件
            trim_log_file(log_filename)
            
            file_handler = logging.FileHandler(log_filename, encoding='utf-8', mode='a')
            file_handler.setLevel(logging.INFO)
            
            # 创建格式化器
            formatter = logging.Formatter(
                '%(asctime)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            file_handler.setFormatter(formatter)
            
            orig_logger.addHandler(file_handler)
            orig_logger.propagate = False  # 防止传播到根日志器
            
            print(f"[原数据日志初始化] 原数据日志记录器初始化成功")
            
        except Exception as e:
            print(f"[原数据日志初始化] 原数据日志记录器初始化失败: {e}", file=sys.stderr)
            return None
    
    return orig_logger

# 初始化原数据日志记录器
original_data_logger = setup_original_data_logger()

# 设置原数据日志文件的绝对路径
ORIGINAL_DATA_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "api_original_data.log")

def log_original_data(request_id, body, headers, method, path, is_codex_request=False):
    """记录发API前的完整原数据内容（不截断）"""
    if not ENABLE_FULL_LOG or not original_data_logger:
        return
    
    try:
        original_data_logger.info("="*40)
        request_type = "[Codex直连]" if is_codex_request else ""
        original_data_logger.info(f"【输入】{request_type} 请求ID: {request_id} | {method} {path}")
        
        # 记录完整请求体
        if body and method == "POST":
            try:
                request_data = json.loads(body.decode('utf-8'))
                # 直接记录完整数据，不进行任何截断
                original_data_logger.info(f"完整输入数据: {json.dumps(request_data, ensure_ascii=False)}")
            except Exception as e:
                # 非JSON格式，直接记录完整内容
                original_data_logger.info(f"完整输入数据(非JSON): {body.decode('utf-8', errors='ignore')}")
        
        trim_log_file(ORIGINAL_DATA_LOG_PATH)
        
    except Exception as e:
        print(f"记录输入数据时出错: {e}", file=sys.stderr)

def log_original_response(request_id, response_chunks, is_codex_request=False):
    """记录API响应的完整输出内容（不截断）"""
    if not ENABLE_FULL_LOG or not original_data_logger:
        return
    
    try:
        request_type = "[Codex直连]" if is_codex_request else ""
        original_data_logger.info(f"【输出】{request_type} 请求ID: {request_id}")
        
        # 合并所有响应块
        if response_chunks:
            full_response = b''.join(response_chunks)
            try:
                # 尝试解析为文本
                response_text = full_response.decode('utf-8', errors='ignore')
                original_data_logger.info(f"完整输出数据: {response_text}")
            except Exception as e:
                # 解码失败，记录十六进制
                original_data_logger.info(f"完整输出数据(十六进制): {full_response.hex()}")
        else:
            original_data_logger.info("完整输出数据: [空响应]")
        
        original_data_logger.info("="*40)
        trim_log_file(ORIGINAL_DATA_LOG_PATH)

    except Exception as e:
        print(f"记录输出数据时出错: {e}", file=sys.stderr)

def extract_usage_from_chunks(response_chunks, is_codex_request=False):
    """
    从响应chunks中提取usage数据

    Args:
        response_chunks: 响应数据块列表
        is_codex_request: 是否为Codex请求

    Returns:
        dict: usage数据，格式统一为：
            {input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, total_tokens}
    """
    try:
        # 合并所有chunks
        complete_response = b''.join(response_chunks)
        response_text = complete_response.decode('utf-8', errors='ignore')

        # 查找usage数据
        lines = response_text.split('\n')
        usage_lines_found = 0
        for line in lines:
            if line.startswith('data: ') and line != 'data: [DONE]':
                try:
                    json_str = line[6:]
                    data = json.loads(json_str)

                    # 检查是否包含usage
                    if 'usage' in data:
                        usage_lines_found += 1

                    if is_codex_request:
                        # Codex API格式：response.completed事件中包含usage
                        if data.get('type') == 'response.completed':
                            codex_usage = data.get('response', {}).get('usage', {})
                            if codex_usage:
                                # 提取缓存token（Codex使用input_tokens_details.cached_tokens）
                                input_tokens_details = codex_usage.get('input_tokens_details', {})
                                cached_tokens = input_tokens_details.get('cached_tokens', 0)

                                # Codex的input_tokens包含了新输入+缓存输入
                                # 需要分离出真正的新输入和缓存读取
                                total_input = codex_usage.get('input_tokens', 0)
                                new_input = total_input - cached_tokens

                                result = {
                                    'input_tokens': new_input,  # 新输入（非缓存）
                                    'output_tokens': codex_usage.get('output_tokens', 0),
                                    'cache_creation_input_tokens': 0,  # Codex缓存创建不单独计费
                                    'cache_read_input_tokens': cached_tokens,  # 缓存读取
                                    'total_tokens': (
                                        new_input +
                                        codex_usage.get('output_tokens', 0) +
                                        cached_tokens
                                    )
                                }
                                return result
                    else:
                        # Claude API格式：message_delta或message_stop事件中包含usage
                        if 'usage' in data:
                            usage = data['usage']
                            # 完整计算：包括所有tokens（input + output + cache_creation + cache_read）
                            result = {
                                'input_tokens': usage.get('input_tokens', 0),
                                'output_tokens': usage.get('output_tokens', 0),
                                'cache_creation_input_tokens': usage.get('cache_creation_input_tokens', 0),
                                'cache_read_input_tokens': usage.get('cache_read_input_tokens', 0),
                                'total_tokens': (
                                    usage.get('input_tokens', 0) +
                                    usage.get('output_tokens', 0) +
                                    usage.get('cache_creation_input_tokens', 0) +
                                    usage.get('cache_read_input_tokens', 0)
                                )
                            }
                            return result
                except Exception as parse_error:
                    continue

        return None
    except Exception as e:
        return None

def validate_and_replace_user_key(authorization_header):
    """
    验证用户Key并替换为真正的API Key
    
    Args:
        authorization_header: 用户提供的Authorization头
        
    Returns:
        tuple: (is_valid, real_api_key_header, error_message)
    """
    if not authorization_header:
        return False, None, "缺少Authorization头"
    
    # 解析Bearer token
    if not authorization_header.startswith('Bearer '):
        return False, None, "Authorization头格式错误，需要Bearer token"
    
    user_key = authorization_header[7:]  # 去掉'Bearer '前缀
    
    # 验证用户key是否存在于映射中
    if user_key not in USER_KEY_MAPPING:
        return False, None, f"无效的用户Key: {user_key}"
    
    # 获取真正的API key（支持动态获取）
    key_source = USER_KEY_MAPPING[user_key]
    if callable(key_source):
        real_api_key = key_source()  # 调用函数获取当前key
    else:
        real_api_key = key_source  # 直接使用静态key
    
    real_auth_header = f"Bearer {real_api_key}"
    
    return True, real_auth_header, None

def get_exact_test_headers():
    """获取验证成功的sonnet-4请求头配置（使用动态API key和防缓存头部）"""
    current_key = get_current_api_key()
    # 添加时间戳和随机数以避免网络缓存
    import time
    import random
    timestamp = int(time.time() * 1000)  # 毫秒级时间戳
    rand_id = random.randint(1000, 9999)
    return {
        'connection': 'keep-alive',
        'accept': 'application/json',
        'x-stainless-retry-count': '0',
        'x-stainless-timeout': '600',
        'x-stainless-lang': 'js',
        'x-stainless-package-version': '0.55.1',
        'x-stainless-os': 'Windows',
        'x-stainless-arch': 'x64',
        'x-stainless-runtime': 'node',
        'x-stainless-runtime-version': 'v22.17.0',
        'anthropic-dangerous-direct-browser-access': 'true',
        'anthropic-version': '2023-06-01',
        'x-app': 'cli',
        'user-agent': f'claude-cli/1.0.77 (external, cli, id-{rand_id})',
        'authorization': f'Bearer {current_key}',
        'content-type': 'application/json',
        'anthropic-beta': 'claude-code-20250219,interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14',
        'x-stainless-helper-method': 'stream',
        'accept-language': '*',
        'sec-fetch-mode': 'cors',
        'accept-encoding': 'gzip, deflate',
        # 添加强化防缓存头部
        'cache-control': 'no-cache, no-store, must-revalidate',
        'pragma': 'no-cache',
        'expires': '0',
        'x-request-id': f'{timestamp}-{rand_id}',
        'x-cache-bypass': f'{rand_id}'
    }

def debug_print(*args, **kwargs):
    """调试信息打印函数"""
    if DEBUG:
        print(*args, **kwargs, file=sys.stderr)

def detect_compressed_error(chunk_data):
    """
    检测并处理压缩的错误响应数据
    
    Args:
        chunk_data: 原始chunk数据（bytes）
        
    Returns:
        tuple: (is_error, error_info, decompressed_content)
    """
    try:
        # 将bytes转换为字符串进行分析
        if isinstance(chunk_data, bytes):
            chunk_text = chunk_data.decode('utf-8', errors='ignore')
        else:
            chunk_text = str(chunk_data)
        
        # 检查是否包含error事件
        if 'event: error' in chunk_text:
            print(f"[错误检测] 发现error事件")
            
            # 提取data部分
            lines = chunk_text.strip().split('\n')
            for line in lines:
                if line.startswith('data: '):
                    try:
                        data_json = line[6:]  # 去掉'data: '前缀
                        error_data = json.loads(data_json)
                        
                        # 检查是否有压缩的details
                        details = error_data.get('details', '')
                        if details and isinstance(details, str):
                            # 检测gzip压缩特征（以\x1f\x8b开头或包含这些转义字符）
                            if details.startswith('\x1f\x8b') or '\\u001f\\u008b' in details:
                                print(f"[错误检测] 发现压缩的错误详情数据")
                                
                                try:
                                    # 处理Unicode转义的压缩数据
                                    if '\\u001f\\u008b' in details:
                                        # 将Unicode转义序列转换为实际字节
                                        import codecs
                                        unescaped = codecs.decode(details, 'unicode_escape')
                                        compressed_data = unescaped.encode('latin-1')
                                    else:
                                        compressed_data = details.encode('latin-1')
                                    
                                    # 尝试解压缩
                                    decompressed = gzip.decompress(compressed_data).decode('utf-8')
                                    print(f"[错误检测] 解压缩成功，内容: {decompressed[:200]}...")
                                    
                                    # 更新错误数据
                                    error_data['details'] = decompressed
                                    error_data['details_decompressed'] = True
                                    
                                    return True, error_data, decompressed
                                    
                                except Exception as decompress_error:
                                    print(f"[错误检测] 解压缩失败: {decompress_error}")
                                    return True, error_data, details
                            else:
                                # 未压缩的错误详情
                                return True, error_data, details
                        
                        # 没有details字段但有error
                        return True, error_data, error_data.get('error', 'Unknown error')
                        
                    except json.JSONDecodeError as e:
                        print(f"[错误检测] JSON解析失败: {e}")
                        return True, {'error': 'JSON parse error', 'details': chunk_text}, chunk_text
            
            # 有error事件但无法解析data
            return True, {'error': 'Error event detected', 'details': chunk_text}, chunk_text
        
        # 检查是否包含403或其他关键错误信息
        error_keywords = ['401', '403', 'forbidden', 'unauthorized', 'access denied', 'invalid key', 'api key']
        if any(keyword in chunk_text.lower() for keyword in error_keywords):
            print(f"[错误检测] 发现关键错误词: {chunk_text[:200]}...")
            return True, {'error': 'Access error detected', 'details': chunk_text}, chunk_text
        
        return False, None, None
        
    except Exception as e:
        print(f"[错误检测] 处理异常: {e}")
        return False, None, None

def should_trigger_api_switch(error_info):
    """
    判断是否应该触发API切换
    
    Args:
        error_info: 错误信息字典
        
    Returns:
        tuple: (should_switch, error_code)
    """
    if not error_info:
        return False, None
    
    # 检查状态码
    status = error_info.get('status')
    if status in [401, 403, 429, 502, 503, 500]:
        return True, status
    
    # 检查错误内容
    error_msg = str(error_info.get('error', '')) + str(error_info.get('details', ''))
    error_msg_lower = error_msg.lower()
    
    # 定义错误类型映射，减少重复代码
    error_patterns = {
        401: ['401', 'unauthorized', 'invalid key', 'authentication', 'bearer token', 'not authorized'],
        403: ['403', 'forbidden', 'access denied', 'invalid key', 'unauthorized', 'authentication'],
        429: ['429', 'rate limit', 'too many requests'],
        502: ['502', 'bad gateway', '500', 'internal server'],
        503: ['503', 'service unavailable', 'unavailable', 'server unavailable', 'overloaded', 'temporarily unavailable']
    }
    
    for error_code, keywords in error_patterns.items():
        if any(keyword in error_msg_lower for keyword in keywords):
            return True, error_code
    
    return False, None

def handle_detected_error(request_id, error_info, decompressed_content, context=""):
    """
    统一处理检测到的错误，避免代码重复
    
    Args:
        request_id: 请求ID
        error_info: 错误信息
        decompressed_content: 解压后内容
        context: 上下文说明（如"流式"或"非流式"）
    """
    should_switch, error_code = should_trigger_api_switch(error_info)
    
    # 如果需要切换API，触发切换逻辑
    if should_switch:
        print(f"[{context}错误切换][{request_id}] 检测到错误码{error_code}，准备切换API")
        # 获取当前API索引
        current_api_index = current_config_index
        
        # 尝试智能切换API
        switch_success, new_api_index = smart_switch_api(current_api_index, error_code)
        
        if switch_success:
            if context == "流式":
                print(f"[{context}错误切换][{request_id}] 切换成功，但流式响应已开始，建议客户端重试")
            else:
                print(f"[{context}错误切换][{request_id}] 切换成功，建议客户端重试")

# 使用动态API端点配置
def build_upstream_url(clean_path, query_string=None, is_openai_format=False, base_url=None):
    """构建上游API的完整URL"""
    if base_url is None:
        config = get_current_config()
        base_url = config["base_url"]

    url = f"{base_url}/{clean_path}"
    
    if query_string:
        if is_openai_format:
            url += f"?{query_string}&beta=true"
        else:
            url += f"?{query_string}"
    elif is_openai_format:
        url += "?beta=true"
    
    return url

# 保持向后兼容
def get_current_base_url():
    """获取当前应该使用的基础URL（保持向后兼容）"""
    config = get_current_config()
    return config["base_url"]

# ===============================================
# 统一超时配置管理 - 提前定义以供全局使用
# ===============================================
class TimeoutConfig:
    """统一的超时配置管理类"""
    
    @classmethod
    def _get_settings(cls):
        """获取超时配置设置"""
        return config_mgr.get_timeout_settings()
    
    @classmethod
    def get_connect_timeout(cls):
        return cls._get_settings().get("connect_timeout", 60.0)
    
    @classmethod
    def get_write_timeout(cls):
        return cls._get_settings().get("write_timeout", 60.0)
    
    @classmethod
    def get_pool_timeout(cls):
        return cls._get_settings().get("pool_timeout", 120.0)
    
    @classmethod
    def get_streaming_read_timeout(cls):
        return cls._get_settings().get("streaming_read_timeout", 60.0)
    
    @classmethod
    def get_non_streaming_read_timeout(cls):
        return cls._get_settings().get("non_streaming_read_timeout", 60.0)
    
    @classmethod
    def get_extended_connect_timeout(cls):
        return cls._get_settings().get("extended_connect_timeout", 45.0)
    
    @classmethod
    def get_retry_read_timeout(cls):
        """获取重试请求的读取超时（默认60秒）"""
        return cls._get_settings().get("retry_read_timeout", 60.0)
    
    @classmethod
    def get_api_cooldown_seconds(cls):
        return cls._get_settings().get("api_cooldown_seconds", 600)

    @classmethod
    def get_api_error_threshold(cls):
        value = cls._get_settings().get("api_error_threshold", 3)
        try:
            threshold = int(value)
        except (TypeError, ValueError):
            return 3
        return threshold if threshold > 0 else 1

    @classmethod
    def get_codex_error_threshold(cls):
        value = cls._get_settings().get("codex_error_threshold", 3)
        try:
            threshold = int(value)
        except (TypeError, ValueError):
            return 3
        return threshold if threshold > 0 else 1

    
    @classmethod
    def get_codex_base_timeout(cls):
        return cls._get_settings().get("codex_base_timeout", 60)
    
    @classmethod
    def get_codex_timeout_increment(cls):
        return cls._get_settings().get("codex_timeout_increment", 60)
    
    @classmethod
    def get_codex_connect_timeout(cls):
        return cls._get_settings().get("codex_connect_timeout", 30.0)
    
    @classmethod
    def get_primary_api_check_interval(cls):
        return cls._get_settings().get("primary_api_check_interval", 30)
    
    @classmethod
    def get_billing_cycle_delay(cls):
        return cls._get_settings().get("billing_cycle_delay", 60)
    
    @classmethod
    def get_health_check_interval(cls):
        return cls._get_settings().get("health_check_interval", 0.5)
    
    @classmethod
    def get_billing_send_interval(cls):
        return cls._get_settings().get("billing_send_interval", 1.0)
    
    @classmethod
    def get_stream_retry_wait(cls):
        return cls._get_settings().get("stream_retry_wait", 1.0)
    
    @classmethod
    def get_max_retries(cls):
        """获取最大重试次数（switch_api策略使用）"""
        value = cls._get_settings().get("max_retries", 4)
        try:
            retries = int(value)
        except (TypeError, ValueError):
            retries = 4
        return retries if retries > 0 else 1
    
    @classmethod
    def get_modify_retry_headers(cls):
        """获取是否在重试时修改请求头（默认True）"""
        return cls._get_settings().get("modify_retry_headers", True)
    
    @classmethod
    def get_strategy_retry_status_codes(cls):
        """获取策略重试状态码集合（从错误处理策略配置读取）"""
        strategies = config_mgr.get_error_handling_strategies()
        http_codes = strategies.get("http_status_codes", {})
        # 跳过"default"键，只处理数字状态码
        retry_codes = [int(code) for code, strategy in http_codes.items()
                      if strategy == "strategy_retry" and code != "default"]
        return set(retry_codes) if retry_codes else {400, 404, 429, 500, 502, 503, 520, 521, 522, 524}
    
    @classmethod
    def get_network_error_strategy(cls, error_type: str) -> str:
        """获取网络错误的处理策略
        
        Args:
            error_type: 错误类型 ("ReadError", "ConnectError", "ReadTimeout")
            
        Returns:
            策略类型: "strategy_retry", "switch_api", "normal_retry"
        """
        strategies = config_mgr.get_error_handling_strategies()
        network_errors = strategies.get("network_errors", {})
        return network_errors.get(error_type, "switch_api")  # 默认切换API
    
    @classmethod
    def get_streaming_timeout(cls):
        """获取流式请求超时配置"""
        return httpx.Timeout(
            connect=cls.get_connect_timeout(),
            read=cls.get_streaming_read_timeout(),
            write=cls.get_write_timeout(),
            pool=cls.get_pool_timeout()
        )
    
    @classmethod
    def get_non_streaming_timeout(cls):
        """获取非流式请求超时配置"""
        return httpx.Timeout(
            connect=cls.get_connect_timeout(),
            read=cls.get_non_streaming_read_timeout(),
            write=cls.get_write_timeout(),
            pool=cls.get_pool_timeout()
        )
    
    @classmethod
    def get_retry_timeout(cls, is_non_streaming=False):
        """获取重试请求超时配置"""
        read_timeout = cls.get_non_streaming_read_timeout() if is_non_streaming else cls.get_retry_read_timeout()
        return httpx.Timeout(
            connect=cls.get_extended_connect_timeout(),
            read=read_timeout,
            write=cls.get_write_timeout(),
            pool=cls.get_pool_timeout()
        )
    
    @classmethod
    def get_strategy_retry_read_timeout(cls):
        """获取策略重试的读取超时（默认200秒）"""
        return cls._get_settings().get("strategy_retry_read_timeout", 200.0)
    
    @classmethod
    def get_strategy_retry_timeout(cls):
        """获取策略重试专用的超时配置"""
        return httpx.Timeout(
            connect=cls.get_extended_connect_timeout(),
            read=cls.get_strategy_retry_read_timeout(),
            write=cls.get_write_timeout(),
            pool=cls.get_pool_timeout()
        )

# 计费优化功能 - 定时启动计费周期
import time
import json

# 为计费启动功能创建同步HTTP客户端，禁用连接复用
billing_limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)
billing_client = httpx.Client(
    timeout=TimeoutConfig.get_non_streaming_timeout(),  # 使用统一的非流式超时配置
    limits=billing_limits
)

def send_billing_activation_message(api_index):
    """向指定API发送计费启动消息（使用OpenAI格式和错误检测）"""
    try:
        config = API_CONFIGS[api_index]
        url = f"{config['base_url']}/v1/messages"
        
        # 使用OpenAI格式的测试消息，转换为Claude格式（增加输出长度以确保计费）
        openai_payload = {
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": "请用100字左右简单介绍一下你自己的能力和特点"}],
            "stream": False
        }
        
        # 使用已有的OpenAI转换功能
        from openai_adapter import detect_and_convert_request
        is_openai, converted_payload, conversion_headers = detect_and_convert_request(openai_payload)
        
        # 使用转换后的头和负载
        headers = conversion_headers.copy()
        headers['authorization'] = f"Bearer {config['key']}"
        
        response = billing_client.post(url, json=converted_payload, headers=headers)
        
        # 使用增强的错误检测功能
        response_text = response.text
        response_content = response_text.encode('utf-8')
        
        # 检测错误
        is_error, error_info, decompressed_content = detect_compressed_error(response_content)
        
        if response.status_code == 200 and not is_error:
            # 检查响应内容是否正常
            try:
                response_data = response.json()
                # 检查是否有有效的内容
                if 'content' in response_data and response_data['content']:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ API健康: {config['name']} - 响应正常")
                    return True
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ API异常: {config['name']} - 响应内容为空")
                    return False
            except Exception as json_error:
                # JSON解析失败，显示详细错误信息
                content_type = response.headers.get('content-type', 'Unknown')
                response_preview = response_text[:200].replace('\n', '\\n').replace('\r', '\\r')
                
                # 检查不同类型的流式响应
                if content_type == 'text/event-stream':
                    # SSE (Server-Sent Events) 格式
                    if 'event:' in response_text or 'data:' in response_text:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ API健康: {config['name']} - SSE流式响应正常")
                        return True
                elif 'content' in response_text and 'text' in response_text:
                    # 传统流式响应格式
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ API健康: {config['name']} - 流式响应正常")
                    return True
                
                # 无法识别的响应格式
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ API异常: {config['name']} - 响应格式异常")
                print(f"    JSON解析错误: {str(json_error)}")
                print(f"    内容类型: {content_type}")
                print(f"    响应长度: {len(response_text)} 字符")
                print(f"    响应预览: {response_preview}{'...' if len(response_text) > 200 else ''}")
                return False
        else:
            # 有错误或状态码异常
            status_msg = f"状态码:{response.status_code}"
            error_msg = ""
            
            if is_error:
                error_msg = f" | 错误:{error_info.get('error', 'Unknown')}"
                if 'details' in error_info:
                    details = str(error_info['details'])[:100]
                    error_msg += f" | 详情:{details}"
            
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ API故障: {config['name']} - {status_msg}{error_msg}")
            
            # 检查是否需要触发API切换逻辑
            should_switch, error_code = should_trigger_api_switch(error_info)
            if should_switch:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 API需要切换: {config['name']} - 错误码:{error_code}")
            
            return False
            
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ API异常: {config['name']} - 连接错误: {e}")
        return False

def send_codex_billing_activation_message(api_index):
    """向指定Codex API发送计费启动消息（使用Codex格式）"""
    try:
        config = CODEX_CONFIGS[api_index]
        base_url = config['base_url']
        # 直接使用配置的base_url，不做处理
        # 正常Codex请求会发送到 base_url + /responses
        url = f"{base_url}/responses"
        
        # Codex身份识别指令（从真实Codex CLI提取）
        codex_instructions = """You are Codex, based on GPT-5. You are running as a coding agent in the Codex CLI on a user's computer.

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
- You are running sandboxed and need to run a command that requires network access (e.g. installing packages)
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
- The user does not command execution outputs. When asked to show the output of a command (e.g. `git show`), relay the important details in your answer or summarize the key lines so the user understands the result.

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
        
        # 构建环境上下文（Codex CLI必需）
        import os
        env_context = {
            'cwd': os.path.abspath('.'),
            'approval_policy': 'on-request',
            'sandbox_mode': 'workspace-write',
            'network_access': 'enabled',
            'shell': 'powershell.exe' if os.name == 'nt' else 'bash'
        }
        
        env_text = f"<environment_context>\n  <cwd>{env_context['cwd']}</cwd>\n  <approval_policy>{env_context['approval_policy']}</approval_policy>\n  <sandbox_mode>{env_context['sandbox_mode']}</sandbox_mode>\n  <network_access>{env_context['network_access']}</network_access>\n  <shell>{env_context['shell']}</shell>\n</environment_context>"
        
        # 构建Codex格式的输入消息（第一条必须是环境上下文）
        codex_input = [
            {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": env_text
                }]
            },
            {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "请简单介绍一下你自己"
                }]
            }
        ]
        
        # 完整的Codex格式payload（包含身份识别）
        payload = {
            "model": "gpt-5-codex",
            "instructions": codex_instructions,
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
                                "description": "Only set if with_escalated_permissions is true. 1-sentence explanation of why we want to run this command."
                            },
                            "timeout_ms": {
                                "type": "number",
                                "description": "The timeout for the command in milliseconds"
                            },
                            "with_escalated_permissions": {
                                "type": "boolean",
                                "description": "Whether to request escalated permissions. Set to true if command needs to be run without sandbox restrictions"
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
                    "description": "Updates the task plan.\\nProvide an optional explanation and a list of plan items, each with a step and status.\\nAt most one step can be in_progress at a time.\\n",
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
                                },
                                "description": "The list of steps"
                            }
                        },
                        "required": ["plan"],
                        "additionalProperties": False
                    }
                },
                {
                    "type": "function",
                    "name": "view_image",
                    "description": "Attach a local image (by filesystem path) to the conversation context for this turn.",
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
        
        # 生成会话ID（每次请求生成新的UUID）
        import uuid as uuid_module
        session_id = str(uuid_module.uuid4())
        
        # 从base_url提取host
        from urllib.parse import urlparse
        parsed_url = urlparse(base_url if base_url.startswith('http') else f"https://{base_url}")
        actual_host = parsed_url.netloc
        
        # 构建完整的Codex CLI headers（模拟真实Codex CLI）
        headers = {
            "authorization": f"Bearer {config['key']}",
            "version": "0.42.0",
            "openai-beta": "responses=experimental",
            "conversation_id": session_id,
            "session_id": session_id,
            "accept": "text/event-stream",
            "content-type": "application/json",
            "user-agent": "codex_cli_rs/0.42.0 (Windows 10.0.19045; x86_64) unknown",
            "originator": "codex_cli_rs",
            "host": actual_host  # 使用实际的host，而不是chatgpt.com
        }
        
        timeout_config = httpx.Timeout(
            connect=TimeoutConfig.get_connect_timeout(),
            read=30.0,
            write=TimeoutConfig.get_write_timeout(),
            pool=TimeoutConfig.get_pool_timeout()
        )

        resp = billing_client.post(url, json=payload, headers=headers, timeout=timeout_config)
        
        # 打印详细的响应信息
        print(f"[RESPONSE STATUS] {resp.status_code}")
        print(f"[RESPONSE HEADERS]")
        for key, value in resp.headers.items():
            print(f"  {key}: {value}")
        print(f"[RESPONSE BODY]")
        response_preview = resp.text[:1000] if len(resp.text) > 1000 else resp.text
        print(f"{response_preview}")
        if len(resp.text) > 1000:
            print(f"... (总长度: {len(resp.text)} 字符)")
        print("=" * 80)
        
        if resp.status_code == 200:
            try:
                response_text = resp.text
                
                # 首先检查是否是简单的success响应（某些配置下的激活确认）
                try:
                    simple_json = json.loads(response_text)
                    if simple_json.get("success") == True:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] Codex正常: {config['name']} (状态码: 200, 激活确认: success=true)")
                        return True
                except (json.JSONDecodeError, ValueError):
                    pass
                
                # Codex返回流式响应，需要解析SSE格式
                has_content = False
                content_pieces = []
                
                for line in response_text.split('\n'):
                    if line.startswith('data: '):
                        data = line[6:]
                        if data == '[DONE]':
                            break
                        try:
                            event = json.loads(data)
                            # 检查output_text.delta事件
                            if event.get("type") == "response.output_text.delta":
                                delta = event.get("delta", "")
                                if delta:
                                    content_pieces.append(delta)
                                    has_content = True
                        except json.JSONDecodeError:
                            continue
                
                if has_content:
                    content = ''.join(content_pieces)
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] Codex正常: {config['name']} (状态码: 200, 内容长度: {len(content)})")
                    return True
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [WARN] Codex响应异常: {config['name']} - 状态码200但内容为空")
                    return False
                    
            except Exception as parse_error:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [WARN] Codex响应异常: {config['name']} - 解析失败: {parse_error}")
                return False
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] Codex错误: {config['name']} - 状态码: {resp.status_code}")
            return False
            
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] Codex异常: {config['name']} - 连接错误: {e}")
        return False

def startup_api_health_check():
    """启动时对所有API进行健康检查"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 开始启动时API健康检查...")
    print("=" * 60)
    
    healthy_apis = []
    failed_apis = []
    
    # 检查所有API配置
    for i, config in enumerate(API_CONFIGS):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 检查 {config['name']}...")
        
        is_healthy = send_billing_activation_message(i)
        if is_healthy:
            healthy_apis.append(config['name'])
        else:
            failed_apis.append(config['name'])
        
        time.sleep(TimeoutConfig.get_health_check_interval())  # 从配置读取健康检查间隔
    
    # 总结报告
    print("=" * 60)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📊 API健康检查结果:")
    
    if healthy_apis:
        print(f"✅ 健康API ({len(healthy_apis)}个): {', '.join(healthy_apis)}")
    
    if failed_apis:
        print(f"❌ 故障API ({len(failed_apis)}个): {', '.join(failed_apis)}")
    
    if not failed_apis:
        print("🎉 所有API运行正常!")
    else:
        print(f"⚠️  {len(failed_apis)}/{len(API_CONFIGS)} API存在问题，请检查")
    
    print("=" * 60)
    return len(healthy_apis), len(failed_apis)

# API定时激活状态管理
api_activation_status = {}
codex_activation_status = {}

def init_activation_status():
    """初始化API激活状态"""
    return _init_activation_status_core(API_CONFIGS)

def init_codex_activation_status():
    """初始化Codex激活状态"""
    return _init_activation_status_core(CODEX_CONFIGS)

def api_activation_scheduler():
    """API定时激活调度器 - 每分钟检查是否需要激活，失败则重试最多20次"""
    global api_activation_status, codex_activation_status
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🕐 API定时激活调度器已启动（Claude + Codex）")
    
    last_check_date = None
    
    while True:
        try:
            now = datetime.now()
            current_date = now.date()
            current_time = now.strftime('%H:%M')
            
            # 检查是否是新的一天，如果是则重置所有状态
            if last_check_date != current_date:
                api_activation_status = init_activation_status()
                codex_activation_status = init_codex_activation_status()
                last_check_date = current_date
                if api_activation_status or codex_activation_status:
                    print(f"[{now.strftime('%H:%M:%S')}] 🔄 新的一天，重置API激活状态（Claude: {len(api_activation_status)}个, Codex: {len(codex_activation_status)}个）")
            
            # 检查每个启用激活的Claude API
            for i, config in enumerate(API_CONFIGS):
                if not config.get('activation_enabled', False):
                    continue
                
                activation_time = config.get('activation_time', '08:00')
                
                # 确保该API有状态记录
                if i not in api_activation_status:
                    api_activation_status[i] = {
                        "retry_count": 0,
                        "last_attempt_date": None,
                        "activated_today": False,
                        "last_attempt_time": None
                    }
                
                status = api_activation_status[i]
                
                # 如果今天已成功激活，跳过
                if status['activated_today']:
                    continue
                
                # 检查是否到达激活时间或需要重试
                should_try = False
                
                # 情况1：到达指定激活时间，且今天还没尝试过
                if current_time == activation_time and status['last_attempt_date'] != current_date:
                    should_try = True
                    reason = "到达激活时间"
                
                # 情况2：已经尝试过但失败，需要重试（每分钟重试一次）
                elif (status['retry_count'] > 0 and 
                      status['retry_count'] < 20 and 
                      status['last_attempt_time'] is not None and
                      (now - status['last_attempt_time']).total_seconds() >= 60):
                    should_try = True
                    reason = f"重试第{status['retry_count']}次"
                
                if should_try:
                    print(f"[{now.strftime('%H:%M:%S')}] 🔔 Claude API激活: {config['name']} - {reason}")
                    
                    # 发送激活消息
                    result = send_billing_activation_message(i)
                    status['last_attempt_time'] = now
                    status['last_attempt_date'] = current_date
                    
                    if result:
                        # 激活成功
                        status['activated_today'] = True
                        status['retry_count'] = 0
                        
                        # 计算下次激活时间(成功时间+1分钟)
                        next_activation_time = (now + timedelta(minutes=1)).strftime('%H:%M')
                        
                        # 找到当前配置在全部配置中的原始索引（避免索引错位）
                        all_api_configs = config_mgr.get_api_configs()
                        original_index = None
                        for idx, cfg in enumerate(all_api_configs):
                            if cfg.get('name') == config.get('name'):
                                original_index = idx
                                break
                        
                        # 只更新激活时间字段
                        if original_index is not None:
                            config_mgr.update_api_config(original_index, {'activation_time': next_activation_time})
                        
                        print(f"[{now.strftime('%H:%M:%S')}] ✅ Claude API激活成功: {config['name']}")
                        print(f"[{now.strftime('%H:%M:%S')}] ⏰ 下次激活时间已更新为: {next_activation_time}")
                    else:
                        # 激活失败，增加重试计数
                        status['retry_count'] += 1
                        if status['retry_count'] >= 20:
                            print(f"[{now.strftime('%H:%M:%S')}] ❌ Claude API激活失败: {config['name']} - 已达到最大重试次数(20次)，明天继续")
                        else:
                            print(f"[{now.strftime('%H:%M:%S')}] ⚠️ Claude API激活失败: {config['name']} - 1分钟后重试 (已重试{status['retry_count']}/20次)")
                    
                    time.sleep(TimeoutConfig.get_billing_send_interval())
            
            # 检查每个启用激活的Codex API
            for i, config in enumerate(CODEX_CONFIGS):
                if not config.get('activation_enabled', False):
                    continue
                
                activation_time = config.get('activation_time', '08:00')
                
                # 确保该Codex API有状态记录
                if i not in codex_activation_status:
                    codex_activation_status[i] = {
                        "retry_count": 0,
                        "last_attempt_date": None,
                        "activated_today": False,
                        "last_attempt_time": None
                    }
                
                status = codex_activation_status[i]
                
                # 如果今天已成功激活，跳过
                if status['activated_today']:
                    continue
                
                # 检查是否到达激活时间或需要重试
                should_try = False
                
                # 情况1：到达指定激活时间，且今天还没尝试过
                if current_time == activation_time and status['last_attempt_date'] != current_date:
                    should_try = True
                    reason = "到达激活时间"
                
                # 情况2：已经尝试过但失败，需要重试（每分钟重试一次）
                elif (status['retry_count'] > 0 and 
                      status['retry_count'] < 20 and 
                      status['last_attempt_time'] is not None and
                      (now - status['last_attempt_time']).total_seconds() >= 60):
                    should_try = True
                    reason = f"重试第{status['retry_count']}次"
                
                if should_try:
                    print(f"[{now.strftime('%H:%M:%S')}] 🔔 Codex API激活: {config['name']} - {reason}")
                    
                    # 发送激活消息（使用Codex的send函数）
                    result = send_codex_billing_activation_message(i)
                    status['last_attempt_time'] = now
                    status['last_attempt_date'] = current_date
                    
                    if result:
                        # 激活成功
                        status['activated_today'] = True
                        status['retry_count'] = 0
                        
                        # 计算下次激活时间(成功时间+1分钟)
                        next_activation_time = (now + timedelta(minutes=1)).strftime('%H:%M')
                        
                        # 找到当前配置在全部配置中的原始索引（避免索引错位）
                        all_codex_configs = config_mgr.get_codex_configs()
                        original_index = None
                        for idx, cfg in enumerate(all_codex_configs):
                            if cfg.get('name') == config.get('name'):
                                original_index = idx
                                break
                        
                        # 只更新激活时间字段
                        if original_index is not None:
                            config_mgr.update_codex_config(original_index, {'activation_time': next_activation_time})
                        
                        print(f"[{now.strftime('%H:%M:%S')}] ✅ Codex API激活成功: {config['name']}")
                        print(f"[{now.strftime('%H:%M:%S')}] ⏰ 下次激活时间已更新为: {next_activation_time}")
                    else:
                        # 激活失败，增加重试计数
                        status['retry_count'] += 1
                        if status['retry_count'] >= 20:
                            print(f"[{now.strftime('%H:%M:%S')}] ❌ Codex API激活失败: {config['name']} - 已达到最大重试次数(20次)，明天继续")
                        else:
                            print(f"[{now.strftime('%H:%M:%S')}] ⚠️ Codex API激活失败: {config['name']} - 1分钟后重试 (已重试{status['retry_count']}/20次)")
                    
                    time.sleep(TimeoutConfig.get_billing_send_interval())
            
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ 激活调度器错误: {e}")
        
        # 每分钟检查一次
        time.sleep(60)

def billing_scheduler():
    """计费周期调度器 - 在4、5、6、9、10、11点发送启动消息"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🕐 计费调度器已启动")
    
    while True:
        now = datetime.now()
        current_hour = now.hour
        current_minute = now.minute
        
        # 检查是否为4、5、6、9、10、11点的第0分钟
        if (current_hour in [4, 5, 6, 9, 10, 11]) and current_minute == 0:
            print(f"[{now.strftime('%H:%M:%S')}] 🔄 开始计费周期启动检查 ({current_hour}点)")
            print("-" * 40)
            
            healthy_count = 0
            # 向所有API发送启动消息（包括备用API）
            for i in range(len(API_CONFIGS)):
                result = send_billing_activation_message(i)
                if result:
                    healthy_count += 1
                time.sleep(TimeoutConfig.get_billing_send_interval())  # 从配置读取计费发送间隔
            
            print("-" * 40)
            print(f"[{now.strftime('%H:%M:%S')}] ✅ 计费周期检查完成: {healthy_count}/{len(API_CONFIGS)} API正常")
            
            # 等待指定秒数，避免在同一分钟内重复发送
            delay = TimeoutConfig.get_billing_cycle_delay()
            time.sleep(delay + 1)
        else:
            # 每分钟检查一次
            delay = TimeoutConfig.get_billing_cycle_delay()
            time.sleep(delay)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 - 启动和关闭事件处理"""
    # 启动时执行 - 先进行API健康检查
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Claude Code API Server starting...")
    
    # 【已注释】执行启动时API健康检查
    # healthy_count, failed_count = startup_api_health_check()
    
    # 【已注释】启动计费调度器
    # billing_thread = threading.Thread(target=billing_scheduler, daemon=True)
    # billing_thread.start()
    # print(f"[{datetime.now().strftime('%H:%M:%S')}] 🕐 计费优化调度器已启动 - 将在每天4、5、6、9、10、11点发送计费启动消息")
    
    # 启动API定时激活调度器
    activation_thread = threading.Thread(target=api_activation_scheduler, daemon=True)
    activation_thread.start()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] API定时激活调度器已启动")
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] 服务器启动完成")
    
    yield
    
    # 关闭时执行（如果需要的话）
    pass

app = FastAPI(lifespan=lifespan)

# 静态文件服务 - 提供 chart.min.js
@app.get("/chart.min.js")
async def get_chart_js():
    """提供 Chart.js 静态文件"""
    chart_file = os.path.join(os.path.dirname(__file__), "chart.min.js")
    if os.path.exists(chart_file):
        return FileResponse(chart_file, media_type="application/javascript")
    return JSONResponse({"error": "Chart.js file not found"}, status_code=404)

# Web管理API端点
@app.get("/", response_class=HTMLResponse)
async def admin_page():
    html_file = os.path.join(os.path.dirname(__file__), "admin.html")
    if os.path.exists(html_file):
        with open(html_file, 'r', encoding='utf-8') as f:
            return f.read()
    return "<h1>管理页面未找到</h1>"

@app.get("/token-stats.html", response_class=HTMLResponse)
async def token_stats_page():
    """Token统计页面"""
    html_file = os.path.join(os.path.dirname(__file__), "token_stats.html")
    if os.path.exists(html_file):
        with open(html_file, 'r', encoding='utf-8') as f:
            return f.read()
    return "<h1>统计页面未找到</h1>"

@app.post("/api/token-stats/generate")
async def generate_token_stats():
    """
    生成Token统计数据（已改为实时统计模式）

    注意：统计功能已改为实时记录模式，无需手动触发生成。
    每次API调用都会自动记录usage数据并更新token_stats.json文件。
    """
    return {
        "success": True,
        "message": "统计功能已改为实时记录模式，数据自动更新",
        "note": "每次API调用都会自动记录，无需手动刷新"
    }

@app.get("/api/token-stats")
async def get_token_stats():
    """获取Token统计数据（实时统计模式）"""
    try:
        stats_file = os.path.join(os.path.dirname(__file__), "json_data", "token_stats.json")

        if not os.path.exists(stats_file):
            # 如果文件不存在，返回空统计数据结构
            return {
                "summary": {
                    "total_requests": 0,
                    "total_tokens": 0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cache_creation_tokens": 0,
                    "total_cache_read_tokens": 0,
                    "unique_models": []
                },
                "by_model": {},
                "daily": {},
                "generated_at": "未生成（等待首次API调用）"
            }

        with open(stats_file, 'r', encoding='utf-8') as f:
            stats_data = json.load(f)

        return stats_data
    except Exception as e:
        return {"error": f"读取统计数据失败: {str(e)}"}

@app.post("/api/token-stats/reset")
async def reset_token_stats():
    """重置/清空Token统计数据"""
    try:
        stats_file = os.path.join(os.path.dirname(__file__), "json_data", "token_stats.json")

        if os.path.exists(stats_file):
            os.remove(stats_file)
            return {"success": True, "message": "统计数据已清空"}
        else:
            return {"success": True, "message": "统计文件不存在，无需清空"}
    except Exception as e:
        return {"success": False, "message": f"清空失败: {str(e)}"}

# ========== API配置管理端点 ==========
@app.get("/api/configs")
async def get_api_configs():
    return {"configs": config_mgr.get_api_configs()}

@app.post("/api/configs")
async def add_api_config(config: dict):
    success = config_mgr.add_api_config(config)
    if success:
        refresh_api_runtime_state(reset_backup_state=True)
        return {"success": True, "message": "配置已添加"}
    return {"success": False, "message": "配置添加失败"}

@app.put("/api/configs/{index}")
async def update_api_config(index: int, config: dict):
    success = config_mgr.update_api_config(index, config)
    if success:
        refresh_api_runtime_state()
        return {"success": True, "message": "配置已更新"}
    return {"success": False, "message": "配置更新失败"}

@app.delete("/api/configs/{index}")
async def delete_api_config(index: int):
    success = config_mgr.delete_api_config(index)
    if success:
        refresh_api_runtime_state(reset_backup_state=True)
        return {"success": True, "message": "配置已删除"}
    return {"success": False, "message": "配置删除失败"}

@app.post("/api/configs/{index}/toggle")
async def toggle_api_config(index: int):
    enabled = config_mgr.toggle_api_config(index)
    if enabled is not None:
        refresh_api_runtime_state(reset_backup_state=not enabled)
        return {"success": True, "enabled": enabled, "message": f"配置已{'启用' if enabled else '禁用'}"}
    return {"success": False, "message": "切换失败"}

@app.post("/api/configs/{index}/move")
async def move_api_config(index: int, direction: dict):
    success = config_mgr.move_api_config(index, direction.get("direction"))
    if success:
        refresh_api_runtime_state()
        return {"success": True, "message": "配置已移动"}
    return {"success": False, "message": "配置移动失败"}

@app.post("/api/configs/{index}/duplicate")
async def duplicate_api_config(index: int):
    success = config_mgr.duplicate_api_config(index)
    if success:
        refresh_api_runtime_state()
        return {"success": True, "message": "配置已复制"}
    return {"success": False, "message": "配置复制失败"}

# ========== Codex配置管理端点 ==========
@app.get("/api/codex")
async def get_codex_configs():
    return {"configs": config_mgr.get_codex_configs()}

@app.post("/api/codex")
async def add_codex_config(config: dict):
    success = config_mgr.add_codex_config(config)
    if success:
        refresh_codex_runtime_state(reset_backup_state=True)
        return {"success": True, "message": "Codex配置已添加"}
    return {"success": False, "message": "Codex配置添加失败"}

@app.put("/api/codex/{index}")
async def update_codex_config(index: int, config: dict):
    success = config_mgr.update_codex_config(index, config)
    if success:
        refresh_codex_runtime_state()
        return {"success": True, "message": "Codex配置已更新"}
    return {"success": False, "message": "Codex配置更新失败"}

@app.delete("/api/codex/{index}")
async def delete_codex_config(index: int):
    success = config_mgr.delete_codex_config(index)
    if success:
        refresh_codex_runtime_state(reset_backup_state=True)
        return {"success": True, "message": "Codex配置已删除"}
    return {"success": False, "message": "Codex配置删除失败"}

@app.post("/api/codex/{index}/toggle")
async def toggle_codex_config(index: int):
    enabled = config_mgr.toggle_codex_config(index)
    if enabled is not None:
        refresh_codex_runtime_state(reset_backup_state=not enabled)
        return {"success": True, "enabled": enabled, "message": f"Codex配置已{'启用' if enabled else '禁用'}"}
    return {"success": False, "message": "切换失败"}

@app.post("/api/codex/{index}/move")
async def move_codex_config(index: int, direction: dict):
    success = config_mgr.move_codex_config(index, direction.get("direction"))
    if success:
        refresh_codex_runtime_state()
        return {"success": True, "message": "Codex配置已移动"}
    return {"success": False, "message": "Codex配置移动失败"}

@app.post("/api/codex/{index}/duplicate")
async def duplicate_codex_config(index: int):
    success = config_mgr.duplicate_codex_config(index)
    if success:
        refresh_codex_runtime_state()
        return {"success": True, "message": "Codex配置已复制"}
    return {"success": False, "message": "Codex配置复制失败"}

# ========== OpenAI转Claude配置管理端点 ==========
@app.get("/api/openai-to-claude")
async def get_openai_to_claude():
    return {"configs": config_mgr.get_openai_to_claude_configs()}


@app.post("/api/openai-to-claude")
async def add_openai_to_claude(config: dict):
    success = config_mgr.add_openai_to_claude_config(config)
    if success:
        refresh_openai_runtime_state()
        return {"success": True, "message": "OpenAI转Claude配置已添加"}
    return {"success": False, "message": "OpenAI转Claude配置添加失败"}


@app.put("/api/openai-to-claude/{index}")
async def update_openai_to_claude(index: int, config: dict):
    success = config_mgr.update_openai_to_claude_config(index, config)
    if success:
        refresh_openai_runtime_state()
        return {"success": True, "message": "OpenAI转Claude配置已更新"}
    return {"success": False, "message": "OpenAI转Claude配置更新失败"}


@app.delete("/api/openai-to-claude/{index}")
async def delete_openai_to_claude(index: int):
    success = config_mgr.delete_openai_to_claude_config(index)
    if success:
        refresh_openai_runtime_state()
        return {"success": True, "message": "OpenAI转Claude配置已删除"}
    return {"success": False, "message": "OpenAI转Claude配置删除失败"}


@app.post("/api/openai-to-claude/{index}/toggle")
async def toggle_openai_to_claude(index: int):
    result = config_mgr.toggle_openai_to_claude_config(index)
    if result is not None:
        refresh_openai_runtime_state()
        status_text = "启用" if result else "禁用"
        return {"success": True, "message": f"OpenAI转Claude配置已{status_text}"}
    return {"success": False, "message": "OpenAI转Claude配置切换失败"}


@app.post("/api/openai-to-claude/{index}/move")
async def move_openai_to_claude(index: int, payload: dict):
    direction = payload.get("direction")
    success = config_mgr.move_openai_to_claude_config(index, direction)
    if success:
        refresh_openai_runtime_state()
        return {"success": True, "message": "OpenAI转Claude配置已移动"}
    return {"success": False, "message": "OpenAI转Claude配置移动失败"}

@app.post("/api/openai-to-claude/{index}/duplicate")
async def duplicate_openai_to_claude_config(index: int):
    success = config_mgr.duplicate_openai_to_claude_config(index)
    if success:
        refresh_openai_runtime_state()
        return {"success": True, "message": "OpenAI转Claude配置已复制"}
    return {"success": False, "message": "OpenAI转Claude配置复制失败"}

# ========== 超时重试配置管理端点 ==========
@app.get("/api/retry")
async def get_retry_configs():
    return {"configs": config_mgr.get_retry_configs()}

@app.post("/api/retry")
async def add_retry_config(config: dict):
    success = config_mgr.add_retry_config(config)
    if success:
        refresh_retry_configs()
        return {"success": True, "message": "重试配置已添加"}
    return {"success": False, "message": "重试配置添加失败"}

@app.put("/api/retry/{index}")
async def update_retry_config(index: int, config: dict):
    success = config_mgr.update_retry_config(index, config)
    if success:
        refresh_retry_configs()
        return {"success": True, "message": "重试配置已更新"}
    return {"success": False, "message": "重试配置更新失败"}

@app.delete("/api/retry/{index}")
async def delete_retry_config(index: int):
    success = config_mgr.delete_retry_config(index)
    if success:
        refresh_retry_configs()
        return {"success": True, "message": "重试配置已删除"}
    return {"success": False, "message": "重试配置删除失败"}

@app.post("/api/retry/{index}/toggle")
async def toggle_retry_config(index: int):
    enabled = config_mgr.toggle_retry_config(index)
    if enabled is not None:
        refresh_retry_configs()
        return {"success": True, "enabled": enabled, "message": f"重试配置已{'启用' if enabled else '禁用'}"}
    return {"success": False, "message": "切换失败"}

@app.post("/api/retry/{index}/move")
async def move_retry_config(index: int, direction: dict):
    success = config_mgr.move_retry_config(index, direction.get("direction"))
    if success:
        refresh_retry_configs()
        return {"success": True, "message": "重试配置已移动"}
    return {"success": False, "message": "重试配置移动失败"}

@app.post("/api/retry/{index}/duplicate")
async def duplicate_retry_config(index: int):
    success = config_mgr.duplicate_retry_config(index)
    if success:
        refresh_retry_configs()
        return {"success": True, "message": "重试配置已复制"}
    return {"success": False, "message": "重试配置复制失败"}

# ========== 模型转换配置管理端点 ==========
@app.get("/api/model-conversion")
async def get_model_conversions():
    return {"configs": config_mgr.get_model_conversions()}

@app.post("/api/model-conversion")
async def add_model_conversion(config: dict):
    success = config_mgr.add_model_conversion(config)
    if success:
        refresh_model_conversion_state()
        return {"success": True, "message": "模型转换配置已添加"}
    return {"success": False, "message": "模型转换配置添加失败"}

@app.put("/api/model-conversion/{index}")
async def update_model_conversion(index: int, config: dict):
    success = config_mgr.update_model_conversion(index, config)
    if success:
        refresh_model_conversion_state()
        return {"success": True, "message": "模型转换配置已更新"}
    return {"success": False, "message": "模型转换配置更新失败"}

@app.delete("/api/model-conversion/{index}")
async def delete_model_conversion(index: int):
    success = config_mgr.delete_model_conversion(index)
    if success:
        refresh_model_conversion_state()
        return {"success": True, "message": "模型转换配置已删除"}
    return {"success": False, "message": "模型转换配置删除失败"}

@app.post("/api/model-conversion/{index}/toggle")
async def toggle_model_conversion(index: int):
    enabled = config_mgr.toggle_model_conversion(index)
    if enabled is not None:
        refresh_model_conversion_state()
        return {"success": True, "enabled": enabled, "message": f"模型转换配置已{'启用' if enabled else '禁用'}"}
    return {"success": False, "message": "切换失败"}

@app.post("/api/model-conversion/{index}/move")
async def move_model_conversion(index: int, direction: dict):
    success = config_mgr.move_model_conversion(index, direction.get("direction"))
    if success:
        refresh_model_conversion_state()
        return {"success": True, "message": "模型转换配置已移动"}
    return {"success": False, "message": "模型转换配置移动失败"}

# ========== 错误处理策略管理端点 ==========
@app.get("/api/error-strategies")
async def get_error_strategies():
    """获取错误处理策略配置"""
    return {"strategies": config_mgr.get_error_handling_strategies()}

@app.put("/api/error-strategies")
async def update_error_strategies(strategies: dict):
    """更新错误处理策略配置"""
    success = config_mgr.update_error_handling_strategies(strategies)
    if success:
        return {"success": True, "message": "错误处理策略已更新"}
    return {"success": False, "message": "错误处理策略更新失败"}


@app.post("/api/model-conversion/{index}/duplicate")
async def duplicate_model_conversion(index: int):
    success = config_mgr.duplicate_model_conversion(index)
    if success:
        refresh_model_conversion_state()
        return {"success": True, "message": "模型转换配置已复制"}
    return {"success": False, "message": "模型转换配置复制失败"}

# ========== 超时设置管理端点 ==========
@app.get("/api/timeout")
async def get_timeout_settings():
    return {"settings": config_mgr.get_timeout_settings()}

@app.put("/api/timeout")
async def update_timeout_settings(settings: dict):
    success = config_mgr.update_timeout_settings(settings)
    if success:
        # 刷新全局HTTP客户端，使超时设置立即生效
        await refresh_timeout_client()
        return {"success": True, "message": "超时设置已更新"}
    return {"success": False, "message": "超时设置更新失败"}

# ========== 优化设置端点 ==========
@app.get("/api/optimization")
async def get_optimization_settings():
    return {"settings": config_mgr.get_optimization_settings()}

@app.put("/api/optimization")
async def update_optimization_settings(settings: dict):
    success = config_mgr.update_optimization_settings(settings)
    if success:
        return {"success": True, "message": "优化设置已更新"}
    return {"success": False, "message": "优化设置更新失败"}

# ========== 重置冷却端点 ==========
@app.post("/api/reset-api-cooldown")
async def reset_api_cooldown(data: dict = None):
    """重置API冷却状态"""
    global api_status
    now = datetime.now()
    
    if data and "index" in data:
        # 重置单个API的冷却
        index = data["index"]
        if 0 <= index < len(API_CONFIGS):
            if index in api_status and api_status[index]["cooldown_until"]:
                api_status[index] = {"status": "normal", "error_count": 0, "cooldown_until": None}
                api_name = API_CONFIGS[index]['name']
                print(f"[{now.strftime('%H:%M:%S')}] 手动重置API冷却: {api_name}")
                return {"success": True, "message": f"已重置 {api_name} 的冷却状态"}
            else:
                return {"success": False, "message": f"API {API_CONFIGS[index]['name']} 未处于冷却状态"}
        return {"success": False, "message": "无效的API索引"}
    else:
        # 重置所有API的冷却
        reset_count = 0
        reset_names = []
        for i in range(len(API_CONFIGS)):
            if i in api_status and api_status[i]["cooldown_until"]:
                api_status[i] = {"status": "normal", "error_count": 0, "cooldown_until": None}
                reset_names.append(API_CONFIGS[i]['name'])
                reset_count += 1
        
        if reset_count > 0:
            print(f"[{now.strftime('%H:%M:%S')}] 手动重置所有API冷却: {', '.join(reset_names)}")
            return {"success": True, "message": f"已重置 {reset_count} 个API的冷却状态"}
        else:
            return {"success": False, "message": "没有API处于冷却状态"}

@app.post("/api/reset-codex-cooldown")
async def reset_codex_cooldown(data: dict = None):
    """重置Codex冷却状态"""
    global codex_api_status
    now = datetime.now()
    
    if data and "index" in data:
        # 重置单个Codex的冷却
        index = data["index"]
        if 0 <= index < len(CODEX_CONFIGS):
            if index in codex_api_status and codex_api_status[index]["cooldown_until"]:
                codex_api_status[index] = {"status": "normal", "error_count": 0, "cooldown_until": None}
                codex_name = CODEX_CONFIGS[index]['name']
                print(f"[{now.strftime('%H:%M:%S')}] 手动重置Codex冷却: {codex_name}")
                return {"success": True, "message": f"已重置 {codex_name} 的冷却状态"}
            else:
                return {"success": False, "message": f"Codex {CODEX_CONFIGS[index]['name']} 未处于冷却状态"}
        return {"success": False, "message": "无效的Codex索引"}
    else:
        # 重置所有Codex的冷却
        reset_count = 0
        reset_names = []
        for i in range(len(CODEX_CONFIGS)):
            if i in codex_api_status and codex_api_status[i]["cooldown_until"]:
                codex_api_status[i] = {"status": "normal", "error_count": 0, "cooldown_until": None}
                reset_names.append(CODEX_CONFIGS[i]['name'])
                reset_count += 1
        
        if reset_count > 0:
            print(f"[{now.strftime('%H:%M:%S')}] 手动重置所有Codex冷却: {', '.join(reset_names)}")
            return {"success": True, "message": f"已重置 {reset_count} 个Codex的冷却状态"}
        else:
            return {"success": False, "message": "没有Codex处于冷却状态"}

# ========== 配置重新加载端点 ==========

@app.post("/api/reload")
async def reload_configs():
    """重新加载配置文件（用于手动修改配置后同步）"""
    previous_snapshot = {
        "api": copy.deepcopy(config_mgr.get_api_configs()),
        "codex": copy.deepcopy(config_mgr.get_codex_configs()),
        "openai": copy.deepcopy(config_mgr.get_openai_to_claude_configs()),
        "retry": copy.deepcopy(config_mgr.get_retry_configs()),
        "model": copy.deepcopy(config_mgr.get_model_conversions()),
    }

    success = config_mgr.reload_all_configs()
    if success:
        now = datetime.now()
        latest_snapshot = {
            "api": config_mgr.get_api_configs(),
            "codex": config_mgr.get_codex_configs(),
            "openai": config_mgr.get_openai_to_claude_configs(),
            "retry": config_mgr.get_retry_configs(),
            "model": config_mgr.get_model_conversions(),
        }

        changed_sections = {key: previous_snapshot[key] != latest_snapshot[key] for key in previous_snapshot}
        if not any(changed_sections.values()):
            # 不显示无变化的日志
            return {"success": True, "message": "配置未变化，无需重新加载"}

        if changed_sections["api"]:
            refresh_api_runtime_state(reset_backup_state=True)
        if changed_sections["codex"]:
            refresh_codex_runtime_state(reset_backup_state=True)
        if changed_sections["openai"]:
            refresh_openai_runtime_state()
        if changed_sections["retry"]:
            refresh_retry_configs()
        if changed_sections["model"]:
            refresh_model_conversion_state()

        section_labels = {
            "api": "API主配置",
            "codex": "Codex配置",
            "openai": "OpenAI转Claude",
            "retry": "超时重试配置",
            "model": "模型转换配置",
        }
        updated_sections = [label for key, label in section_labels.items() if changed_sections.get(key)]
        sections_text = "、".join(updated_sections)

        api_index_info = current_config_index if current_config_index is not None and current_config_index >= 0 else "-"
        codex_index_info = codex_current_config_index if 'codex_current_config_index' in globals() and codex_current_config_index is not None and codex_current_config_index >= 0 else "-"
        print(f"[{now.strftime('%H:%M:%S')}] 配置重新加载：更新项={sections_text}；主API索引={api_index_info}，Codex索引={codex_index_info}")

        return {"success": True, "message": f"已刷新：{sections_text}"}
    return {"success": False, "message": "配置重新加载失败"}


# ========== cache_control 数量限制函数 ==========
def limit_cache_control_blocks(request_data: Dict[str, Any], max_blocks: int = 4) -> Dict[str, Any]:
    """
    限制请求中 cache_control 块的数量，避免超过 Claude API 的限制

    Args:
        request_data: 请求数据
        max_blocks: 最大允许的 cache_control 块数量（默认 4）

    Returns:
        修复后的请求数据
    """
    try:
        import copy
        fixed_request = copy.deepcopy(request_data)
        cache_control_count = 0

        # 统计并限制 system 中的 cache_control
        system_items = fixed_request.get("system", [])
        if system_items:
            fixed_system = []
            for item in system_items:
                if isinstance(item, dict) and "cache_control" in item:
                    if cache_control_count < max_blocks:
                        # 保留 cache_control
                        fixed_system.append(item)
                        cache_control_count += 1
                    else:
                        # 移除 cache_control
                        item_copy = item.copy()
                        del item_copy["cache_control"]
                        fixed_system.append(item_copy)
                        print(f"[cache_control限制] 移除system中第{cache_control_count + 1}个cache_control", file=sys.stderr)
                else:
                    fixed_system.append(item)
            fixed_request["system"] = fixed_system

        # 统计并限制 messages 中的 cache_control
        messages = fixed_request.get("messages", [])
        if messages:
            fixed_messages = []
            for msg in messages:
                if isinstance(msg, dict):
                    msg_copy = msg.copy()
                    content = msg_copy.get("content", [])

                    # 处理列表格式的 content
                    if isinstance(content, list):
                        fixed_content = []
                        for item in content:
                            if isinstance(item, dict) and "cache_control" in item:
                                if cache_control_count < max_blocks:
                                    fixed_content.append(item)
                                    cache_control_count += 1
                                else:
                                    item_copy = item.copy()
                                    del item_copy["cache_control"]
                                    fixed_content.append(item_copy)
                                    print(f"[cache_control限制] 移除messages中第{cache_control_count + 1}个cache_control", file=sys.stderr)
                            else:
                                fixed_content.append(item)
                        msg_copy["content"] = fixed_content

                    fixed_messages.append(msg_copy)
                else:
                    fixed_messages.append(msg)
            fixed_request["messages"] = fixed_messages

        if cache_control_count > max_blocks:
            print(f"[cache_control限制] 检测到{cache_control_count}个cache_control块，已限制为{max_blocks}个", file=sys.stderr)

        return fixed_request
    except Exception as e:
        print(f"[cache_control限制] 处理失败: {e}", file=sys.stderr)
        return request_data  # 出错时返回原始数据


# 在初始化客户端时，我们不设置 base_url，以便在请求时构建完整的 URL

# 预定义的超时配置对象
timeout = TimeoutConfig.get_streaming_timeout()
non_streaming_timeout = TimeoutConfig.get_non_streaming_timeout()

# 禁用连接复用，避免异常连接影响后续请求
limits = httpx.Limits(max_keepalive_connections=0, max_connections=100)
client = httpx.AsyncClient(timeout=timeout, limits=limits)

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def reverse_proxy(request: Request, path: str):
    """
    一个高保真异步反向代理，支持OpenAI到Claude格式的自动转换。
    核心特性是"绝对透传"响应头，以应对具有非标准头依赖的客户端。
    """
    # 跳过非API路径的请求（浏览器自动请求的资源）
    skip_paths = ['favicon.ico', 'robots.txt', 'sitemap.xml', 'apple-touch-icon', '.well-known']
    if any(skip_path in path for skip_path in skip_paths):
        return JSONResponse(content={"error": "Not Found"}, status_code=404)
    
    # 生成请求ID用于日志跟踪
    request_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    
    # 声明全局变量（Codex自适应超时）
    global codex_timeout_extra_seconds, codex_success_count

    # 1. 读取原始请求体
    body = await request.body()

    # 路径归一化与Codex直连识别
    normalized_path = path.lstrip('/')
    is_codex_request = normalized_path == CODEX_PATH_PREFIX or normalized_path.startswith(f"{CODEX_PATH_PREFIX}/")
    base_url_override = get_current_codex_config()["base_url"] if is_codex_request else None

    # 简化日志记录：仅记录基本信息和用户模型
    if ENABLE_FULL_LOG and full_logger:
        try:
            full_logger.info("="*40)
            full_logger.info(f"请求 - ID: {request_id}")
            # 记录用户输入的模型信息和问题内容
            if body and request.method == "POST":
                try:
                    request_data = json.loads(body.decode('utf-8'))
                    user_model = request_data.get("model", "unknown")
                    full_logger.info(f"用户使用模型: {user_model}")
                    
                    # 记录用户发出的问题内容
                    messages = request_data.get("messages", [])
                    if messages:
                        # 找到最后一条用户消息
                        for message in reversed(messages):
                            if message.get("role") == "user":
                                content = message.get("content", "")
                                if isinstance(content, str):
                                    full_logger.info(f"用户问题: {content}")
                                elif isinstance(content, list):
                                    # 处理多模态内容
                                    text_parts = []
                                    for part in content:
                                        if isinstance(part, dict) and part.get("type") == "text":
                                            text_parts.append(part.get("text", ""))
                                    if text_parts:
                                        full_logger.info(f"用户问题: {' '.join(text_parts)}")
                                break
                except:
                    pass
        except Exception as log_error:
            print(f"记录请求日志时出错: {log_error}", file=sys.stderr)
    
    # 2. 验证用户Key并替换为真正的API Key
    user_auth_header = request.headers.get('authorization')
    is_valid, real_auth_header, error_msg = validate_and_replace_user_key(user_auth_header)
    
    if not is_valid:
        print(f"Key验证失败: {error_msg}", file=sys.stderr)
        error_response = {
            "error": {
                "message": error_msg,
                "type": "authentication_error", 
                "code": "invalid_api_key"
            }
        }
        return JSONResponse(
            content=error_response,
            status_code=401,
            headers={"content-type": "application/json"}
        )

    # 提前检测是否为OpenAI客户端（用于正确显示API信息）
    is_openai_client_early = (path == "v1/chat/completions" or path.endswith("/v1/chat/completions")) and not is_codex_request

    print(f"\n" + "=" * 50)
    print(f"Key验证成功，用户Key: {user_auth_header[7:] if user_auth_header else 'None'}")
    if is_codex_request:
        print(f"{get_current_codex_info()}")
    elif is_openai_client_early:
        print(f"{get_openai_to_claude_info()}")
    else:
        print(f"{get_current_api_info()}")
    print()

    if is_codex_request:
        current_codex_config = get_current_codex_config()
        real_auth_header = f"Bearer {current_codex_config['key']}"

    # 3. 检测是否为OpenAI客户端（通过路径判断）
    is_openai_client = is_openai_client_early

    # 3. 处理OpenAI格式转换
    original_request_data = None
    is_openai_format = False
    converted_body = body
    user_wants_stream = True  # 记录用户原始的stream设置
    original_model = None  # 用户输入的原始模型
    converted_model = None  # 转换后的模型
    model_conversion_info = ""  # 模型转换信息
    
    if request.method == "POST" and body:
        try:
            original_request_data = json.loads(body.decode('utf-8'))
            
            # 统一的模型转换 - 使用配置驱动（Codex和Claude都适用）
            user_original_model = original_request_data.get("model", "unknown")
            
            # 调试日志：显示当前请求的模型
            if is_codex_request:
                print(f"[模型转换调试] Codex请求，原始模型: {user_original_model}", file=sys.stderr)
            
            # 遍历模型转换配置，查找匹配的规则
            for conversion in MODEL_CONVERSIONS:
                if user_original_model == conversion.get("source_model"):
                    target_model = conversion.get("target_model")
                    conversion_name = conversion.get("name", "未命名转换")
                    conversion_type = conversion.get("conversion_type", "simple_rename")  # 默认简单替换
                    
                    # 根据配置的转换类型选择转换逻辑
                    if conversion_type == "full_format":
                        # Claude 3.5 -> Claude 4 完整格式转换
                        converted_request = {
                            "model": target_model,
                            "max_tokens": original_request_data.get("max_tokens", 8192),
                            "temperature": original_request_data.get("temperature", 1),
                            "stream": original_request_data.get("stream", True)
                        }
                        
                        # 转换messages格式：字符串 -> 对象数组
                        original_messages = original_request_data.get("messages", [])
                        converted_messages = []
                        for msg in original_messages:
                            converted_msg = {"role": msg.get("role", "user")}
                            content = msg.get("content", "")
                            if isinstance(content, str):
                                # 3.5格式：字符串 -> 4格式：对象数组
                                converted_msg["content"] = [{"type": "text", "text": content}]
                            else:
                                # 已经是对象格式，保持不变
                                converted_msg["content"] = content
                            converted_messages.append(converted_msg)
                        converted_request["messages"] = converted_messages
                        
                        # 转换system格式：添加Claude 4必需的关键提示词和cache_control
                        converted_system = []
                        
                        # 完整格式转换：添加Claude Code关键提示词
                        converted_system.append({
                            "type": "text",
                            "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                            "cache_control": {"type": "ephemeral"}
                        })
                        
                        # 处理原始system项
                        original_system = original_request_data.get("system", [])
                        for sys_item in original_system:
                            if isinstance(sys_item, dict) and "type" in sys_item:
                                # 添加cache_control到现有system项
                                converted_sys_item = sys_item.copy()
                                converted_sys_item["cache_control"] = {"type": "ephemeral"}
                                converted_system.append(converted_sys_item)
                            else:
                                # 处理其他格式
                                converted_system.append(sys_item)
                        
                        converted_request["system"] = converted_system
                        
                        # 保留其他可能的参数
                        for key in ["metadata", "top_p", "top_k"]:
                            if key in original_request_data:
                                converted_request[key] = original_request_data[key]
                        
                        converted_body = json.dumps(converted_request, ensure_ascii=False).encode('utf-8')
                        model_conversion_info = f"{user_original_model} → {target_model}"
                    else:
                        # 简单模型名替换
                        import copy
                        converted_request = copy.deepcopy(original_request_data)
                        converted_request["model"] = target_model
                        converted_body = json.dumps(converted_request, ensure_ascii=False).encode('utf-8')
                        model_conversion_info = f"{user_original_model} → {target_model}"
                        
                        # 调试日志：确认转换执行
                        if is_codex_request:
                            print(f"[模型转换调试] ✅ 转换成功: {user_original_model} → {target_model}", file=sys.stderr)
                    
                    # 找到匹配规则后，退出循环
                    break
            openai_config_for_request: Dict[str, Any] = {}

            if is_openai_client:
                openai_config_for_request = get_primary_openai_to_claude_config()
                if not openai_config_for_request or not openai_config_for_request.get("base_url") or not openai_config_for_request.get("key"):
                    error_response = {
                        "error": {
                            "message": "OpenAI转Claude配置缺失或未启用，请在管理后台配置有效的Key",
                            "type": "configuration_error",
                            "code": "invalid_configuration"
                        }
                    }
                    return JSONResponse(
                        content=error_response,
                        status_code=500,
                        headers={"content-type": "application/json"}
                    )

                is_openai_format = True
                # OpenAI转Claude时，强制使用专用配置
                base_url_override = openai_config_for_request.get("base_url", base_url_override)
                # 记录用户原始的stream设置和模型
                user_wants_stream = original_request_data.get("stream", False)
                original_model = user_original_model  # 使用保存的原始模型名
                
                try:
                    # 转换OpenAI请求为Claude格式，获取转换结果和对应的请求头
                    _, converted_request, conversion_headers = detect_and_convert_request(original_request_data)
                    
                    # 获取转换后的模型名
                    converted_model = converted_request.get("model", original_model)
                    
                    # 生成模型转换信息（只有在未设置时才生成）
                    if not model_conversion_info:
                        if original_model != converted_model:
                            # 检测是否是思考模式
                            is_thinking_mode = "-thinking" in original_model
                            thinking_suffix = " (思考模式)" if is_thinking_mode else ""
                            model_conversion_info = f"{original_model} → {converted_model}{thinking_suffix}"
                        else:
                            model_conversion_info = f"{original_model}"
                    
                    # 验证转换后的请求是否有效
                    if not converted_request.get("model"):
                        converted_request["model"] = original_model
                    if not converted_request.get("messages"):
                        raise ValueError("转换后的请求缺少messages字段")
                    if "max_tokens" not in converted_request:
                        converted_request["max_tokens"] = 32000  # OpenAI方式默认32000
                    
                    # 移除thinking功能，使用exact_test.py验证成功的简洁格式
                    # exact_test.py中的成功请求没有使用thinking功能
                    
                    converted_body = json.dumps(converted_request, ensure_ascii=False).encode('utf-8')
                    
                    # OpenAI格式输入请求日志已删除
                    
                    # 对于OpenAI格式请求，转换路径为 v1/messages
                    path = "v1/messages"
                    
                except Exception as convert_error:
                    import traceback
                    error_msg = f"OpenAI请求转换失败: {convert_error}"
                    print(error_msg, file=sys.stderr)
                    print(f"转换错误详情: {traceback.format_exc()}", file=sys.stderr)
                    
                    # 转换失败时返回错误响应
                    error_response = {
                        "error": {
                            "message": f"Request conversion failed: {str(convert_error)}",
                            "type": "conversion_error",
                            "code": "invalid_request_error"
                        }
                    }
                    return JSONResponse(
                        content=error_response,
                        status_code=400,
                        headers={"content-type": "application/json"}
                    )
                
                # 删除模型转换日志记录
            else:
                # 非OpenAI路径（直连Claude API）
                # 如果发生了模型转换，设置相应信息用于日志记录
                if model_conversion_info:
                    # 模型转换信息已在上面设置，这里仅用于日志
                    pass
                    
        except json.JSONDecodeError:
            # 不是JSON请求，保持原样
            pass
        except Exception as e:
            import traceback
            print(f"转换请求时出错: {e}")
            print(f"错误详情: {traceback.format_exc()}")
            # 删除原始请求数据记录
    
    # 5. 复制请求头，排除 host 头和 authorization 头（将使用验证后的真正API key）
    headers = {key: value for key, value in request.headers.items() 
               if key.lower() not in ['host', 'authorization']}
    
    # 添加验证后的真正API key
    headers['authorization'] = real_auth_header
    
    # 对于OpenAI格式请求，使用从转换函数返回的头信息配置
    if is_openai_format:
        successful_headers = conversion_headers if 'conversion_headers' in locals() else get_exact_test_headers()
        
        # OpenAI转Claude时，强制使用专用配置的key
        if openai_config_for_request and openai_config_for_request.get("key"):
            successful_headers['authorization'] = f"Bearer {openai_config_for_request['key']}"
        
        # 更新为成功的头信息
        headers.update(successful_headers)
        
        # 处理外部请求路径：提取核心API路径，去掉所有前缀
        # 不管是 api/v1/messages 还是 ao/api2/v1/messages，都提取出 v1/messages
        import re
        # 匹配最后的 v1/... 部分
        path_match = re.search(r'(v1/(?:messages|chat/completions).*?)(?:\?|$)', path)
        if path_match:
            clean_path = path_match.group(1)
        else:
            # 如果没有匹配到标准路径，去掉开头的任何路径段直到遇到v1
            parts = path.split('/')
            v1_index = -1
            for i, part in enumerate(parts):
                if part == 'v1':
                    v1_index = i
                    break
            if v1_index >= 0:
                clean_path = '/'.join(parts[v1_index:])
            else:
                clean_path = path  # 保持原样
        
        # 添加query参数 - exact_test.py中使用了beta=true
        upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, base_url_override)
    else:
        # 非OpenAI请求保持原有逻辑
        # 确保必要的默认头信息存在
        default_headers = {
            'content-type': 'application/json',
            'accept': 'application/json, text/event-stream',
            'user-agent': headers.get('user-agent', 'Claude-Proxy/1.0')
        }
        
        # 添加缺失的默认头信息
        for key, value in default_headers.items():
            if key.lower() not in {h.lower() for h in headers.keys()}:
                headers[key] = value
        
        # 处理外部请求路径：提取核心API路径，去掉所有前缀
        # 不管是 api/v1/messages 还是 ao/api2/v1/messages，都提取出 v1/messages
        import re
        # 匹配最后的 v1/... 部分  
        path_match = re.search(r'(v1/(?:messages|chat/completions).*?)(?:\?|$)', path)
        if path_match:
            clean_path = path_match.group(1)
        else:
            # 如果没有匹配到标准路径，去掉开头的任何路径段直到遇到v1
            parts = path.split('/')
            v1_index = -1
            for i, part in enumerate(parts):
                if part == 'v1':
                    v1_index = i
                    break
            if v1_index >= 0:
                clean_path = '/'.join(parts[v1_index:])
            else:
                # 对于 Codex 请求，去掉 openai/ 前缀（因为 base_url 已包含）
                if is_codex_request and normalized_path.startswith(f"{CODEX_PATH_PREFIX}/"):
                    clean_path = normalized_path[len(CODEX_PATH_PREFIX)+1:]  # 去掉 "openai/"
                else:
                    clean_path = path  # 保持原样
        
        # 完整地重建上游 URL，包括查询参数
        upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, base_url_override)
    
    # 如果转换了请求体，需要更新Content-Length
    if converted_body != body:
        headers['content-length'] = str(len(converted_body))
    
    # 打印用户请求信息（精简版）
    print(f"=== {datetime.now().strftime('%H:%M:%S')} {request.method} {path} ===")
    if is_openai_format and model_conversion_info:
        print(f"OpenAI格式转换: {model_conversion_info}")
    elif is_openai_format:
        print(f"OpenAI格式 → Claude格式 (转换完成)")
    elif is_codex_request:
        if model_conversion_info:
            print(f"Codex格式 (直接透传): {model_conversion_info}")
        else:
            print("Codex格式 (直接透传)")
        print(f"Codex目标URL: {upstream_url}")
    elif model_conversion_info and not is_openai_format:
        print(f"Claude格式 (直接透传): {model_conversion_info}")
    else:
        print(f"Claude格式 (直接透传)")
    
    # 删除详细的上游请求记录
    pass

    # 5. 定义转换标志
    should_convert_to_openai = is_openai_client  # 只有OpenAI客户端才转换响应格式
    
    # 添加重试机制 - 使用独立client避免并发冲突
    # Claude请求的重试次数从配置读取（默认4次）
    # Codex请求的重试次数取决于READ_TIMEOUT_RETRY_CONFIGS长度
    if is_codex_request:
        max_retries = len(READ_TIMEOUT_RETRY_CONFIGS) if READ_TIMEOUT_RETRY_CONFIGS else 2
    else:
        # Claude请求：从配置读取最大重试次数
        max_retries = TimeoutConfig.get_max_retries()
    
    # 注意：临时性错误（400, 404, 429, 500, 502, 503, 520-524）使用策略重试处理
    # 持续性错误（401, 403）使用智能API切换处理（达到切换阈值后）
    last_error = None
    retry_errors = []
    
    # Claude请求的错误追踪（用于在重试循环结束后统一记录错误）
    last_error_status_code = None  # 最后的HTTP状态码
    last_error_strategy = None  # 最后的错误处理策略
    should_record_error_after_retry = False  # 是否在重试结束后记录错误
    
    for retry_attempt in range(max_retries):
        # 为每次重试创建独立的client实例，避免连接复用问题
        # 包括第一次也使用新client，确保不复用可能有坏连接的全局client
        # 根据是否为非流式请求选择合适的超时配置
        if is_codex_request:
            # Codex请求：连接30秒超时（asyncio.wait_for控制）+ 流式总超时（手动计时控制）
            # 禁用httpx的read超时，完全由流式总超时控制
            codex_timeout = httpx.Timeout(
                connect=TimeoutConfig.get_connect_timeout(),
                read=None,  # ✅ 禁用read超时，由流式总超时控制
                write=TimeoutConfig.get_write_timeout(),
                pool=TimeoutConfig.get_pool_timeout()
            )
            retry_client = httpx.AsyncClient(timeout=codex_timeout, limits=limits)
            # 显示 Codex 超时信息
            codex_base_timeout = TimeoutConfig.get_codex_base_timeout()
            with codex_timeout_lock:
                current_extra_seconds = codex_timeout_extra_seconds
            print(f"[Codex超时配置] 连接超时: {TimeoutConfig.get_codex_connect_timeout()}秒 | 流式总超时: {codex_base_timeout + current_extra_seconds}秒", file=sys.stderr)
            if current_extra_seconds > 0:
                print(f"[Codex自适应超时] 流式总超时详情: 基础{codex_base_timeout}秒 + 额外{current_extra_seconds}秒", file=sys.stderr)
        elif should_convert_to_openai and not user_wants_stream:
            # 非流式请求使用60秒超时
            retry_client = httpx.AsyncClient(timeout=non_streaming_timeout, limits=limits)
            # 显示 Claude 非流式超时信息
            print(f"[Claude超时配置] 连接超时: {TimeoutConfig.get_connect_timeout()}秒 | 读取超时: {TimeoutConfig.get_non_streaming_read_timeout()}秒", file=sys.stderr)
        else:
            # 流式请求或非OpenAI请求使用标准超时
            retry_client = httpx.AsyncClient(timeout=timeout, limits=limits)
            # 显示 Claude 流式超时信息
            print(f"[Claude超时配置] 连接超时: {TimeoutConfig.get_connect_timeout()}秒 | 流式读取超时: {TimeoutConfig.get_streaming_read_timeout()}秒", file=sys.stderr)
        
        try:
            # 记录发API前的原数据（仅在第一次尝试时记录）
            if retry_attempt == 0:
                log_original_data(request_id, body, headers, request.method, path, is_codex_request)
                
                # 如果发生了模型转换，记录转换后数据用于对比分析
                if converted_body != body and model_conversion_info and original_data_logger:
                    original_data_logger.info("="*40)
                    original_data_logger.info(f"模型转换对比 - 请求ID: {request_id}")
                    original_data_logger.info(f"转换信息: {model_conversion_info}")
                    # 直接记录转换后数据，复用现有处理逻辑
                    log_original_data(f"{request_id}_转换后", converted_body, headers, request.method, path, is_codex_request)
            
            # 获取当前API配置
            current_config = get_current_codex_config() if is_codex_request else get_current_config()
            
            # 根据配置决定是否修改重试请求头
            retry_headers = headers.copy()
            if TimeoutConfig.get_modify_retry_headers():
                # 强制关闭连接复用，让每次重试都像新请求一样使用全新连接
                retry_headers['connection'] = 'close'
                # 添加唯一标识和完整的防缓存头部，确保API不使用缓存
                import random
                import time
                retry_rand = random.randint(1000,9999)
                retry_timestamp = int(time.time() * 1000)
                retry_headers['x-request-id'] = f"{request_id}-retry{retry_attempt}-{retry_rand}"
                retry_headers['cache-control'] = 'no-cache, no-store, must-revalidate'
                retry_headers['pragma'] = 'no-cache'
                retry_headers['expires'] = '0'
                retry_headers['x-cache-bypass'] = f'{retry_timestamp}-{retry_rand}'
                retry_headers['x-retry-count'] = str(retry_attempt + 1)

            # 应用 cache_control 数量限制（实际限制是3个，而不是文档说的4个）
            # 检查是否启用了cache_control限制功能
            optimization_settings = config_mgr.get_optimization_settings()
            if optimization_settings.get("enable_cache_control_limit", True):
                try:
                    request_data_to_limit = json.loads(converted_body.decode('utf-8'))

                    # 先统计cache_control块数量
                    cache_count = 0
                    for item in request_data_to_limit.get("system", []):
                        if isinstance(item, dict) and "cache_control" in item:
                            cache_count += 1
                    for msg in request_data_to_limit.get("messages", []):
                        if isinstance(msg, dict):
                            content = msg.get("content", [])
                            if isinstance(content, list):
                                for c_item in content:
                                    if isinstance(c_item, dict) and "cache_control" in c_item:
                                        cache_count += 1

                    # 只有超过3个时才打印诊断信息并限制
                    if cache_count > 3:
                        print(f"🔍 [cache_control诊断][{request_id}] 检测到 {cache_count} 个cache_control块", file=sys.stderr)
                        limited_request_data = limit_cache_control_blocks(request_data_to_limit, max_blocks=3)
                        converted_body = json.dumps(limited_request_data, ensure_ascii=False).encode('utf-8')
                        # 修改了请求体后，必须删除旧的 Content-Length 头，让 httpx 重新计算
                        # HTTP 头名称不区分大小写，需要逐一检查
                        headers_to_remove = [k for k in retry_headers.keys() if k.lower() == 'content-length']
                        for h in headers_to_remove:
                            del retry_headers[h]
                except Exception as e:
                    print(f"[cache_control限制] 应用失败，使用原始请求: {e}", file=sys.stderr)

            # 6. 以流式模式向上游发送请求（使用转换后的请求体和重试专用headers）
            upstream_req = retry_client.build_request(
                method=request.method,
                url=upstream_url,
                headers=retry_headers,  # 使用重试专用的headers副本
                content=converted_body  # 使用转换后的请求体
            )
            
            # Codex请求使用30秒连接超时（只针对连接阶段，不影响后续流式读取）
            if is_codex_request:
                import asyncio
                try:
                    upstream_resp = await asyncio.wait_for(
                        retry_client.send(upstream_req, stream=True),
                        timeout=TimeoutConfig.get_codex_connect_timeout()
                    )
                except asyncio.TimeoutError:
                    timeout_msg = f"[Codex连接超时][{request_id}] {TimeoutConfig.get_codex_connect_timeout()}秒内未收到响应，准备重试"
                    retry_errors.append(timeout_msg)
                    print(timeout_msg, file=sys.stderr)  # ← 立即打印超时信息
                    # 记录Codex连接超时错误
                    msg = record_codex_error(codex_current_config_index, 503, silent=True)
                    if msg:
                        retry_errors.append(msg)
                        print(msg, file=sys.stderr)  # ← 立即打印错误详情
                    await retry_client.aclose()
                    # 转换为httpx.ReadTimeout以复用现有重试逻辑
                    raise httpx.ReadTimeout("Codex connection timeout: 30 seconds")
            else:
                upstream_resp = await retry_client.send(upstream_req, stream=True)
            
            # 检查状态码是否需要策略重试（临时性错误，快速恢复）
            status_code = upstream_resp.status_code
            strategy_retry_status_codes = TimeoutConfig.get_strategy_retry_status_codes()
            
            # 临时性错误：使用策略重试（快速尝试其他API）
            if (not is_codex_request) and status_code in strategy_retry_status_codes:
                print(f"[策略重试触发][{request_id}] 检测到临时性错误{status_code}，将使用超时重试策略", file=sys.stderr)
                # 保存状态码，后续在异常处理块外使用策略重试
                # 这里先关闭响应，制造一个"需要策略重试"的状态
                await upstream_resp.aclose()
                await retry_client.aclose()
                # 抛出特殊标记，后续捕获并使用策略重试
                raise httpx.HTTPStatusError(
                    f"Status {status_code} - Strategy Retry Needed",
                    request=upstream_req,
                    response=upstream_resp
                )
            
            # 临时性错误处理：内部重试，不返回给用户（Codex和Claude都适用）
            # 从配置中读取哪些状态码需要触发API切换
            strategies = config_mgr.get_error_handling_strategies()
            http_codes = strategies.get("http_status_codes", {})
            switch_api_codes = [int(code) for code, strategy in http_codes.items()
                              if strategy == "switch_api" and code != "default"]
            no_retry_codes = [int(code) for code, strategy in http_codes.items()
                            if strategy == "normal_retry" and code != "default"]
            
            # no_retry策略：记录错误，延时后跳出重试循环（Codex和Claude都适用）
            if status_code in no_retry_codes:
                if is_codex_request:
                    # Codex请求：记录错误
                    print(f"[不重试策略][{request_id}] 检测到错误{status_code}，延时后返回错误给用户", file=sys.stderr)
                    msg = record_codex_error(codex_current_config_index, status_code, silent=True)
                    if msg:
                        print(msg, file=sys.stderr)
                    # 添加延时
                    import asyncio
                    delay = 2
                    print(f"[不重试策略][{request_id}] 等待 {delay} 秒后继续...", file=sys.stderr)
                    await asyncio.sleep(delay)
                    # 跳出重试循环，让后续的正常流程处理响应（保留usage信息）
                    break
                else:
                    # Claude请求：记录错误
                    print(f"[不重试策略][{request_id}] 检测到错误{status_code}，延时后返回错误给用户", file=sys.stderr)
                    msg = record_api_error(current_config_index, status_code, silent=True)
                    if msg:
                        print(msg, file=sys.stderr)
                    # 添加延时
                    import asyncio
                    delay = 2
                    print(f"[不重试策略][{request_id}] 等待 {delay} 秒后继续...", file=sys.stderr)
                    await asyncio.sleep(delay)
                    # 跳出重试循环，让后续的正常流程处理响应（保留usage信息）
                    break
            
            if status_code in switch_api_codes:
                # 根据请求类型选择不同的错误提示
                request_type = "Codex" if is_codex_request else "Claude"
                error_msg = f"[{request_type}错误重试 {retry_attempt + 1}/{max_retries}][{request_id}] 检测到错误{status_code}，内部重试"
                retry_errors.append(error_msg)
                print(error_msg, file=sys.stderr)  # ← 立即打印错误信息
                
                # 初始化切换标志
                switch_success = False
                
                # 记录错误（根据请求类型）
                if is_codex_request:
                    # Codex请求：每次都记录错误（保持原有逻辑）
                    msg = record_codex_error(codex_current_config_index, status_code, silent=True)
                    if msg:
                        retry_errors.append(msg)
                        print(msg, file=sys.stderr)  # ← 立即打印错误详情
                # Claude请求：不在这里记录错误，改为在重试循环结束后统一记录
                # 更新错误追踪信息
                else:
                    last_error_status_code = status_code
                    last_error_strategy = "switch_api"
                    should_record_error_after_retry = True
                
                # 尝试切换API（如果错误次数>=阈值，根据请求类型）
                if is_codex_request:
                    current_codex_api_index = codex_current_config_index
                    switch_success, new_codex_api_index = smart_codex_switch_api(current_codex_api_index, status_code)
                    
                    if switch_success:
                        switch_msg = f"[Codex错误重试][{request_id}] 已切换到 {CODEX_CONFIGS[new_codex_api_index]['name']}"
                        retry_errors.append(switch_msg)
                        print(switch_msg, file=sys.stderr)  # ← 立即打印切换信息
                else:
                    # Claude请求的API切换
                    current_api_index = current_config_index
                    switch_success, new_api_index = smart_switch_api(current_api_index, status_code)
                    
                    if switch_success:
                        switch_msg = f"[Claude错误重试][{request_id}] 已切换到 {API_CONFIGS[new_api_index]['name']}"
                        retry_errors.append(switch_msg)
                        print(switch_msg, file=sys.stderr)  # ← 立即打印切换信息
                
                # 关闭当前响应和客户端
                await upstream_resp.aclose()
                await retry_client.aclose()
                
                # 根据请求类型重新构建配置和URL
                if is_codex_request:
                    # 获取当前Codex配置（可能已切换）
                    current_codex_config = get_current_codex_config()
                    
                    # 显示切换后的完整API信息（格式和请求开始时一致）
                    if switch_success:
                        print(f"\n{get_current_codex_info()}")
                    
                    # 重新构建URL和认证信息
                    upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, current_codex_config["base_url"])
                    headers['authorization'] = f'Bearer {current_codex_config["key"]}'
                else:
                    # Claude请求的配置重建
                    if switch_success:
                        # 重新验证用户Key，获取新的真实Auth
                        is_valid, real_auth_header, error_msg = validate_and_replace_user_key(user_auth_header)
                        if is_valid:
                            headers['authorization'] = real_auth_header
                            # 重新构建URL（base_url可能已变化）
                            upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, base_url_override)
                            print(f"\n{get_current_api_info()}")  # 显示切换后的API信息
                
                # 继续下一次重试
                continue

            # 检查响应状态码，决定是否继续重试
            if upstream_resp.status_code < 400:
                # 请求成功，跳出重试循环
                break
            else:
                # 错误响应：只有最后一次重试才跳出，否则继续重试
                if retry_attempt < max_retries - 1:
                    # 还有重试机会，继续重试
                    error_msg = f"[重试 {retry_attempt + 1}/{max_retries}][{request_id}] 检测到错误{upstream_resp.status_code}，继续重试"
                    retry_errors.append(error_msg)
                    print(error_msg, file=sys.stderr)
                    await upstream_resp.aclose()
                    await retry_client.aclose()
                    continue
                else:
                    # 最后一次重试，跳出循环返回错误
                    break
            
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            last_error = e
            error_type_name = "状态码错误" if isinstance(e, httpx.HTTPStatusError) else "连接错误"
            general_error_msg = f"[重试 {retry_attempt + 1}/{max_retries}][{request_id}] {error_type_name}: {e}"
            retry_errors.append(general_error_msg)
            print(general_error_msg, file=sys.stderr)  # ← 立即打印通用错误
            
            # 关闭当前的client实例，避免状态异常影响后续重试
            await retry_client.aclose()
            
            # 特殊处理ReadError：根据配置决定处理策略
            if isinstance(e, httpx.ReadError):
                read_error_strategy = TimeoutConfig.get_network_error_strategy("ReadError")
                
                if is_codex_request:
                    # Codex请求的ReadError：每次都记录错误（保持原有逻辑）
                    retry_errors.append(f"[SSL读取错误][{request_id}] Codex检测到SSL读取错误或连接中断")
                    msg = record_codex_error(codex_current_config_index, 503, silent=True)
                    if msg:
                        retry_errors.append(msg)
                # Claude请求的ReadError：不在这里记录错误，改为在重试循环结束后统一记录
                
                # 为normal_retry策略设置错误记录标志
                if (not is_codex_request) and read_error_strategy == "normal_retry":
                    last_error_status_code = 503
                    last_error_strategy = "normal_retry"
                    should_record_error_after_retry = True
                
                if (not is_codex_request) and read_error_strategy == "switch_api":
                    # 配置为switch_api策略：强制切换API
                    read_error_msg = f"[SSL读取错误-切换API][{request_id}] 检测到SSL读取错误或连接中断，强制切换API"
                    retry_errors.append(read_error_msg)
                    print(read_error_msg, file=sys.stderr)  # ← 立即打印ReadError检测
                    
                    # ReadError视为严重连接错误，直接记录错误并尝试切换API
                    current_api_index = current_config_index
                    switch_success, new_api_index = smart_switch_api(current_api_index, 503)  # 使用503错误码触发切换
                    if switch_success:
                        read_switch_msg = f"[SSL读取错误-切换API成功][{request_id}] API切换成功，使用新API重试"
                        retry_errors.append(read_switch_msg)
                        print(read_switch_msg, file=sys.stderr)  # ← 立即打印ReadError切换成功
                        # 重新构建请求头和URL，使用新API
                        is_valid, real_auth_header, error_msg = validate_and_replace_user_key(user_auth_header)
                        if is_valid:
                            headers['authorization'] = real_auth_header
                            new_upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, base_url_override)
                            
                            # 根据配置决定是否修改重试请求头
                            read_error_retry_headers = headers.copy()
                            if TimeoutConfig.get_modify_retry_headers():
                                read_error_retry_headers['connection'] = 'close'
                                import random
                                import time
                                read_error_rand = random.randint(1000,9999)
                                read_error_timestamp = int(time.time() * 1000)
                                read_error_retry_headers['x-request-id'] = f"{request_id}-readerror-{retry_attempt + 1}-{read_error_rand}"
                                read_error_retry_headers['cache-control'] = 'no-cache, no-store, must-revalidate'
                                read_error_retry_headers['pragma'] = 'no-cache'
                                read_error_retry_headers['expires'] = '0'
                                read_error_retry_headers['x-cache-bypass'] = f'{read_error_timestamp}-{read_error_rand}'
                                read_error_retry_headers['x-retry-count'] = str(retry_attempt + 1)
                        
                        try:
                            # 使用新的重试客户端
                            read_error_retry_client = httpx.AsyncClient(timeout=timeout, limits=limits)
                            read_error_upstream_req = read_error_retry_client.build_request(
                                method=request.method,
                                url=new_upstream_url,
                                headers=read_error_retry_headers,
                                content=converted_body
                            )
                            upstream_resp = await read_error_retry_client.send(read_error_upstream_req, stream=True)
                            retry_client = read_error_retry_client  # 更新重试客户端引用
                            read_status_msg = f"[SSL读取错误-切换API][{request_id}] 新API响应状态码: {upstream_resp.status_code}"
                            retry_errors.append(read_status_msg)
                            print(read_status_msg, file=sys.stderr)  # ← 立即打印新API状态码
                            # 只有成功响应（< 400）才跳出重试循环，错误响应继续重试
                            if upstream_resp.status_code < 400:
                                retry_errors.clear()
                                break
                            else:
                                read_error_status_msg = f"[SSL读取错误-切换API][{request_id}] 新API仍返回错误{upstream_resp.status_code}，继续重试"
                                retry_errors.append(read_error_status_msg)
                                print(read_error_status_msg, file=sys.stderr)  # ← 立即打印新API错误
                                await upstream_resp.aclose()
                                await read_error_retry_client.aclose()
                                # 不break，继续下一次重试
                        except Exception as read_error_retry_exception:
                            retry_errors.append(f"[SSL读取错误-切换API失败][{request_id}] 新API重试也失败: {read_error_retry_exception}")
                            await read_error_retry_client.aclose()
                    else:
                        read_fail_msg = f"[SSL读取错误-切换API失败][{request_id}] API切换失败，继续正常重试流程"
                        retry_errors.append(read_fail_msg)
                        print(read_fail_msg, file=sys.stderr)  # ← 立即打印ReadError切换失败
            
            # 特殊处理ConnectError：根据配置决定处理策略
            if isinstance(e, httpx.ConnectError):
                connect_error_strategy = TimeoutConfig.get_network_error_strategy("ConnectError")
                
                if is_codex_request:
                    # Codex请求的ConnectError：每次都记录错误（保持原有逻辑）
                    retry_errors.append(f"[连接失败][{request_id}] Codex检测到连接错误")
                    msg = record_codex_error(codex_current_config_index, 503, silent=True)
                    if msg:
                        retry_errors.append(msg)
                # Claude请求的ConnectError：不在这里记录错误，改为在重试循环结束后统一记录
                
                # 为normal_retry策略设置错误记录标志
                if (not is_codex_request) and connect_error_strategy == "normal_retry":
                    last_error_status_code = 503
                    last_error_strategy = "normal_retry"
                    should_record_error_after_retry = True
                
                if (not is_codex_request) and connect_error_strategy == "switch_api":
                    # 配置为switch_api策略：强制切换API
                    connect_error_msg = f"[连接失败-切换API][{request_id}] 检测到连接错误，强制切换API"
                    retry_errors.append(connect_error_msg)
                    print(connect_error_msg, file=sys.stderr)  # ← 立即打印ConnectError检测
                    
                    # ConnectError视为严重连接错误，直接记录错误并尝试切换API
                    current_api_index = current_config_index
                    switch_success, new_api_index = smart_switch_api(current_api_index, 503)  # 使用503错误码触发切换
                    if switch_success:
                        connect_switch_msg = f"[连接失败-切换API成功][{request_id}] API切换成功，使用新API重试"
                        retry_errors.append(connect_switch_msg)
                        print(connect_switch_msg, file=sys.stderr)  # ← 立即打印ConnectError切换成功
                        # 重新构建请求头和URL，使用新API
                        is_valid, real_auth_header, error_msg = validate_and_replace_user_key(user_auth_header)
                        if is_valid:
                            headers['authorization'] = real_auth_header
                            new_upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, base_url_override)
                            
                            # 使用全新headers副本，强制断开旧连接
                            connect_error_retry_headers = headers.copy()
                            if TimeoutConfig.get_modify_retry_headers():
                                connect_error_retry_headers['connection'] = 'close'
                                import random
                                import time
                                connect_error_rand = random.randint(1000,9999)
                                connect_error_timestamp = int(time.time() * 1000)
                                connect_error_retry_headers['x-request-id'] = f"{request_id}-connecterror-{retry_attempt + 1}-{connect_error_rand}"
                                connect_error_retry_headers['cache-control'] = 'no-cache, no-store, must-revalidate'
                                connect_error_retry_headers['pragma'] = 'no-cache'
                                connect_error_retry_headers['expires'] = '0'
                                connect_error_retry_headers['x-cache-bypass'] = f'{connect_error_timestamp}-{connect_error_rand}'
                                connect_error_retry_headers['x-retry-count'] = str(retry_attempt + 1)
                        
                        try:
                            # 使用新的重试客户端
                            connect_error_retry_client = httpx.AsyncClient(timeout=timeout, limits=limits)
                            connect_error_upstream_req = connect_error_retry_client.build_request(
                                method=request.method,
                                url=new_upstream_url,
                                headers=connect_error_retry_headers,
                                content=converted_body
                            )
                            upstream_resp = await connect_error_retry_client.send(connect_error_upstream_req, stream=True)
                            retry_client = connect_error_retry_client  # 更新重试客户端引用
                            connect_status_msg = f"[连接失败-切换API][{request_id}] 新API响应状态码: {upstream_resp.status_code}"
                            retry_errors.append(connect_status_msg)
                            print(connect_status_msg, file=sys.stderr)  # ← 立即打印新API状态码
                            # 只有成功响应（< 400）才跳出重试循环，错误响应继续重试
                            if upstream_resp.status_code < 400:
                                retry_errors.clear()
                                break
                            else:
                                connect_error_status_msg = f"[连接失败-切换API][{request_id}] 新API仍返回错误{upstream_resp.status_code}，继续重试"
                                retry_errors.append(connect_error_status_msg)
                                print(connect_error_status_msg, file=sys.stderr)  # ← 立即打印新API错误
                                await upstream_resp.aclose()
                                await connect_error_retry_client.aclose()
                                # 不break，继续下一次重试
                        except Exception as connect_error_retry_exception:
                            retry_errors.append(f"[连接失败-切换API失败][{request_id}] 新API重试也失败: {connect_error_retry_exception}")
                            await connect_error_retry_client.aclose()
                    else:
                        connect_fail_msg = f"[连接失败-切换API失败][{request_id}] API切换失败，继续正常重试流程"
                        retry_errors.append(connect_fail_msg)
                        print(connect_fail_msg, file=sys.stderr)  # ← 立即打印ConnectError切换失败
            
            # 特殊处理网络错误和HTTPStatusError：根据配置决定是否使用策略重试
            is_read_timeout = isinstance(e, httpx.ReadTimeout)
            is_strategy_status = isinstance(e, httpx.HTTPStatusError) and "Strategy Retry Needed" in str(e)
            # 检查其他网络错误是否配置为strategy_retry
            is_read_error_strategy = isinstance(e, httpx.ReadError) and TimeoutConfig.get_network_error_strategy("ReadError") == "strategy_retry"
            is_connect_error_strategy = isinstance(e, httpx.ConnectError) and TimeoutConfig.get_network_error_strategy("ConnectError") == "strategy_retry"
            # ReadTimeout根据配置决定是否使用策略重试
            is_read_timeout_strategy = is_read_timeout and TimeoutConfig.get_network_error_strategy("ReadTimeout") == "strategy_retry"
            
            if (is_read_timeout_strategy or is_read_error_strategy or is_connect_error_strategy or is_strategy_status) and not is_codex_request:
                # 识别错误类型
                if is_read_timeout_strategy:
                    error_type = "读取超时"
                elif is_read_error_strategy:
                    error_type = "SSL读取错误"
                elif is_connect_error_strategy:
                    error_type = "连接失败"
                else:
                    error_type = "临时性状态码"
                strategy_detect_msg = f"[策略重试][{request_id}] 检测到{error_type}，尝试第{retry_attempt + 1}个策略"
                retry_errors.append(strategy_detect_msg)
                print(strategy_detect_msg, file=sys.stderr)  # ← 立即打印策略重试检测
                
                # 检查是否有对应的重试策略
                if retry_attempt < len(READ_TIMEOUT_RETRY_CONFIGS):
                    retry_config = READ_TIMEOUT_RETRY_CONFIGS[retry_attempt]
                    strategy_use_msg = f"[策略重试][{request_id}] 使用策略: {retry_config['name']}"
                    retry_errors.append(strategy_use_msg)
                    print(strategy_use_msg, file=sys.stderr)  # ← 立即打印使用策略
                    
                    # 构建重试URL（与build_upstream_url函数逻辑保持一致）
                    temp_upstream_url = f"{retry_config['base_url']}/{clean_path}"
                    
                    if request.url.query:
                        if is_openai_format:
                            temp_upstream_url += f"?{request.url.query}&beta=true"
                        else:
                            temp_upstream_url += f"?{request.url.query}"
                    elif is_openai_format:
                        temp_upstream_url += "?beta=true"
                    
                    # 构建临时请求头，使用策略配置的key
                    temp_headers = headers.copy()
                    temp_headers['authorization'] = f"Bearer {retry_config['key']}"
                    if TimeoutConfig.get_modify_retry_headers():
                        temp_headers['connection'] = 'close'
                        # 添加完整的防缓存头部，确保ReadTimeout重试时API不使用缓存
                        import time
                        temp_rand = random.randint(1000,9999)
                        temp_timestamp = int(time.time() * 1000)
                        temp_headers['x-request-id'] = f"{request_id}-readtimeout-{retry_attempt + 1}-{temp_rand}"
                        temp_headers['cache-control'] = 'no-cache, no-store, must-revalidate'
                        temp_headers['pragma'] = 'no-cache'
                        temp_headers['expires'] = '0'
                        temp_headers['x-cache-bypass'] = f'{temp_timestamp}-{temp_rand}'
                        temp_headers['x-retry-count'] = str(retry_attempt + 1)
                    
                    # 使用策略重试专用的超时配置（200秒读取超时）
                    extended_timeout = TimeoutConfig.get_strategy_retry_timeout()
                    temp_client = httpx.AsyncClient(timeout=extended_timeout, limits=limits)
                    try:
                        temp_upstream_req = temp_client.build_request(
                            method=request.method,
                            url=temp_upstream_url,
                            headers=temp_headers,
                            content=converted_body
                        )
                        upstream_resp = await temp_client.send(temp_upstream_req, stream=True)
                        strategy_status_msg = f"[策略重试][{request_id}] {retry_config['name']} 响应状态码: {upstream_resp.status_code}"
                        retry_errors.append(strategy_status_msg)
                        print(strategy_status_msg, file=sys.stderr)  # ← 立即打印策略响应状态码
                        
                        # 只有成功响应（< 400）才跳出重试循环，错误响应继续重试
                        if upstream_resp.status_code < 400:
                            retry_errors.clear()
                            retry_client = temp_client
                            break
                        else:
                            strategy_error_msg = f"[策略重试][{request_id}] {retry_config['name']} 仍返回错误{upstream_resp.status_code}，继续重试"
                            retry_errors.append(strategy_error_msg)
                            print(strategy_error_msg, file=sys.stderr)  # ← 立即打印策略错误
                            await upstream_resp.aclose()
                            await temp_client.aclose()
                            # 不break，继续下一次重试
                        
                    except Exception as strategy_error:
                        error_type = type(strategy_error).__name__
                        error_msg = str(strategy_error) or "无错误信息"
                        strategy_fail_msg1 = f"[策略重试][{request_id}] {retry_config['name']} 失败"
                        strategy_fail_msg2 = f"[策略重试][{request_id}] 错误类型: {error_type}"
                        strategy_fail_msg3 = f"[策略重试][{request_id}] 错误详情: {error_msg}"
                        retry_errors.append(strategy_fail_msg1)
                        retry_errors.append(strategy_fail_msg2)
                        retry_errors.append(strategy_fail_msg3)
                        print(strategy_fail_msg1, file=sys.stderr)  # ← 立即打印策略失败
                        print(strategy_fail_msg2, file=sys.stderr)
                        print(strategy_fail_msg3, file=sys.stderr)
                        retry_errors.append(f"[策略重试][{request_id}] 尝试的URL: {temp_upstream_url}")
                        retry_errors.append(f"[策略重试][{request_id}] 使用的Key: {retry_config['key'][:20]}...")
                        
                        # 特殊检查：如果是https连接问题，给出建议
                        if "https://anyrouter.top" in temp_upstream_url:
                            if "timeout" in error_msg.lower() or isinstance(strategy_error, (httpx.ReadTimeout, httpx.ConnectTimeout)):
                                retry_errors.append(f"[读取超时-策略重试][{request_id}] 提示: anyrouter.top可能网络延迟较高，考虑检查网络连接")
                            elif "ssl" in error_msg.lower() or "certificate" in error_msg.lower():
                                retry_errors.append(f"[读取超时-策略重试][{request_id}] 提示: anyrouter.top可能有SSL证书问题")
                        
                        await temp_client.aclose()
                        # 继续下一个重试策略
                else:
                    retry_errors.append(f"[读取超时-策略重试][{request_id}] 已超出预定义策略数量，回到正常重试逻辑")
                
                # Claude请求：如果执行到这里，说明当前的strategy_retry尝试失败了
                # 更新错误追踪信息（但不立即记录，等所有重试都失败后统一记录）
                if not is_codex_request:
                    last_error_strategy = "strategy_retry"
                    should_record_error_after_retry = True
            
            if retry_attempt < max_retries - 1:
                # 还有重试机会，使用递增延迟（指数退避）让网络状态有时间恢复
                import asyncio
                delay = 2 ** retry_attempt  # 1, 2, 4, 8秒的递增延迟
                retry_errors.append(f"[{request_id}] 等待 {delay} 秒后重试...")
                await asyncio.sleep(delay)
                continue
            else:
                # 最后一次重试失败，输出所有收集的错误信息
                for err in retry_errors:
                    print(err, file=sys.stderr)
                
                # Claude请求：在所有重试都失败后，统一记录错误
                if not is_codex_request and should_record_error_after_retry:
                    if last_error_strategy == "switch_api" and last_error_status_code:
                        # switch_api策略：重试max_retries次都失败，记录+1次错误
                        msg = record_api_error(current_config_index, last_error_status_code, silent=True)
                        if msg:
                            print(msg, file=sys.stderr)
                    elif last_error_strategy == "strategy_retry":
                        # strategy_retry策略：所有备用节点都失败，记录+1次错误
                        msg = record_api_error(current_config_index, 503, silent=True)
                        if msg:
                            print(msg, file=sys.stderr)
                    elif last_error_strategy == "normal_retry" and last_error_status_code:
                        # normal_retry策略：重试max_retries次都失败，记录+1次错误
                        msg = record_api_error(current_config_index, last_error_status_code, silent=True)
                        if msg:
                            print(msg, file=sys.stderr)
                
                import traceback
                error_message = f"Proxy Error: Could not connect to upstream server at {upstream_url}. Exception: {e}"
                print(f"[{request_id}] {error_message}", file=sys.stderr)
                print(f"[{request_id}] 连接错误详情: {traceback.format_exc()}", file=sys.stderr)
                print(f"[{request_id}] 请求方法: {request.method}, 目标URL: {upstream_url}", file=sys.stderr)
                print(f"[{request_id}] 请求头: {dict(headers)}", file=sys.stderr)
                
                from fastapi.responses import Response
                
                # switch_api策略：不立即返回错误，尝试切换所有可用API
                strategies = config_mgr.get_error_handling_strategies()
                http_codes = strategies.get("http_status_codes", {})
                switch_api_codes = [int(code) for code, strategy in http_codes.items()
                                  if strategy == "switch_api" and code != "default"]
                
                # 如果配置了switch_api策略，尝试扩展重试
                if len(switch_api_codes) > 0:
                    extended_retry_success = False
                    max_api_count = len(CODEX_CONFIGS) if is_codex_request else len(API_CONFIGS)
                    extended_switch_count = 0
                    max_extended_switches = max_api_count * 3  # 每个API最多尝试3次
                    
                    print(f"[switch_api扩展重试][{request_id}] 主重试循环失败，开始尝试其他可用API...", file=sys.stderr)
                    
                    while extended_switch_count < max_extended_switches:
                        # 尝试切换API
                        if is_codex_request:
                            switch_success, new_index = smart_codex_switch_api(codex_current_config_index, 503)
                        else:
                            switch_success, new_index = smart_switch_api(current_config_index, 503)
                        
                        if not switch_success:
                            print(f"[switch_api扩展重试][{request_id}] 无法切换到新API，所有API已尝试", file=sys.stderr)
                            break
                        
                        extended_switch_count += 1
                        print(f"[switch_api扩展重试][{request_id}] 第{extended_switch_count}次API切换", file=sys.stderr)
                        
                        # 重新构建请求
                        try:
                            if is_codex_request:
                                current_codex_config = get_current_codex_config()
                                print(f"\n{get_current_codex_info()}")
                                upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, current_codex_config["base_url"])
                                headers['authorization'] = f'Bearer {current_codex_config["key"]}'
                                
                                codex_timeout = httpx.Timeout(
                                    connect=TimeoutConfig.get_connect_timeout(),
                                    read=None,
                                    write=TimeoutConfig.get_write_timeout(),
                                    pool=TimeoutConfig.get_pool_timeout()
                                )
                                extended_client = httpx.AsyncClient(timeout=codex_timeout, limits=limits)
                            else:
                                is_valid, real_auth_header, error_msg = validate_and_replace_user_key(user_auth_header)
                                if is_valid:
                                    headers['authorization'] = real_auth_header
                                    upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, base_url_override)
                                    print(f"\n{get_current_api_info()}")
                                else:
                                    print(f"[switch_api扩展重试][{request_id}] 验证Key失败: {error_msg}", file=sys.stderr)
                                    break
                                
                                if should_convert_to_openai and not user_wants_stream:
                                    extended_client = httpx.AsyncClient(timeout=non_streaming_timeout, limits=limits)
                                else:
                                    extended_client = httpx.AsyncClient(timeout=timeout, limits=limits)
                            
                            # 发送请求
                            extended_headers = headers.copy()
                            if TimeoutConfig.get_modify_retry_headers():
                                extended_headers['connection'] = 'close'
                                import random, time
                                ext_rand = random.randint(1000,9999)
                                ext_timestamp = int(time.time() * 1000)
                                extended_headers['x-request-id'] = f"{request_id}-extended-{extended_switch_count}-{ext_rand}"
                                extended_headers['cache-control'] = 'no-cache, no-store, must-revalidate'
                                extended_headers['pragma'] = 'no-cache'
                                extended_headers['expires'] = '0'
                                extended_headers['x-cache-bypass'] = f'{ext_timestamp}-{ext_rand}'
                            
                            extended_req = extended_client.build_request(
                                method=request.method,
                                url=upstream_url,
                                headers=extended_headers,
                                content=converted_body
                            )
                            
                            if is_codex_request:
                                import asyncio
                                try:
                                    extended_resp = await asyncio.wait_for(
                                        extended_client.send(extended_req, stream=True),
                                        timeout=TimeoutConfig.get_codex_connect_timeout()
                                    )
                                except asyncio.TimeoutError:
                                    print(f"[switch_api扩展重试][{request_id}] Codex连接超时", file=sys.stderr)
                                    await extended_client.aclose()
                                    continue
                            else:
                                extended_resp = await extended_client.send(extended_req, stream=True)
                            
                            print(f"[switch_api扩展重试][{request_id}] 响应状态码: {extended_resp.status_code}", file=sys.stderr)
                            
                            # 检查响应
                            if extended_resp.status_code < 400:
                                # 成功！使用这个响应
                                print(f"[switch_api扩展重试][{request_id}] 成功！使用新API响应", file=sys.stderr)
                                upstream_resp = extended_resp
                                retry_client = extended_client
                                extended_retry_success = True
                                retry_errors.clear()  # 清空错误列表
                                break
                            else:
                                # 失败，继续尝试其他API
                                print(f"[switch_api扩展重试][{request_id}] API返回错误{extended_resp.status_code}，继续尝试其他API", file=sys.stderr)
                                if is_codex_request:
                                    record_codex_error(codex_current_config_index, extended_resp.status_code, silent=True)
                                else:
                                    record_api_error(current_config_index, extended_resp.status_code, silent=True)
                                await extended_resp.aclose()
                                await extended_client.aclose()
                                continue
                        
                        except Exception as extended_error:
                            print(f"[switch_api扩展重试][{request_id}] 扩展重试异常: {extended_error}", file=sys.stderr)
                            if is_codex_request:
                                record_codex_error(codex_current_config_index, 503, silent=True)
                            else:
                                record_api_error(current_config_index, 503, silent=True)
                            await extended_client.aclose()
                            continue
                    
                    # 如果扩展重试成功，不返回错误，继续正常流程
                    if not extended_retry_success:
                        print(f"[switch_api扩展重试][{request_id}] 所有API均已尝试，仍然失败", file=sys.stderr)
                        return Response(content=error_message, status_code=502)
                else:
                    # 非switch_api策略，直接返回错误
                    return Response(content=error_message, status_code=502)
    
    # 确保关闭重试创建的client实例（如果有的话）
    if 'retry_client' in locals() and retry_client != client:
        # 延迟关闭，确保响应处理完成后再关闭
        pass  # 后续在finally块中处理
    
    # 删除详细的上游响应记录
    
    # 检查上游响应状态码，处理错误情况
    if upstream_resp.status_code < 400:
        # 请求成功，重置当前API的错误计数
        if not is_codex_request:
            current_api_index = current_config_index
            if (api_status[current_api_index]["error_count"] > 0 or
                api_status[current_api_index]["cooldown_until"] is not None):
                api_status[current_api_index].update({
                    "error_count": 0,
                    "cooldown_until": None,
                    "status": "normal"
                })
                print(f"[{datetime.now().strftime('%H:%M:%S')}] API {API_CONFIGS[current_api_index]['name']} 请求成功，完全重置状态", file=sys.stderr)
        else:
            # Codex请求成功，重置错误计数
            current_codex_index = codex_current_config_index
            if current_codex_index < len(CODEX_CONFIGS) and current_codex_index in codex_api_status:
                if (codex_api_status[current_codex_index]["error_count"] > 0 or
                    codex_api_status[current_codex_index]["cooldown_until"] is not None):
                    codex_api_status[current_codex_index].update({
                        "error_count": 0,
                        "cooldown_until": None,
                        "status": "normal"
                    })
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Codex {CODEX_CONFIGS[current_codex_index]['name']} 请求成功，完全重置状态", file=sys.stderr)
    else:
        # 重试循环正常结束但请求失败（status_code >= 400）
        # 对于switch_api策略，先尝试扩展重试，只有所有API都失败后才记录错误
        strategies = config_mgr.get_error_handling_strategies()
        http_codes = strategies.get("http_status_codes", {})
        switch_api_codes = [int(code) for code, strategy in http_codes.items()
                          if strategy == "switch_api" and code != "default"]
        
        # 检查是否是switch_api策略的错误
        if upstream_resp.status_code in switch_api_codes and len(switch_api_codes) > 0:
            extended_retry_success = False
            max_api_count = len(CODEX_CONFIGS) if is_codex_request else len(API_CONFIGS)
            extended_switch_count = 0
            max_extended_switches = max_api_count * 3  # 每个API最多尝试3次
            
            print(f"[switch_api扩展重试][{request_id}] 检测到switch_api错误{upstream_resp.status_code}，开始尝试其他可用API...", file=sys.stderr)
            
            # 保存错误状态码
            failed_status_code = upstream_resp.status_code
            
            # 关闭当前失败的响应
            await upstream_resp.aclose()
            
            while extended_switch_count < max_extended_switches:
                # 尝试切换API
                if is_codex_request:
                    switch_success, new_index = smart_codex_switch_api(codex_current_config_index, failed_status_code)
                else:
                    switch_success, new_index = smart_switch_api(current_config_index, failed_status_code)
                
                if not switch_success:
                    print(f"[switch_api扩展重试][{request_id}] 无法切换到新API，所有API已尝试", file=sys.stderr)
                    break
                
                extended_switch_count += 1
                print(f"[switch_api扩展重试][{request_id}] 第{extended_switch_count}次API切换", file=sys.stderr)
                
                # 重新构建请求
                try:
                    if is_codex_request:
                        current_codex_config = get_current_codex_config()
                        print(f"\n{get_current_codex_info()}")
                        upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, current_codex_config["base_url"])
                        headers['authorization'] = f'Bearer {current_codex_config["key"]}'
                        
                        codex_timeout = httpx.Timeout(
                            connect=TimeoutConfig.get_connect_timeout(),
                            read=None,
                            write=TimeoutConfig.get_write_timeout(),
                            pool=TimeoutConfig.get_pool_timeout()
                        )
                        extended_client = httpx.AsyncClient(timeout=codex_timeout, limits=limits)
                    else:
                        is_valid, real_auth_header, error_msg = validate_and_replace_user_key(user_auth_header)
                        if is_valid:
                            headers['authorization'] = real_auth_header
                            upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, base_url_override)
                            print(f"\n{get_current_api_info()}")
                        else:
                            print(f"[switch_api扩展重试][{request_id}] 验证Key失败: {error_msg}", file=sys.stderr)
                            break
                        
                        if should_convert_to_openai and not user_wants_stream:
                            extended_client = httpx.AsyncClient(timeout=non_streaming_timeout, limits=limits)
                        else:
                            extended_client = httpx.AsyncClient(timeout=timeout, limits=limits)
                    
                    # 发送请求
                    extended_headers = headers.copy()
                    if TimeoutConfig.get_modify_retry_headers():
                        extended_headers['connection'] = 'close'
                        import random, time
                        ext_rand = random.randint(1000,9999)
                        ext_timestamp = int(time.time() * 1000)
                        extended_headers['x-request-id'] = f"{request_id}-extended-{extended_switch_count}-{ext_rand}"
                        extended_headers['cache-control'] = 'no-cache, no-store, must-revalidate'
                        extended_headers['pragma'] = 'no-cache'
                        extended_headers['expires'] = '0'
                        extended_headers['x-cache-bypass'] = f'{ext_timestamp}-{ext_rand}'
                    
                    extended_req = extended_client.build_request(
                        method=request.method,
                        url=upstream_url,
                        headers=extended_headers,
                        content=converted_body
                    )
                    
                    if is_codex_request:
                        import asyncio
                        try:
                            extended_resp = await asyncio.wait_for(
                                extended_client.send(extended_req, stream=True),
                                timeout=TimeoutConfig.get_codex_connect_timeout()
                            )
                        except asyncio.TimeoutError:
                            print(f"[switch_api扩展重试][{request_id}] Codex连接超时", file=sys.stderr)
                            await extended_client.aclose()
                            continue
                    else:
                        extended_resp = await extended_client.send(extended_req, stream=True)
                    
                    print(f"[switch_api扩展重试][{request_id}] 响应状态码: {extended_resp.status_code}", file=sys.stderr)
                    
                    # 检查响应
                    if extended_resp.status_code < 400:
                        # 成功！使用这个响应
                        print(f"[switch_api扩展重试][{request_id}] 成功！使用新API响应", file=sys.stderr)
                        upstream_resp = extended_resp
                        retry_client = extended_client
                        extended_retry_success = True
                        retry_errors.clear()  # 清空错误列表
                        break
                    elif extended_resp.status_code in switch_api_codes:
                        # 仍然是switch_api错误，继续尝试其他API
                        print(f"[switch_api扩展重试][{request_id}] API返回错误{extended_resp.status_code}，继续尝试其他API", file=sys.stderr)
                        if is_codex_request:
                            record_codex_error(codex_current_config_index, extended_resp.status_code, silent=True)
                        else:
                            record_api_error(current_config_index, extended_resp.status_code, silent=True)
                        await extended_resp.aclose()
                        await extended_client.aclose()
                        continue
                    else:
                        # 不同类型的错误，停止扩展重试
                        print(f"[switch_api扩展重试][{request_id}] API返回非switch_api错误{extended_resp.status_code}，停止扩展重试", file=sys.stderr)
                        upstream_resp = extended_resp
                        retry_client = extended_client
                        break
                
                except Exception as extended_error:
                    print(f"[switch_api扩展重试][{request_id}] 扩展重试异常: {extended_error}", file=sys.stderr)
                    if is_codex_request:
                        record_codex_error(codex_current_config_index, 503, silent=True)
                    else:
                        record_api_error(current_config_index, 503, silent=True)
                    await extended_client.aclose()
                    continue
            
            # 如果扩展重试失败，继续记录错误
            if not extended_retry_success:
                print(f"[switch_api扩展重试][{request_id}] 所有API均已尝试，仍然失败，记录错误", file=sys.stderr)
        
        # 记录错误（原有逻辑）
        if not is_codex_request and should_record_error_after_retry and upstream_resp.status_code >= 400:
            if last_error_strategy == "switch_api" and last_error_status_code:
                # switch_api策略：重试max_retries次都失败，记录+1次错误
                msg = record_api_error(current_config_index, last_error_status_code, silent=True)
                if msg:
                    print(msg, file=sys.stderr)
            elif last_error_strategy == "strategy_retry":
                # strategy_retry策略：所有备用节点都失败，记录+1次错误
                msg = record_api_error(current_config_index, 503, silent=True)
                if msg:
                    print(msg, file=sys.stderr)
            elif last_error_strategy == "normal_retry" and last_error_status_code:
                # normal_retry策略：重试max_retries次都失败，记录+1次错误
                msg = record_api_error(current_config_index, last_error_status_code, silent=True)
                if msg:
                    print(msg, file=sys.stderr)
    
    if upstream_resp.status_code >= 400:
        error_msg = f"上游API返回错误状态码: {upstream_resp.status_code}"
        
        # HTTP状态码错误处理
        
        # 处理持续性认证/权限错误，尝试智能切换API并重试
        # 注意：临时性错误（400, 404, 429, 500, 502, 503, 520-524）已由策略重试处理
        if (not is_codex_request) and upstream_resp.status_code in [401, 403]:
            # 获取当前API索引
            current_api_index = current_config_index
            
            # 尝试智能切换API
            switch_success, new_api_index = smart_switch_api(current_api_index, upstream_resp.status_code)
            
            if switch_success:
                
                # 重新构建请求头，使用新的API key
                is_valid, real_auth_header, error_msg = validate_and_replace_user_key(user_auth_header)
                if is_valid:
                    headers['authorization'] = real_auth_header
                    
                    # 重新构建请求URL使用新API
                    try:
                        await upstream_resp.aclose()  # 关闭原有连接
                        
                        # 重新构建URL使用新API (动态头部已自动避免缓存)
                        new_upstream_url = build_upstream_url(clean_path, request.url.query, is_openai_format, base_url_override)
                        
                        # 获取重试API配置
                        retry_config = get_current_config()
                        
                        # API切换重试也要使用全新headers副本，强制断开旧连接
                        api_switch_headers = headers.copy()
                        if TimeoutConfig.get_modify_retry_headers():
                            api_switch_headers['connection'] = 'close'
                            # 添加完整的防缓存头部，确保API切换重试时不使用缓存
                            import time
                            api_switch_rand = random.randint(1000,9999)
                            api_switch_timestamp = int(time.time() * 1000)
                            api_switch_headers['x-request-id'] = f"{request_id}-apiswitch-{api_switch_rand}"
                            api_switch_headers['cache-control'] = 'no-cache, no-store, must-revalidate'
                            api_switch_headers['pragma'] = 'no-cache'
                            api_switch_headers['expires'] = '0'
                            api_switch_headers['x-cache-bypass'] = f'{api_switch_timestamp}-{api_switch_rand}'
                        
                        upstream_req = retry_client.build_request(
                            method=request.method,
                            url=new_upstream_url,
                            headers=api_switch_headers,  # 使用API切换专用headers
                            content=converted_body
                        )
                        upstream_resp = await retry_client.send(upstream_req, stream=True)
                        
                        
                        # 如果重试成功，继续正常处理
                        if upstream_resp.status_code < 400:
                            # 记录成功切换到日志
                            if ENABLE_FULL_LOG and full_logger:
                                full_logger.info(f"错误重试成功 - 状态码: {upstream_resp.status_code} - 使用: {API_CONFIGS[new_api_index]['name']}")
                        else:
                            pass
                    except Exception as retry_error:
                        pass
                else:
                    pass
            else:
                pass
        # 如果仍然是错误状态码，执行原有错误处理逻辑
        if upstream_resp.status_code >= 400:
            # 对于OpenAI客户端，转换错误响应格式
            if should_convert_to_openai:
                try:
                    # 读取错误响应内容
                    error_content = await upstream_resp.aread()
                    error_text = error_content.decode('utf-8', errors='ignore')
                    
                    # 构造OpenAI格式的错误响应
                    openai_error = {
                        "error": {
                            "message": f"Upstream API error (status {upstream_resp.status_code}): {error_text}",
                            "type": "upstream_error",
                            "code": "api_error"
                        }
                    }
                    
                    if ENABLE_FULL_LOG and full_logger:
                        full_logger.error(f"上游API错误 - 状态码: {upstream_resp.status_code}")
                        full_logger.error(f"错误内容: {error_text}")
                        full_logger.error(f"请求ID: {request_id} - 处理失败")
                        full_logger.error("="*80)
                        # 检查并修剪日志文件大小
                        trim_log_file(LOG_FILE_PATH)
                    
                    return JSONResponse(
                        content=openai_error,
                        status_code=upstream_resp.status_code,
                        headers={"content-type": "application/json"}
                    )
                except Exception as error_process_error:
                    print(f"处理上游错误响应时出错: {error_process_error}", file=sys.stderr)

    # 7. 根据用户原始请求决定响应处理方式
    if should_convert_to_openai and not user_wants_stream:
        # 用户要求非流式响应，需要收集完整流式数据然后转换为JSON
        try:
            # 收集所有流式数据
            all_chunks = []
            async for chunk in upstream_resp.aiter_raw():
                all_chunks.append(chunk)
            
            # 合并所有数据
            complete_response = b''.join(all_chunks)
            response_text = complete_response.decode('utf-8', errors='ignore')
            
            
            # 【错误检测】使用增强的错误检测功能
            is_error, error_info, decompressed_content = detect_compressed_error(response_text.encode('utf-8'))
            
            # 如果检测到错误，使用统一错误处理函数
            if is_error and not is_codex_request:
                handle_detected_error(request_id, error_info, decompressed_content, "非流式")
            
            # 解析流式数据并提取内容
            full_content = ""
            lines = response_text.split('\n')
            
            for line in lines:
                if line.startswith('data: ') and line != 'data: [DONE]':
                    try:
                        json_str = line[6:]  # 移除 'data: '
                        claude_data = json.loads(json_str)
                        
                        # 提取文本内容
                        if claude_data.get("type") == "content_block_delta":
                            delta = claude_data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                full_content += delta.get("text", "")
                                
                    except json.JSONDecodeError:
                        continue
            
            # 构造标准OpenAI JSON响应
            openai_response = {
                "id": "chatcmpl-adapter",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": original_request_data.get("model", "gpt-4"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": full_content
                        },
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            }
            
            # 构建响应信息显示
            response_info = f"响应: {upstream_resp.status_code}"
            if model_conversion_info:
                response_info += f" | {model_conversion_info}"
            response_info += " | Claude → OpenAI格式"
            
            print(response_info)
            
            # 记录完整输出数据（非流式响应）
            log_original_response(request_id, all_chunks, is_codex_request)

            # ✅ 实时统计token使用量（非流式响应）
            if stats_mgr and all_chunks:
                try:
                    usage_data = extract_usage_from_chunks(all_chunks, is_codex_request)
                    if usage_data:
                        # 获取模型名称
                        model_name = "unknown"
                        try:
                            if 'user_model' in locals():
                                model_name = user_model
                            elif body:
                                request_data = json.loads(body.decode('utf-8'))
                                model_name = request_data.get('model', 'unknown')
                        except:
                            pass

                        # 记录统计数据
                        stats_mgr.record_usage(
                            model=model_name,
                            usage_data=usage_data,
                            request_id=request_id
                        )
                except Exception as stats_error:
                    pass

            return JSONResponse(
                content=openai_response,
                status_code=upstream_resp.status_code,
                headers={"content-type": "application/json"}
            )
            
        except Exception as e:
            print(f"非流式响应转换出错: {e}", file=sys.stderr)
            error_response = {
                "error": {
                    "message": f"Response conversion failed: {str(e)}",
                    "type": "conversion_error"
                }
            }
            return JSONResponse(content=error_response, status_code=500)

    # 8. 流式响应处理（用户要求流式或非OpenAI客户端）  
    response_chunks = []
    is_stream_started = False
    
    async def stream_generator():
        nonlocal is_stream_started
        global codex_timeout_extra_seconds, codex_success_count
        connection_interrupted = False  # 连接中断标志
        line_buffer = ""  # 缓冲区用于处理TCP分包的SSE流
        
        # Codex流式读取总超时（基础超时 + 额外超时秒数）
        stream_total_timeout = None
        stream_start_time = None
        stream_aiter = None
        
        if is_codex_request:
            import time
            import asyncio
            codex_base_timeout = TimeoutConfig.get_codex_base_timeout()
            with codex_timeout_lock:
                current_extra_seconds = codex_timeout_extra_seconds
            stream_total_timeout = codex_base_timeout + current_extra_seconds
            stream_start_time = time.time()
            # 获取异步迭代器
            stream_aiter = upstream_resp.aiter_raw().__aiter__()
        else:
            stream_aiter = upstream_resp.aiter_raw().__aiter__()
        
        try:
            while True:
                try:
                    # Codex请求使用精确的asyncio超时控制
                    if is_codex_request:
                        # 计算剩余时间
                        elapsed = time.time() - stream_start_time
                        remaining = stream_total_timeout - elapsed
                        
                        if remaining <= 0:
                            # 已经超时
                            connection_interrupted = True
                            print(f"\n[Codex流式超时] 总时间{elapsed:.1f}秒超过{stream_total_timeout}秒", file=sys.stderr)
                            
                            # 记录Codex流式超时错误
                            record_codex_error(codex_current_config_index, 503)
                            
                            codex_increment = TimeoutConfig.get_codex_timeout_increment()
                            with codex_timeout_lock:
                                codex_timeout_extra_seconds += codex_increment
                                codex_success_count = 0
                                new_timeout = codex_timeout_extra_seconds
                            codex_base_timeout = TimeoutConfig.get_codex_base_timeout()
                            print(f"[Codex自适应超时] 下次流式超时增加到 {codex_base_timeout + new_timeout}秒", file=sys.stderr)
                            raise httpx.ReadTimeout(f"Codex stream total timeout: {elapsed:.1f}s > {stream_total_timeout}s")
                        
                        # 使用asyncio.wait_for精确控制每次chunk等待的超时
                        try:
                            chunk = await asyncio.wait_for(stream_aiter.__anext__(), timeout=remaining)
                        except asyncio.TimeoutError:
                            # asyncio超时，精确到剩余时间
                            elapsed = time.time() - stream_start_time
                            connection_interrupted = True
                            print(f"\n[Codex流式超时] 总时间{elapsed:.1f}秒达到{stream_total_timeout}秒限制（精确检测）", file=sys.stderr)
                            
                            # 记录Codex流式精确超时错误
                            record_codex_error(codex_current_config_index, 503)
                            
                            codex_increment = TimeoutConfig.get_codex_timeout_increment()
                            with codex_timeout_lock:
                                codex_timeout_extra_seconds += codex_increment
                                codex_success_count = 0
                                new_timeout = codex_timeout_extra_seconds
                            codex_base_timeout = TimeoutConfig.get_codex_base_timeout()
                            print(f"[Codex自适应超时] 下次流式超时增加到 {codex_base_timeout + new_timeout}秒", file=sys.stderr)
                            raise httpx.ReadTimeout(f"Codex stream total timeout (precise): {elapsed:.1f}s >= {stream_total_timeout}s")
                    else:
                        # 非Codex请求，正常迭代
                        chunk = await stream_aiter.__anext__()
                    
                except StopAsyncIteration:
                    # 流式读取正常结束
                    break
                # 保存响应块
                response_chunks.append(chunk)
                
                # 第一次收到数据时打印响应信息（精简版）
                if not is_stream_started:
                    is_stream_started = True
                    
                    # 【错误检测】使用增强的错误检测功能
                    is_error, error_info, decompressed_content = detect_compressed_error(chunk)
                    
                    chunk_text = chunk.decode('utf-8', errors='ignore')
                    
                    # 如果检测到错误，使用统一错误处理函数
                    if is_error and not is_codex_request:
                        handle_detected_error(request_id, error_info, decompressed_content, "流式")
                    
                    response_info = f"响应: {upstream_resp.status_code}"
                    if model_conversion_info:
                        response_info += f" | {model_conversion_info}"
                    response_info += " | "
                    print(response_info, end="")
                    if should_convert_to_openai:
                        print("Claude → OpenAI格式（使用缓冲区处理TCP分包）")
                    else:
                        # 根据请求类型显示对应的原始格式
                        format_name = "Codex原始格式" if is_codex_request else "Claude原始格式"
                        print(format_name)
                    sys.stdout.flush()
                
                # 处理响应数据转换
                processed_chunk = chunk
                
                # 只有OpenAI客户端才转换响应格式
                if should_convert_to_openai:
                    content_type = str(upstream_resp.headers.get("content-type", ""))
                    
                    if "text/event-stream" in content_type:
                        # 流式响应转换（使用缓冲区处理TCP分包）
                        try:
                            chunk_text = chunk.decode('utf-8', errors='ignore')
                            line_buffer += chunk_text  # 累积到缓冲区
                            
                            converted_lines = []
                            
                            # 处理缓冲区中的完整行
                            while '\n' in line_buffer:
                                line, line_buffer = line_buffer.split('\n', 1)
                                line = line.strip()
                                
                                if not line:
                                    continue
                                    
                                if line.startswith('data: ') and line != 'data: [DONE]':
                                    try:
                                        json_str = line[6:]  # 移除 'data: '
                                        claude_data = json.loads(json_str)
                                        openai_data = convert_response_to_openai(claude_data)
                                        converted_lines.append(f'data: {json.dumps(openai_data, separators=(",", ":"))}')
                                        
                                        # OpenAI响应数据收集功能已删除
                                        
                                    except json.JSONDecodeError as e:
                                        # 检查是否是不完整的JSON（而非格式错误）
                                        json_str_stripped = json_str.rstrip()
                                        # 如果看起来像完整JSON（以}结尾）但仍解析失败，可能是格式错误
                                        if json_str_stripped.endswith('}') or json_str_stripped.endswith(']'):
                                            print(f"JSON格式错误，跳过此行: {e}, 内容: {line[:100]}", file=sys.stderr)
                                            continue  # 跳过这个错误行
                                        else:
                                            # 可能是不完整的JSON，放回缓冲区等待更多数据
                                            line_buffer = line + '\n' + line_buffer
                                            break
                                elif line == 'data: [DONE]':
                                    converted_lines.append('data: [DONE]')
                                elif line.startswith('event:'):
                                    # 过滤掉Claude特有的事件类型，只保留兼容OpenAI的
                                    continue
                                else:
                                    if line:  # 只添加非空行
                                        converted_lines.append(line)
                            
                            # 只有当有完整的转换行时才输出
                            if converted_lines:
                                processed_chunk = ('\n'.join(converted_lines) + '\n').encode('utf-8')
                            else:
                                # 如果没有完整的行，暂时不输出，等待更多数据
                                continue
                                
                        except Exception as convert_error:
                            # 转换出错时详细打印，但停止转换以避免格式混乱
                            import traceback
                            print(f"\n流式响应转换出错: {convert_error}", file=sys.stderr)
                            print(f"转换错误详情: {traceback.format_exc()}", file=sys.stderr)
                            # 删除原始chunk内容记录
                            # 如果是OpenAI客户端但转换失败，发送错误响应后退出
                            if should_convert_to_openai:
                                error_chunk = 'data: {"error": {"message": "Response conversion failed", "type": "conversion_error"}}\n\ndata: [DONE]\n'
                                yield error_chunk.encode('utf-8')
                                return
                            processed_chunk = chunk  # 非OpenAI客户端使用原始块
                    else:
                        # 非流式响应转换（JSON响应）
                        try:
                            chunk_text = chunk.decode('utf-8', errors='ignore')
                            if chunk_text.strip():
                                claude_data = json.loads(chunk_text)
                                openai_data = convert_response_to_openai(claude_data)
                                processed_chunk = json.dumps(openai_data, separators=(",", ":"), ensure_ascii=False).encode('utf-8')
                                
                                # 非流式响应转换记录功能已删除
                                
                        except json.JSONDecodeError:
                            # 不是完整的JSON，可能是分块传输，保持原样
                            pass
                        except Exception as convert_error:
                            # 转换出错时详细打印，但停止转换以避免格式混乱
                            import traceback
                            print(f"\nJSON响应转换出错: {convert_error}", file=sys.stderr)
                            print(f"转换错误详情: {traceback.format_exc()}", file=sys.stderr)
                            # 删除原始chunk内容记录
                            # 如果是OpenAI客户端但转换失败，返回错误JSON
                            if should_convert_to_openai:
                                error_response = {
                                    "error": {
                                        "message": "Response conversion failed",
                                        "type": "conversion_error"
                                    }
                                }
                                processed_chunk = json.dumps(error_response, separators=(",", ":")).encode('utf-8')
                            else:
                                processed_chunk = chunk
                
                # 精简的数据块显示（移除详细打印）
                # 只在调试时需要时才打印具体内容
                
                try:
                    yield processed_chunk  # 返回处理后的数据块
                except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as conn_error:
                    # 客户端断开连接，正常退出
                    print(f"\n客户端断开连接: {conn_error}", file=sys.stderr)
                    return
            
            # async for循环结束，处理缓冲区中剩余的数据
            if should_convert_to_openai and line_buffer.strip():
                try:
                    remaining_lines = line_buffer.strip().split('\n')
                    converted_lines = []
                    
                    for line in remaining_lines:
                        line = line.strip()
                        if not line:
                            continue
                            
                        if line.startswith('data: ') and line != 'data: [DONE]':
                            try:
                                json_str = line[6:]
                                claude_data = json.loads(json_str)
                                openai_data = convert_response_to_openai(claude_data)
                                converted_lines.append(f'data: {json.dumps(openai_data, separators=(",", ":"))}')
                            except json.JSONDecodeError:
                                print(f"缓冲区剩余数据无法解析: {line[:100]}", file=sys.stderr)
                        elif line == 'data: [DONE]':
                            converted_lines.append('data: [DONE]')
                        elif not line.startswith('event:'):
                            if line:
                                converted_lines.append(line)
                    
                    if converted_lines:
                        final_chunk = ('\n'.join(converted_lines) + '\n').encode('utf-8')
                        yield final_chunk
                except Exception as e:
                    print(f"处理剩余缓冲区数据时出错: {e}", file=sys.stderr)
                    
        except Exception as e:
            import traceback
            
            # 检查是否是ReadTimeout异常，直接转换为连接错误
            if isinstance(e, httpx.ReadTimeout):
                connection_interrupted = True
                error_msg = str(e)
                print(f"\n流处理超时: {e}", file=sys.stderr)
                
                # Codex请求超时时，增加超时时间（但如果是流式总超时，已经在上面处理过了）
                if is_codex_request and "Codex stream total timeout" not in error_msg:
                    # 这是httpx原生的ReadTimeout（每次读取超时），不是流式总超时
                    
                    # 记录Codex读取超时错误
                    record_codex_error(codex_current_config_index, 503)
                    
                    codex_increment = TimeoutConfig.get_codex_timeout_increment()
                    with codex_timeout_lock:
                        codex_timeout_extra_seconds += codex_increment
                        codex_success_count = 0  # 重置成功计数
                        new_timeout = codex_timeout_extra_seconds
                    codex_base_timeout = TimeoutConfig.get_codex_base_timeout()
                    print(f"[Codex自适应超时] 超时失败，下次超时增加到 {codex_base_timeout + new_timeout}秒", file=sys.stderr)
                
                # 抛出特殊异常用于外层重试检测
                raise ConnectionError(f"Stream read timeout: {e}")
            
            # 检查是否是连接中断相关的错误
            error_str = str(e).lower()
            connection_errors = [
                'peer closed connection',
                'incomplete chunked read',
                'remoteprotocolerror',
                'connection reset',
                'broken pipe',
                'connection aborted'
            ]
            
            is_connection_error = any(err in error_str for err in connection_errors)
            if is_connection_error:
                connection_interrupted = True
                print(f"\n流处理连接中断: {e}", file=sys.stderr)
                # 抛出特殊异常用于外层重试检测
                raise ConnectionError(f"Stream connection interrupted: {e}")
            else:
                print(f"\n流处理异常: {e}", file=sys.stderr)
                print(f"异常详情: {traceback.format_exc()}", file=sys.stderr)
                print(f"已处理的响应块数量: {len(response_chunks)}", file=sys.stderr)
                if upstream_resp:
                    print(f"上游响应状态: {upstream_resp.status_code}", file=sys.stderr)
                    print(f"上游响应头: {dict(upstream_resp.headers)}", file=sys.stderr)
        finally:
            # Codex请求成功时，增加成功计数（只有在有额外超时时才需要计数和重置）
            if is_codex_request and not connection_interrupted:
                with codex_timeout_lock:
                    if codex_timeout_extra_seconds > 0:
                        codex_success_count += 1
                        current_count = codex_success_count
                        print(f"\n[Codex自适应超时] 请求成功 (连续{current_count}/3次)", file=sys.stderr)
                        
                        # 连续3次成功，重置超时
                        if codex_success_count >= 3:
                            print(f"[Codex自适应超时] 连续3次成功，重置超时至默认 60秒", file=sys.stderr)
                            codex_timeout_extra_seconds = 0
                            codex_success_count = 0
            
            # 确保关闭retry_client，避免client状态累积
            if 'retry_client' in locals():
                try:
                    await retry_client.aclose()
                except Exception as close_error:
                    print(f"关闭retry_client时出错: {close_error}", file=sys.stderr)
            
            # 简化的完成记录
            if ENABLE_FULL_LOG and full_logger:
                try:
                    full_logger.info(f"请求ID: {request_id} - 处理完成")
                    full_logger.info("="*40)
                    trim_log_file(LOG_FILE_PATH)
                except Exception as log_error:
                    print(f"记录完成日志时出错: {log_error}", file=sys.stderr)
            
            # 记录完整输出数据
            log_original_response(request_id, response_chunks, is_codex_request)

            # ✅ 实时统计token使用量
            if stats_mgr and response_chunks:
                try:
                    usage_data = extract_usage_from_chunks(response_chunks, is_codex_request)
                    if usage_data:
                        # 获取模型名称
                        model_name = "unknown"
                        try:
                            if 'user_model' in locals():
                                model_name = user_model
                            elif body:
                                request_data = json.loads(body.decode('utf-8'))
                                model_name = request_data.get('model', 'unknown')
                        except:
                            pass

                        # 记录统计数据
                        stats_mgr.record_usage(
                            model=model_name,
                            usage_data=usage_data,
                            request_id=request_id
                        )
                except Exception as stats_error:
                    pass

            # OpenAI响应日志功能已删除
            
            # 精简的完成信息 - 连接中断时不输出完成信息
            if response_chunks and not connection_interrupted:
                completion_info = " ✓ 完成"
                if model_conversion_info:
                    completion_info = f" ✓ 完成 [{model_conversion_info}]"
                # 添加Codex请求的实际用时
                if is_codex_request and 'stream_start_time' in locals():
                    actual_elapsed = time.time() - stream_start_time
                    completion_info += f" (耗时: {actual_elapsed:.1f}秒)"
                print(completion_info)
                print("=" * 50)  # 结束分隔线
            elif connection_interrupted:
                print(f"\n❌ 连接中断 - 流处理未完成 (已处理 {len(response_chunks)} 个响应块)", file=sys.stderr)
                print("=" * 50)  # 结束分隔线
            # 确保上游连接正确关闭
            try:
                await upstream_resp.aclose()
            except Exception as close_error:
                print(f"关闭上游连接时出错: {close_error}", file=sys.stderr)

    # -------------------------------------------------------------------
    # 核心修改点: 处理响应头，特别是Content-Length
    # -------------------------------------------------------------------
    
    # 添加流处理重试机制
    max_stream_retries = 1  # 禁用流重试，避免重复发送（主重试逻辑已足够）
    for stream_retry_count in range(max_stream_retries):
        try:
            response_headers = dict(upstream_resp.headers)
            
            # 如果进行了OpenAI格式转换，需要移除Content-Length让FastAPI自动处理
            if should_convert_to_openai and "content-length" in response_headers:
                del response_headers["content-length"]  # 让FastAPI自动处理Content-Length

            return StreamingResponse(
                content=stream_generator(),
                status_code=upstream_resp.status_code,
                headers=response_headers,
                media_type=upstream_resp.headers.get("content-type")
            )
            
        except ConnectionError as ce:
            print(f"[流重试 {stream_retry_count + 1}/{max_stream_retries}][{request_id}] 检测到连接中断: {ce}", file=sys.stderr)
            
            if stream_retry_count < max_stream_retries - 1:
                # 还有重试机会，重新发起请求
                try:
                    await upstream_resp.aclose()  # 关闭当前连接
                    
                    # 重新发起请求（使用全新headers，强制断开旧连接）
                    # 根据请求类型选择合适的超时配置
                    if is_codex_request:
                        # Codex流重试：禁用read超时，由流式总超时控制
                        codex_timeout = httpx.Timeout(
                            connect=TimeoutConfig.get_connect_timeout(),
                            read=None,  # ✅ 禁用read超时，由流式总超时控制
                            write=TimeoutConfig.get_write_timeout(),
                            pool=TimeoutConfig.get_pool_timeout()
                        )
                        new_client = httpx.AsyncClient(timeout=codex_timeout, limits=limits)
                        codex_base_timeout = TimeoutConfig.get_codex_base_timeout()
                        with codex_timeout_lock:
                            current_extra_seconds = codex_timeout_extra_seconds
                        print(f"[流重试][Codex超时配置] 连接超时: {TimeoutConfig.get_codex_connect_timeout()}秒 | 流式总超时: {codex_base_timeout + current_extra_seconds}秒", file=sys.stderr)
                    elif should_convert_to_openai and not user_wants_stream:
                        # 非流式请求使用60秒超时
                        new_client = httpx.AsyncClient(timeout=non_streaming_timeout, limits=limits)
                    else:
                        # 流式请求使用标准超时
                        new_client = httpx.AsyncClient(timeout=timeout, limits=limits)
                    try:
                        # 流重试也要使用全新headers副本，避免连接复用
                        stream_retry_headers = headers.copy()
                        if TimeoutConfig.get_modify_retry_headers():
                            stream_retry_headers['connection'] = 'close'
                            # 添加完整的防缓存头部，确保流重试时API不使用缓存
                            import time
                            stream_retry_rand = random.randint(1000,9999)
                            stream_retry_timestamp = int(time.time() * 1000)
                            stream_retry_headers['x-request-id'] = f"{request_id}-stream-retry{stream_retry_count}-{stream_retry_rand}"
                            stream_retry_headers['cache-control'] = 'no-cache, no-store, must-revalidate'
                            stream_retry_headers['pragma'] = 'no-cache'
                            stream_retry_headers['expires'] = '0'
                            stream_retry_headers['x-cache-bypass'] = f'{stream_retry_timestamp}-{stream_retry_rand}'
                            stream_retry_headers['x-retry-count'] = str(stream_retry_count + 1)
                        
                        new_upstream_req = new_client.build_request(
                            method=request.method,
                            url=upstream_url,
                            headers=stream_retry_headers,  # 使用流重试专用headers
                            content=converted_body
                        )
                        
                        # Codex流重试也使用30秒连接超时
                        if is_codex_request:
                            import asyncio
                            try:
                                upstream_resp = await asyncio.wait_for(
                                    new_client.send(new_upstream_req, stream=True),
                                    timeout=TimeoutConfig.get_codex_connect_timeout()
                                )
                            except asyncio.TimeoutError:
                                print(f"[Codex流重试连接超时][{request_id}] {TimeoutConfig.get_codex_connect_timeout()}秒内未收到响应", file=sys.stderr)
                                
                                # 记录Codex流重试连接超时错误
                                record_codex_error(codex_current_config_index, 503)
                                
                                await new_client.aclose()
                                raise httpx.ReadTimeout("Codex stream retry connection timeout: 30 seconds")
                        else:
                            upstream_resp = await new_client.send(new_upstream_req, stream=True)
                        
                        print(f"[流重试 {stream_retry_count + 1}/{max_stream_retries}][{request_id}] 重新建立连接成功", file=sys.stderr)
                        
                        # 重置流处理相关变量
                        response_chunks = []
                        is_stream_started = False
                        
                        # 等待配置的时间后重试
                        import asyncio
                        await asyncio.sleep(TimeoutConfig.get_stream_retry_wait())
                        continue
                    finally:
                        # 确保关闭new_client连接
                        await new_client.aclose()
                    
                except Exception as retry_error:
                    print(f"[流重试 {stream_retry_count + 1}/{max_stream_retries}][{request_id}] 重连失败: {retry_error}", file=sys.stderr)
                    continue
            
            # 最后一次重试失败，返回错误响应
            from fastapi.responses import Response
            return Response(content=f"Stream processing failed after {max_stream_retries} retries: {ce}", status_code=502)

if __name__ == "__main__":
    import uvicorn
    import logging
    
    # 配置日志级别，减少不必要的输出
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
    # 显示当前API配置状态
    print("\n" + "=" * 60)
    print("Claude Code API Server Startup")
    print("=" * 60)
    print("API轮动配置:")
    
    # 显示按优先级排序的主API
    primary_apis = [cfg for cfg in API_CONFIGS if cfg.get('type', 'primary') == 'primary']
    if primary_apis:
        print("  主API（按配置优先级顺序）:")
        for rank, config in enumerate(primary_apis, start=1):
            print(f"    优先级#{rank}: {config['name']} | {config['base_url']}")
            print(f"      Key: {config['key'][:20]}...")

    # 显示备用API（type=backup）
    backup_apis = [cfg for cfg in API_CONFIGS if cfg.get('type') == 'backup']
    if backup_apis:
        print("  备用API（全周可用）:")
        for config in backup_apis:
            print(f"    {config['name']}: {config['base_url']}")
            print(f"      Key: {config['key'][:20]}...")

    print("轮动说明: 主API按配置顺序自动选用，主API不可用时顺延下一优先级")
    print("恢复说明: 主API恢复后自动切回，配合错误计数和冷却监控")
    print("支持格式: OpenAI格式自动转换为Claude格式")
    print(f"日志功能: 已启用API输入输出日志，最大{MAX_LOG_SIZE/1024/1024:.0f}MB")
    # print("🔄 新功能: 启动时API健康检查，4/5/6/9/10/11点定时健康检查（使用OpenAI→Claude格式，claude-sonnet-4-5-20250929模型）")  # 【已注释】健康检查功能
    print("错误检测: 增强错误检测，即使200状态也检查响应内容，支持压缩错误解析")
    print("端口: 5101")
    print("=" * 60 + "\n")
    
    # 确保依赖已安装: pip install "fastapi[all]" httpx
    # 使用端口5101，禁用access log
    uvicorn.run(app, host="0.0.0.0", port=5101, access_log=False, log_level="warning")

















