#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一配置管理模块
支持多类型配置: API配置、Codex配置、OpenAI转Claude配置、超时重试配置
"""
import json
import copy
import os
import threading
from typing import List, Dict, Any, Optional
from datetime import datetime


class ConfigManager:
    """统一配置管理器 - 支持多种配置类型"""
    
    def __init__(self, config_file: str = "json_data/all_configs.json"):
        # 如果是相对路径，转换为基于脚本目录的绝对路径
        if not os.path.isabs(config_file):
            script_dir = os.path.dirname(os.path.abspath(__file__))
            config_file = os.path.join(script_dir, config_file)

        self.config_file = config_file
        self.lock = threading.RLock()
        self._all_configs = {}
        self.load_all_configs()
    
    def load_all_configs(self) -> Dict[str, Any]:
        """加载所有配置"""
        with self.lock:
            if os.path.exists(self.config_file):
                try:
                    with open(self.config_file, 'r', encoding='utf-8') as f:
                        self._all_configs = json.load(f)
                    
                    # 补充缺失的字段（向后兼容旧配置文件）
                    default_configs = self._get_default_all_configs()
                    config_updated = False
                    
                    # 如果没有model_conversions字段，从默认配置补充
                    if "model_conversions" not in self._all_configs:
                        self._all_configs["model_conversions"] = default_configs["model_conversions"]
                        config_updated = True
                        print(f"[配置管理] 自动补充缺失的model_conversions配置")

                    # 如果是旧版单配置格式，转换为列表格式
                    if "openai_to_claude_configs" not in self._all_configs:
                        single_config = self._all_configs.pop("openai_to_claude_config", None)
                        if isinstance(single_config, dict):
                            single_config.setdefault("enabled", True)
                            self._all_configs["openai_to_claude_configs"] = [single_config]
                            config_updated = True
                            print(f"[配置管理] 自动迁移openai_to_claude配置为多配置列表")
                        else:
                            self._all_configs["openai_to_claude_configs"] = default_configs["openai_to_claude_configs"]
                            config_updated = True
                            print(f"[配置管理] 自动补充缺失的openai_to_claude_configs配置")

                    # 确保openai_to_claude_configs为列表结构
                    if not isinstance(self._all_configs.get("openai_to_claude_configs"), list):
                        self._all_configs["openai_to_claude_configs"] = default_configs["openai_to_claude_configs"]
                        config_updated = True
                        print(f"[配置管理] 修正openai_to_claude_configs配置格式")

                    # 保存更新后的配置
                    if config_updated:
                        self.save_all_configs()
                    
                    print(f"[配置管理] 从 {self.config_file} 加载配置成功")
                    print(f"  - API配置: {len(self._all_configs.get('api_configs', []))} 个")
                    print(f"  - Codex配置: {len(self._all_configs.get('codex_configs', []))} 个")
                    openai_cfgs = self._all_configs.get('openai_to_claude_configs', [])
                    print(f"  - OpenAI转Claude配置: {len(openai_cfgs)} 个")
                    print(f"  - 超时重试配置: {len(self._all_configs.get('retry_configs', []))} 个")
                    print(f"  - 模型转换配置: {len(self._all_configs.get('model_conversions', []))} 个")
                except Exception as e:
                    print(f"[配置管理] 加载配置文件失败: {e}")
                    self._all_configs = self._get_default_all_configs()
            else:
                print(f"[配置管理] 配置文件不存在，使用默认配置")
                self._all_configs = self._get_default_all_configs()
                self.save_all_configs()
            return self._all_configs.copy()
    
    def reload_all_configs(self) -> bool:
        """重新加载配置文件（用于手动修改配置后同步）"""
        with self.lock:
            try:
                old_configs = copy.deepcopy(self._all_configs)
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    new_configs = json.load(f)
                if old_configs != new_configs:
                    self._all_configs = new_configs
                    print(f"[配置管理] 配置已重新加载")
                else:
                    self._all_configs = new_configs
                return True
            except Exception as e:
                print(f"[配置管理] 重新加载配置失败: {e}")
                return False
    
    def save_all_configs(self) -> bool:
        """保存所有配置"""
        with self.lock:
            try:
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    json.dump(self._all_configs, f, ensure_ascii=False, indent=2)
                print(f"[配置管理] 配置已保存到 {self.config_file}")
                return True
            except Exception as e:
                print(f"[配置管理] 保存配置失败: {e}")
                return False
    
    def _get_default_all_configs(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            "api_configs": [
                {
                    "base_url": "https://anyrouter.top",
                    "key": "sk-WOvSO2O1cp1JO00lxun4qL3v4um1KBWSNpcCuagkaffwkJ31",
                    "type": "primary",
                    "name": "周一KEY",
                    "enabled": True,
                    "time_enabled": [1, 0, 0, 0, 0, 0, 0],
                    "activation_enabled": False,
                    "activation_time": "08:00"
                },
                {
                    "base_url": "https://anyrouter.top",
                    "key": "sk-o4x46gphGLYNIFlmbZrEVyS7PnRP1umSDKllZ1zhqCSSWG1Q",
                    "type": "primary",
                    "name": "周二KEY",
                    "enabled": True,
                    "time_enabled": [0, 1, 0, 0, 0, 0, 0]
                },
                {
                    "base_url": "https://anyrouter.top",
                    "key": "sk-7tzk3enmficfIq1LfY0Ebrk1AXWUqKBaCXKIy5RAK0joW9UK",
                    "type": "primary",
                    "name": "周三KEY",
                    "enabled": True,
                    "time_enabled": [0, 0, 1, 0, 0, 0, 0]
                },
                {
                    "base_url": "https://anyrouter.top",
                    "key": "sk-0yK6PVdbGx160Nz2H7mKgBukJM7Xhb4mdBUYSsL0MiHLMNnG",
                    "type": "primary",
                    "name": "周四KEY",
                    "enabled": True,
                    "time_enabled": [0, 0, 0, 1, 0, 0, 0]
                },
                {
                    "base_url": "https://anyrouter.top",
                    "key": "sk-EdmtgS9pizpqq9yXOFlgNoBLSJ1RznOIDHUSxWYaKE98Wsca",
                    "type": "primary",
                    "name": "周五KEY",
                    "enabled": True,
                    "time_enabled": [0, 0, 0, 0, 1, 0, 0]
                },
                {
                    "base_url": "https://anyrouter.top",
                    "key": "sk-ZmQ4AOQfFUxNNDyNZ9xrDmcRlhCHr2VGS0BZA6IZxt5a0Mv5",
                    "type": "primary",
                    "name": "周六KEY",
                    "enabled": True,
                    "time_enabled": [0, 0, 0, 0, 0, 1, 0]
                },
                {
                    "base_url": "https://anyrouter.top",
                    "key": "sk-JOlfdrh8rgTCVIonLHtixj2qVOefNG9j7vEQp4pbBt21biDm",
                    "type": "primary",
                    "name": "周日KEY",
                    "enabled": True,
                    "time_enabled": [0, 0, 0, 0, 0, 0, 1]
                },
                {
                    "base_url": "https://19880321.xyz/code",
                    "key": "sk-N47CWo7igg21V1dAuvb4je7hG5h0F8SzvJ0brQhrt7JP2t7O",
                    "type": "backup",
                    "name": "备用API",
                    "enabled": True,
                    "time_enabled": [1, 1, 1, 1, 1, 1, 1]
                },
                {
                    "base_url": "http://47.181.223.190:3688/api",
                    "key": "cr_d7d91143104f9ea62ad1c9d770b61209e3e2498c5b9b6be3bc2f6f321dfce6d9",
                    "type": "backup",
                    "name": "拼车",
                    "enabled": True,
                    "time_enabled": [1, 1, 1, 1, 1, 1, 1]
                }
            ],
            "codex_configs": [
                {
                    "base_url": "https://fizzlycode.com/openai",
                    "key": "cr_2e941f6ad99a0a76a554e492a4ae9bdbb5d877c91507dedbcd894615f79f97be",
                    "type": "primary",
                    "name": "Codex主KEY",
                    "enabled": True,
                    "time_enabled": [1, 1, 1, 1, 1, 1, 1]
                }
            ],
            "openai_to_claude_configs": [
                {
                    "base_url": "http://47.181.223.190:3688/api",
                    "key": "cr_d7d91143104f9ea62ad1c9d770b61209e3e2498c5b9b6be3bc2f6f321dfce6d9",
                    "type": "openai_to_claude",
                    "name": "OpenAI转Claude专用",
                    "enabled": True
                }
            ],
            "retry_configs": [
                {
                    "base_url": "https://19880321.xyz/code",
                    "key": "sk-N47CWo7igg21V1dAuvb4je7hG5h0F8SzvJ0brQhrt7JP2t7O",
                    "name": "第1次-125刀",
                    "enabled": True
                },
                {
                    "base_url": "http://47.181.223.190:3688/api",
                    "key": "cr_d7d91143104f9ea62ad1c9d770b61209e3e2498c5b9b6be3bc2f6f321dfce6d9",
                    "name": "第2次-拼车",
                    "enabled": True
                },
                {
                    "base_url": "https://anyrouter.top",
                    "key": "sk-JOlfdrh8rgTCVIonLHtixj2qVOefNG9j7vEQp4pbBt21biDm",
                    "name": "第3次-anyrouter",
                    "enabled": True
                },
                {
                    "base_url": "https://19880321.xyz/code",
                    "key": "sk-N47CWo7igg21V1dAuvb4je7hG5h0F8SzvJ0brQhrt7JP2t7O",
                    "name": "第4次-125刀",
                    "enabled": True
                },
                {
                    "base_url": "https://19880321.xyz/code",
                    "key": "sk-N47CWo7igg21V1dAuvb4je7hG5h0F8SzvJ0brQhrt7JP2t7O",
                    "name": "第5次-125刀",
                    "enabled": True
                }
            ],
            "model_conversions": [
                {
                    "name": "Haiku转Sonnet 4",
                    "source_model": "claude-3-5-haiku-20241022",
                    "target_model": "claude-sonnet-4-5-20250929",
                    "conversion_type": "full_format",
                    "enabled": True
                },
                {
                    "name": "旧Sonnet 4转新版",
                    "source_model": "claude-sonnet-4-20250514",
                    "target_model": "claude-sonnet-4-5-20250929",
                    "conversion_type": "simple_rename",
                    "enabled": True
                }
            ],
            "timeout_settings": {
                "connect_timeout": 60.0,
                "write_timeout": 60.0,
                "pool_timeout": 120.0,
                "streaming_read_timeout": 60.0,
                "non_streaming_read_timeout": 60.0,
                "retry_read_timeout": 60.0,
                "extended_connect_timeout": 90.0,
                "api_cooldown_seconds": 600,
                "api_error_threshold": 3,
                "codex_error_threshold": 3,
                "codex_base_timeout": 60,
                "codex_timeout_increment": 60,
                "codex_connect_timeout": 30.0,
                "primary_api_check_interval": 30,
                "billing_cycle_delay": 60,
                "health_check_interval": 0.5,
                "billing_send_interval": 1.0,
                "stream_retry_wait": 1.0,
                "strategy_retry_read_timeout": 200.0,
                "max_retries": 4
            },
            "error_handling_strategies": {
                "http_status_codes": {
                    "400": "strategy_retry",
                    "404": "strategy_retry",
                    "429": "strategy_retry",
                    "500": "strategy_retry",
                    "502": "strategy_retry",
                    "503": "strategy_retry",
                    "520": "strategy_retry",
                    "521": "strategy_retry",
                    "522": "strategy_retry",
                    "524": "strategy_retry",
                    "401": "switch_api",
                    "403": "switch_api"
                },
                "network_errors": {
                    "ReadError": "switch_api",
                    "ConnectError": "switch_api",
                    "ReadTimeout": "strategy_retry"
                }
            },
            "optimization_settings": {
                "enable_cache_control_limit": True
            }
        }
    
    # ========== API配置管理 ==========
    def get_api_configs(self) -> List[Dict[str, Any]]:
        """获取所有API配置"""
        with self.lock:
            return self._all_configs.get("api_configs", []).copy()
    
    def get_enabled_api_configs(self) -> List[Dict[str, Any]]:
        """获取已启用的API配置"""
        with self.lock:
            return [cfg for cfg in self._all_configs.get("api_configs", []) if cfg.get("enabled", True)]
    
    def add_api_config(self, config: Dict[str, Any]) -> bool:
        """添加API配置"""
        with self.lock:
            # 验证必填字段
            base_url = config.get("base_url", "").strip()
            key = config.get("key", "").strip()
            
            if not base_url or not key:
                return False
            
            # 验证URL格式
            if not base_url.startswith(("http://", "https://")):
                return False
            
            # 验证Key长度（至少10个字符）
            if len(key) < 10:
                return False
            
            configs = self._all_configs.setdefault("api_configs", [])
            config.setdefault("name", f"API-{len(configs) + 1}")
            config.setdefault("type", "primary")
            config.setdefault("enabled", True)
            config.setdefault("time_enabled", [1, 1, 1, 1, 1, 1, 1])  # 默认周一至周日全部启用
            config.setdefault("activation_enabled", False)  # 默认不启用定时激活
            config.setdefault("activation_time", "08:00")  # 默认激活时间为上午8点
            config.setdefault("created_at", datetime.now().isoformat())
            
            configs.append(config)
            self.save_all_configs()
            return True
    
    def update_api_config(self, index: int, config: Dict[str, Any]) -> bool:
        """更新API配置"""
        with self.lock:
            configs = self._all_configs.get("api_configs", [])
            if 0 <= index < len(configs):
                config.setdefault("updated_at", datetime.now().isoformat())
                configs[index].update(config)
                self.save_all_configs()
                return True
            return False
    
    def delete_api_config(self, index: int) -> bool:
        """删除API配置"""
        with self.lock:
            configs = self._all_configs.get("api_configs", [])
            if 0 <= index < len(configs):
                configs.pop(index)
                self.save_all_configs()
                return True
            return False
    
    def toggle_api_config(self, index: int) -> Optional[bool]:
        """切换API配置启用状态"""
        with self.lock:
            configs = self._all_configs.get("api_configs", [])
            if 0 <= index < len(configs):
                configs[index]["enabled"] = not configs[index].get("enabled", True)
                self.save_all_configs()
                return configs[index]["enabled"]
            return None
    
    def move_api_config(self, index: int, direction: str) -> bool:
        """移动API配置"""
        with self.lock:
            configs = self._all_configs.get("api_configs", [])
            if not configs or not (0 <= index < len(configs)):
                return False
            direction = (direction or "").lower()
            if direction == "up" and index > 0:
                configs[index], configs[index - 1] = configs[index - 1], configs[index]
                self.save_all_configs()
                return True
            if direction == "down" and index < len(configs) - 1:
                configs[index], configs[index + 1] = configs[index + 1], configs[index]
                self.save_all_configs()
                return True
            if direction == "top" and index > 0:
                item = configs.pop(index)
                configs.insert(0, item)
                self.save_all_configs()
                return True
            if direction == "bottom" and index < len(configs) - 1:
                item = configs.pop(index)
                configs.append(item)
                self.save_all_configs()
                return True
            return False


    def duplicate_api_config(self, index: int) -> bool:
        """复制一条API配置"""
        with self.lock:
            configs = self._all_configs.get("api_configs", [])
            if 0 <= index < len(configs):
                new_config = copy.deepcopy(configs[index])
                new_config.pop("updated_at", None)
                new_config["created_at"] = datetime.now().isoformat()
                original_name = new_config.get("name", f"API-{index + 1}")
                new_config["name"] = f"{original_name}(复制)"
                configs.insert(index + 1, new_config)
                self.save_all_configs()
                return True
            return False
    
    # ========== Codex配置管理 ==========
    def get_codex_configs(self) -> List[Dict[str, Any]]:
        """获取所有Codex配置"""
        with self.lock:
            return self._all_configs.get("codex_configs", []).copy()
    
    def get_enabled_codex_configs(self) -> List[Dict[str, Any]]:
        """获取已启用的Codex配置"""
        with self.lock:
            return [cfg for cfg in self._all_configs.get("codex_configs", []) if cfg.get("enabled", True)]
    
    def get_codex_config(self) -> Dict[str, Any]:
        """获取Codex配置（向后兼容，返回第一个启用的配置）"""
        configs = self.get_enabled_codex_configs()
        return configs[0] if configs else {}
    
    def add_codex_config(self, config: Dict[str, Any]) -> bool:
        """添加Codex配置"""
        with self.lock:
            # 验证必填字段
            base_url = config.get("base_url", "").strip()
            key = config.get("key", "").strip()
            
            if not base_url or not key:
                return False
            
            # 验证URL格式
            if not base_url.startswith(("http://", "https://")):
                return False
            
            # 验证Key长度（至少10个字符）
            if len(key) < 10:
                return False
            
            configs = self._all_configs.setdefault("codex_configs", [])
            config.setdefault("name", f"Codex-{len(configs) + 1}")
            config.setdefault("type", "primary")
            config.setdefault("enabled", True)
            config.setdefault("time_enabled", [1, 1, 1, 1, 1, 1, 1])
            config.setdefault("created_at", datetime.now().isoformat())
            
            configs.append(config)
            self.save_all_configs()
            return True
    
    def update_codex_config(self, index: int, config: Dict[str, Any]) -> bool:
        """更新Codex配置"""
        with self.lock:
            configs = self._all_configs.get("codex_configs", [])
            if 0 <= index < len(configs):
                config.setdefault("updated_at", datetime.now().isoformat())
                configs[index].update(config)
                self.save_all_configs()
                return True
            return False
    
    def delete_codex_config(self, index: int) -> bool:
        """删除Codex配置"""
        with self.lock:
            configs = self._all_configs.get("codex_configs", [])
            if 0 <= index < len(configs):
                configs.pop(index)
                self.save_all_configs()
                return True
            return False
    
    def toggle_codex_config(self, index: int) -> bool:
        """切换Codex配置的启用状态"""
        with self.lock:
            configs = self._all_configs.get("codex_configs", [])
            if 0 <= index < len(configs):
                configs[index]["enabled"] = not configs[index].get("enabled", True)
                configs[index]["updated_at"] = datetime.now().isoformat()
                self.save_all_configs()
                return configs[index]["enabled"]
            return None
    
    def move_codex_config(self, index: int, direction: str) -> bool:
        """移动Codex配置的顺序"""
        with self.lock:
            configs = self._all_configs.get("codex_configs", [])
            if not configs or not (0 <= index < len(configs)):
                return False
            direction = (direction or "").lower()
            if direction == "up" and index > 0:
                configs[index], configs[index - 1] = configs[index - 1], configs[index]
                self.save_all_configs()
                return True
            if direction == "down" and index < len(configs) - 1:
                configs[index], configs[index + 1] = configs[index + 1], configs[index]
                self.save_all_configs()
                return True
            if direction == "top" and index > 0:
                item = configs.pop(index)
                configs.insert(0, item)
                self.save_all_configs()
                return True
            if direction == "bottom" and index < len(configs) - 1:
                item = configs.pop(index)
                configs.append(item)
                self.save_all_configs()
                return True
            return False


    def duplicate_codex_config(self, index: int) -> bool:
        """复制一条Codex配置"""
        with self.lock:
            configs = self._all_configs.get("codex_configs", [])
            if 0 <= index < len(configs):
                new_config = copy.deepcopy(configs[index])
                new_config.pop("updated_at", None)
                new_config["created_at"] = datetime.now().isoformat()
                original_name = new_config.get("name", f"Codex-{index + 1}")
                new_config["name"] = f"{original_name}(复制)"
                configs.insert(index + 1, new_config)
                self.save_all_configs()
                return True
            return False
    
    # ========== OpenAI转Claude配置管理 ==========
    def get_openai_to_claude_configs(self) -> List[Dict[str, Any]]:
        """获取所有OpenAI转Claude配置"""
        with self.lock:
            return [cfg.copy() for cfg in self._all_configs.get("openai_to_claude_configs", [])]

    def get_enabled_openai_to_claude_configs(self) -> List[Dict[str, Any]]:
        """获取已启用的OpenAI转Claude配置"""
        with self.lock:
            return [cfg.copy() for cfg in self._all_configs.get("openai_to_claude_configs", []) if cfg.get("enabled", True)]

    def get_openai_to_claude_config(self) -> Dict[str, Any]:
        """获取首选的OpenAI转Claude配置（向后兼容）"""
        with self.lock:
            configs = self._all_configs.get("openai_to_claude_configs", [])
            for cfg in configs:
                if cfg.get("enabled", True):
                    return cfg.copy()
            return configs[0].copy() if configs else {}

    def add_openai_to_claude_config(self, config: Dict[str, Any]) -> bool:
        """添加OpenAI转Claude配置"""
        with self.lock:
            # 验证必填字段
            base_url = config.get("base_url", "").strip()
            key = config.get("key", "").strip()
            
            if not base_url or not key:
                return False
            
            # 验证URL格式
            if not base_url.startswith(("http://", "https://")):
                return False
            
            # 验证Key长度（至少10个字符）
            if len(key) < 10:
                return False

            configs = self._all_configs.setdefault("openai_to_claude_configs", [])
            config.setdefault("name", f"OpenAI转Claude-{len(configs) + 1}")
            config.setdefault("type", "openai_to_claude")
            config.setdefault("enabled", True)
            config.setdefault("created_at", datetime.now().isoformat())

            configs.append(config)
            self.save_all_configs()
            return True

    def update_openai_to_claude_config(self, index: int, config: Dict[str, Any]) -> bool:
        """更新OpenAI转Claude配置"""
        with self.lock:
            configs = self._all_configs.get("openai_to_claude_configs", [])
            if 0 <= index < len(configs):
                config.setdefault("updated_at", datetime.now().isoformat())
                configs[index].update(config)
                self.save_all_configs()
                return True
            return False

    def delete_openai_to_claude_config(self, index: int) -> bool:
        """删除OpenAI转Claude配置"""
        with self.lock:
            configs = self._all_configs.get("openai_to_claude_configs", [])
            if 0 <= index < len(configs):
                configs.pop(index)
                self.save_all_configs()
                return True
            return False

    def toggle_openai_to_claude_config(self, index: int) -> Optional[bool]:
        """切换OpenAI转Claude配置启用状态"""
        with self.lock:
            configs = self._all_configs.get("openai_to_claude_configs", [])
            if 0 <= index < len(configs):
                configs[index]["enabled"] = not configs[index].get("enabled", True)
                configs[index]["updated_at"] = datetime.now().isoformat()
                self.save_all_configs()
                return configs[index]["enabled"]
            return None

    def move_openai_to_claude_config(self, index: int, direction: str) -> bool:
        """移动OpenAI转Claude配置顺序"""
        with self.lock:
            configs = self._all_configs.get("openai_to_claude_configs", [])
            if not configs or not (0 <= index < len(configs)):
                return False
            direction = (direction or "").lower()
            if direction == "up" and index > 0:
                configs[index], configs[index - 1] = configs[index - 1], configs[index]
                self.save_all_configs()
                return True
            if direction == "down" and index < len(configs) - 1:
                configs[index], configs[index + 1] = configs[index + 1], configs[index]
                self.save_all_configs()
                return True
            if direction == "top" and index > 0:
                item = configs.pop(index)
                configs.insert(0, item)
                self.save_all_configs()
                return True
            if direction == "bottom" and index < len(configs) - 1:
                item = configs.pop(index)
                configs.append(item)
                self.save_all_configs()
                return True
            return False


    def duplicate_openai_to_claude_config(self, index: int) -> bool:
        """复制一条OpenAI转Claude配置"""
        with self.lock:
            configs = self._all_configs.get("openai_to_claude_configs", [])
            if 0 <= index < len(configs):
                new_config = copy.deepcopy(configs[index])
                new_config.pop("updated_at", None)
                new_config["created_at"] = datetime.now().isoformat()
                original_name = new_config.get("name", f"OpenAI-{index + 1}")
                new_config["name"] = f"{original_name}(复制)"
                configs.insert(index + 1, new_config)
                self.save_all_configs()
                return True
            return False
    
    # ========== 超时重试配置管理 ==========
    def get_retry_configs(self) -> List[Dict[str, Any]]:
        """获取所有超时重试配置"""
        with self.lock:
            return self._all_configs.get("retry_configs", []).copy()
    
    def get_enabled_retry_configs(self) -> List[Dict[str, Any]]:
        """获取已启用的超时重试配置"""
        with self.lock:
            return [cfg for cfg in self._all_configs.get("retry_configs", []) if cfg.get("enabled", True)]
    
    def add_retry_config(self, config: Dict[str, Any]) -> bool:
        """添加超时重试配置"""
        with self.lock:
            # 验证必填字段
            base_url = config.get("base_url", "").strip()
            key = config.get("key", "").strip()
            
            if not base_url or not key:
                return False
            
            # 验证URL格式
            if not base_url.startswith(("http://", "https://")):
                return False
            
            # 验证Key长度（至少10个字符）
            if len(key) < 10:
                return False
            
            configs = self._all_configs.setdefault("retry_configs", [])
            config.setdefault("name", f"第{len(configs) + 1}次重试")
            config.setdefault("enabled", True)
            config.setdefault("created_at", datetime.now().isoformat())
            
            configs.append(config)
            self.save_all_configs()
            return True
    
    def update_retry_config(self, index: int, config: Dict[str, Any]) -> bool:
        """更新超时重试配置"""
        with self.lock:
            configs = self._all_configs.get("retry_configs", [])
            if 0 <= index < len(configs):
                config.setdefault("updated_at", datetime.now().isoformat())
                configs[index].update(config)
                self.save_all_configs()
                return True
            return False
    
    def delete_retry_config(self, index: int) -> bool:
        """删除超时重试配置"""
        with self.lock:
            configs = self._all_configs.get("retry_configs", [])
            if 0 <= index < len(configs):
                configs.pop(index)
                self.save_all_configs()
                return True
            return False
    
    def toggle_retry_config(self, index: int) -> Optional[bool]:
        """切换超时重试配置启用状态"""
        with self.lock:
            configs = self._all_configs.get("retry_configs", [])
            if 0 <= index < len(configs):
                configs[index]["enabled"] = not configs[index].get("enabled", True)
                self.save_all_configs()
                return configs[index]["enabled"]
            return None
    
    def move_retry_config(self, index: int, direction: str) -> bool:
        """移动超时重试配置"""
        with self.lock:
            configs = self._all_configs.get("retry_configs", [])
            if not configs or not (0 <= index < len(configs)):
                return False
            direction = (direction or "").lower()
            if direction == "up" and index > 0:
                configs[index], configs[index - 1] = configs[index - 1], configs[index]
                self.save_all_configs()
                return True
            if direction == "down" and index < len(configs) - 1:
                configs[index], configs[index + 1] = configs[index + 1], configs[index]
                self.save_all_configs()
                return True
            if direction == "top" and index > 0:
                item = configs.pop(index)
                configs.insert(0, item)
                self.save_all_configs()
                return True
            if direction == "bottom" and index < len(configs) - 1:
                item = configs.pop(index)
                configs.append(item)
                self.save_all_configs()
                return True
            return False

    def duplicate_retry_config(self, index: int) -> bool:
        """复制一条超时重试配置"""
        with self.lock:
            configs = self._all_configs.get("retry_configs", [])
            if 0 <= index < len(configs):
                new_config = copy.deepcopy(configs[index])
                new_config.pop("updated_at", None)
                new_config["created_at"] = datetime.now().isoformat()
                configs.insert(index + 1, new_config)
                self.save_all_configs()
                return True
            return False

    # ========== 模型转换配置管理 ==========
    def get_model_conversions(self) -> List[Dict[str, Any]]:
        """获取所有模型转换配置"""
        with self.lock:
            return self._all_configs.get("model_conversions", []).copy()
    
    def get_enabled_model_conversions(self) -> List[Dict[str, Any]]:
        """获取已启用的模型转换配置"""
        with self.lock:
            return [cfg for cfg in self._all_configs.get("model_conversions", []) if cfg.get("enabled", True)]
    
    def add_model_conversion(self, config: Dict[str, Any]) -> bool:
        """添加模型转换配置"""
        with self.lock:
            if not config.get("source_model") or not config.get("target_model"):
                return False
            
            configs = self._all_configs.setdefault("model_conversions", [])
            config.setdefault("name", f"模型转换{len(configs) + 1}")
            config.setdefault("enabled", True)
            config.setdefault("created_at", datetime.now().isoformat())
            configs.append(config)
            self.save_all_configs()
            return True
    
    def update_model_conversion(self, index: int, config: Dict[str, Any]) -> bool:
        """更新模型转换配置"""
        with self.lock:
            configs = self._all_configs.get("model_conversions", [])
            if 0 <= index < len(configs):
                config.setdefault("updated_at", datetime.now().isoformat())
                configs[index].update(config)
                self.save_all_configs()
                return True
            return False
    
    def delete_model_conversion(self, index: int) -> bool:
        """删除模型转换配置"""
        with self.lock:
            configs = self._all_configs.get("model_conversions", [])
            if 0 <= index < len(configs):
                configs.pop(index)
                self.save_all_configs()
                return True
            return False
    
    def toggle_model_conversion(self, index: int) -> Optional[bool]:
        """切换模型转换配置启用状态"""
        with self.lock:
            configs = self._all_configs.get("model_conversions", [])
            if 0 <= index < len(configs):
                configs[index]["enabled"] = not configs[index].get("enabled", True)
                self.save_all_configs()
                return configs[index]["enabled"]
            return None
    
    def move_model_conversion(self, index: int, direction: str) -> bool:
        """移动模型转换配置"""
        with self.lock:
            configs = self._all_configs.get("model_conversions", [])
            if not configs or not (0 <= index < len(configs)):
                return False
            direction = (direction or "").lower()
            if direction == "up" and index > 0:
                configs[index], configs[index - 1] = configs[index - 1], configs[index]
                self.save_all_configs()
                return True
            if direction == "down" and index < len(configs) - 1:
                configs[index], configs[index + 1] = configs[index + 1], configs[index]
                self.save_all_configs()
                return True
            if direction == "top" and index > 0:
                item = configs.pop(index)
                configs.insert(0, item)
                self.save_all_configs()
                return True
            if direction == "bottom" and index < len(configs) - 1:
                item = configs.pop(index)
                configs.append(item)
                self.save_all_configs()
                return True
            return False


    def duplicate_model_conversion(self, index: int) -> bool:
        """复制一条模型转换配置"""
        with self.lock:
            configs = self._all_configs.get("model_conversions", [])
            if 0 <= index < len(configs):
                new_config = copy.deepcopy(configs[index])
                new_config.pop("updated_at", None)
                new_config["created_at"] = datetime.now().isoformat()
                original_name = new_config.get("name", f"模型转换{index + 1}")
                new_config["name"] = f"{original_name}(复制)"
                configs.insert(index + 1, new_config)
                self.save_all_configs()
                return True
            return False
    
    # ========== 超时设置管理 ==========
    def get_timeout_settings(self) -> Dict[str, Any]:
        """获取超时设置"""
        with self.lock:
            default_settings = {
                "connect_timeout": 60.0,
                "write_timeout": 60.0,
                "pool_timeout": 120.0,
                "streaming_read_timeout": 60.0,
                "non_streaming_read_timeout": 60.0,
                "extended_connect_timeout": 90.0,
                "api_cooldown_seconds": 600,
                "api_error_threshold": 3,
                "codex_error_threshold": 3,
                "codex_base_timeout": 60,
                "codex_timeout_increment": 60,
                "codex_connect_timeout": 30.0,
                "primary_api_check_interval": 30,
                "billing_cycle_delay": 60,
                "health_check_interval": 0.5,
                "billing_send_interval": 1.0,
                "stream_retry_wait": 1.0,
                "strategy_retry_read_timeout": 200.0,
                "modify_retry_headers": True
            }
            stored_settings = self._all_configs.get("timeout_settings", {})
            merged_settings = default_settings.copy()
            if isinstance(stored_settings, dict):
                merged_settings.update(stored_settings)
            return merged_settings
    
    def update_timeout_settings(self, settings: Dict[str, Any]) -> bool:
        """更新超时设置（带输入校验）"""
        with self.lock:
            # 校验必需字段
            required_fields = [
                "connect_timeout", "write_timeout", "pool_timeout",
                "streaming_read_timeout", "non_streaming_read_timeout",
                "extended_connect_timeout",
                "api_cooldown_seconds", "api_error_threshold",
                "codex_base_timeout", "codex_error_threshold",
                "codex_timeout_increment", "codex_connect_timeout",
                "primary_api_check_interval",
                "billing_cycle_delay", "health_check_interval",
                "billing_send_interval", "stream_retry_wait",
                "strategy_retry_read_timeout", "modify_retry_headers"
            ]
            
            # 类型和范围校验
            for field in required_fields:
                if field not in settings:
                    return False
                value = settings[field]
                # modify_retry_headers 是布尔值，单独处理
                if field == "modify_retry_headers":
                    if not isinstance(value, bool):
                        return False
                    continue
                # 检查类型
                if not isinstance(value, (int, float)):
                    return False
                # 检查范围（必须大于0）
                if value <= 0:
                    return False
            
            settings.setdefault("updated_at", datetime.now().isoformat())
            self._all_configs["timeout_settings"] = settings
            self.save_all_configs()
            return True

    # ========== 错误处理策略管理 ==========
    def get_error_handling_strategies(self) -> Dict[str, Any]:
        """获取错误处理策略配置"""
        with self.lock:
            default_strategies = {
                "http_status_codes": {
                    "400": "strategy_retry",
                    "404": "strategy_retry",
                    "408": "strategy_retry",  # Request Timeout
                    "429": "strategy_retry",
                    "500": "strategy_retry",
                    "502": "strategy_retry",
                    "503": "strategy_retry",
                    "504": "strategy_retry",  # Gateway Timeout
                    "520": "strategy_retry",
                    "521": "strategy_retry",
                    "522": "strategy_retry",
                    "524": "strategy_retry",
                    "401": "switch_api",
                    "403": "switch_api",
                    "default": "strategy_retry"  # 默认策略：未列出的错误码使用策略重试
                },
                "network_errors": {
                    "ReadError": "switch_api",
                    "ConnectError": "switch_api",
                    "ReadTimeout": "strategy_retry",
                    "default": "switch_api"  # 默认策略：未列出的网络错误切换API
                }
            }
            stored_strategies = self._all_configs.get("error_handling_strategies", {})
            
            # 合并默认配置和存储配置
            result = default_strategies.copy()
            if "http_status_codes" in stored_strategies:
                result["http_status_codes"].update(stored_strategies["http_status_codes"])
            if "network_errors" in stored_strategies:
                result["network_errors"].update(stored_strategies["network_errors"])
            
            return result
    
    def update_error_handling_strategies(self, strategies: Dict[str, Any]) -> bool:
        """更新错误处理策略"""
        with self.lock:
            try:
                # 校验策略有效性
                valid_strategies = {"strategy_retry", "switch_api", "normal_retry"}
                
                if "http_status_codes" in strategies:
                    for code, strategy in strategies["http_status_codes"].items():
                        if strategy not in valid_strategies:
                            return False
                
                if "network_errors" in strategies:
                    for error_type, strategy in strategies["network_errors"].items():
                        if strategy not in valid_strategies:
                            return False
                
                self._all_configs["error_handling_strategies"] = strategies
                self.save_all_configs()
                return True
            except Exception as e:
                print(f"[配置管理] 更新错误处理策略失败: {e}")
                return False

    # ========== 优化设置管理 ==========
    def get_optimization_settings(self) -> Dict[str, Any]:
        """获取优化设置"""
        with self.lock:
            default_settings = {
                "enable_cache_control_limit": True
            }
            stored_settings = self._all_configs.get("optimization_settings", {})
            merged_settings = default_settings.copy()
            if isinstance(stored_settings, dict):
                merged_settings.update(stored_settings)
            return merged_settings

    def update_optimization_settings(self, settings: Dict[str, Any]) -> bool:
        """更新优化设置"""
        with self.lock:
            try:
                # 校验字段
                if "enable_cache_control_limit" in settings:
                    if not isinstance(settings["enable_cache_control_limit"], bool):
                        return False

                settings.setdefault("updated_at", datetime.now().isoformat())
                self._all_configs["optimization_settings"] = settings
                self.save_all_configs()
                return True
            except Exception as e:
                print(f"[配置管理] 更新优化设置失败: {e}")
                return False


# 全局单例
_config_manager = None

def get_config_manager() -> ConfigManager:
    """获取配置管理器单例"""
    global _config_manager
    if _config_manager is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_file = os.path.join(script_dir, "json_data", "all_configs.json")
        _config_manager = ConfigManager(config_file)
    return _config_manager

