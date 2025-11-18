"""
Claude API Token使用量统计分析器
解析日志文件，提供详细的token使用统计
"""

import os
import json
import re
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Any
import glob
import threading
import tempfile

class TokenStatsAnalyzer:
    """Token统计分析器"""

    def __init__(self, log_dir: str = "logs"):
        """初始化分析器

        Args:
            log_dir: 日志文件目录
        """
        self.log_dir = log_dir
        self.stats_data = []  # 存储所有解析的统计数据

    def parse_log_files(self):
        """解析所有log文件"""
        print(f"开始解析日志文件目录: {self.log_dir}")

        # 获取所有.log文件
        log_files = glob.glob(os.path.join(self.log_dir, "*.log"))
        print(f"找到 {len(log_files)} 个日志文件")

        for log_file in log_files:
            print(f"正在解析: {log_file}")
            self._parse_single_file(log_file)

        print(f"解析完成！共提取 {len(self.stats_data)} 条记录")

    def _parse_single_file(self, file_path: str):
        """解析单个日志文件

        Args:
            file_path: 日志文件路径
        """
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                current_timestamp = None

                for line in f:
                    # 提取时间戳
                    timestamp_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                    if timestamp_match:
                        current_timestamp = timestamp_match.group(1)

                    # 查找包含usage信息的行
                    if '"usage":' in line and '"message_start"' in line:
                        # 提取usage JSON数据
                        try:
                            # 查找data:后的JSON
                            data_match = re.search(r'data:\s*(\{.+\})', line)
                            if data_match:
                                data_json = json.loads(data_match.group(1))

                                # 提取message中的usage
                                if 'message' in data_json:
                                    message = data_json['message']
                                    usage = message.get('usage', {})
                                    model = message.get('model', 'unknown')

                                    # 保存统计数据
                                    self.stats_data.append({
                                        'timestamp': current_timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                        'model': model,
                                        'input_tokens': usage.get('input_tokens', 0),
                                        'cache_creation_input_tokens': usage.get('cache_creation_input_tokens', 0),
                                        'cache_read_input_tokens': usage.get('cache_read_input_tokens', 0),
                                        'output_tokens': usage.get('output_tokens', 0),
                                        'service_tier': usage.get('service_tier', 'standard')
                                    })
                        except json.JSONDecodeError:
                            continue

                    # 也处理message_delta中的final usage
                    elif '"usage":' in line and '"message_delta"' in line:
                        try:
                            data_match = re.search(r'data:\s*(\{.+\})', line)
                            if data_match:
                                data_json = json.loads(data_match.group(1))
                                usage = data_json.get('usage', {})

                                # 更新最后一条记录的output_tokens
                                if self.stats_data and usage.get('output_tokens'):
                                    # 只更新output_tokens（这是最终值）
                                    self.stats_data[-1]['output_tokens'] = usage.get('output_tokens', 0)
                        except json.JSONDecodeError:
                            continue

        except Exception as e:
            print(f"解析文件 {file_path} 时出错: {e}")

    def get_stats_by_model(self) -> Dict[str, Any]:
        """按模型统计token使用量"""
        model_stats = defaultdict(lambda: {
            'total_input_tokens': 0,
            'total_cache_creation_tokens': 0,
            'total_cache_read_tokens': 0,
            'total_output_tokens': 0,
            'total_requests': 0,
            'total_tokens': 0  # 总计所有token
        })

        for record in self.stats_data:
            model = record['model']
            model_stats[model]['total_input_tokens'] += record['input_tokens']
            model_stats[model]['total_cache_creation_tokens'] += record['cache_creation_input_tokens']
            model_stats[model]['total_cache_read_tokens'] += record['cache_read_input_tokens']
            model_stats[model]['total_output_tokens'] += record['output_tokens']
            model_stats[model]['total_requests'] += 1

            # 计算总token数
            total = (record['input_tokens'] +
                    record['cache_creation_input_tokens'] +
                    record['cache_read_input_tokens'] +
                    record['output_tokens'])
            model_stats[model]['total_tokens'] += total

        return dict(model_stats)

    def get_stats_by_date(self, period: str = 'daily') -> Dict[str, Any]:
        """按日期统计token使用量

        Args:
            period: 统计周期 ('daily', 'weekly', 'monthly')
        """
        date_stats = defaultdict(lambda: {
            'total_input_tokens': 0,
            'total_cache_creation_tokens': 0,
            'total_cache_read_tokens': 0,
            'total_output_tokens': 0,
            'total_requests': 0,
            'total_tokens': 0,
            'models': defaultdict(int)  # 各模型的使用次数
        })

        for record in self.stats_data:
            try:
                timestamp = datetime.strptime(record['timestamp'], '%Y-%m-%d %H:%M:%S')

                # 根据period确定日期key
                if period == 'daily':
                    date_key = timestamp.strftime('%Y-%m-%d')
                elif period == 'weekly':
                    # 周一作为一周的开始
                    week_start = timestamp - timedelta(days=timestamp.weekday())
                    date_key = week_start.strftime('%Y-W%V')
                elif period == 'monthly':
                    date_key = timestamp.strftime('%Y-%m')
                else:
                    date_key = timestamp.strftime('%Y-%m-%d')

                # 累加统计
                date_stats[date_key]['total_input_tokens'] += record['input_tokens']
                date_stats[date_key]['total_cache_creation_tokens'] += record['cache_creation_input_tokens']
                date_stats[date_key]['total_cache_read_tokens'] += record['cache_read_input_tokens']
                date_stats[date_key]['total_output_tokens'] += record['output_tokens']
                date_stats[date_key]['total_requests'] += 1

                total = (record['input_tokens'] +
                        record['cache_creation_input_tokens'] +
                        record['cache_read_input_tokens'] +
                        record['output_tokens'])
                date_stats[date_key]['total_tokens'] += total
                date_stats[date_key]['models'][record['model']] += 1

            except ValueError:
                continue

        # 转换为可序列化的格式
        result = {}
        for date_key, stats in date_stats.items():
            stats['models'] = dict(stats['models'])
            result[date_key] = stats

        return result

    def get_summary(self) -> Dict[str, Any]:
        """获取总体统计摘要"""
        total_stats = {
            'total_input_tokens': 0,
            'total_cache_creation_tokens': 0,
            'total_cache_read_tokens': 0,
            'total_output_tokens': 0,
            'total_requests': len(self.stats_data),
            'total_tokens': 0,
            'unique_models': set(),
            'date_range': {
                'start': None,
                'end': None
            }
        }

        timestamps = []

        for record in self.stats_data:
            total_stats['total_input_tokens'] += record['input_tokens']
            total_stats['total_cache_creation_tokens'] += record['cache_creation_input_tokens']
            total_stats['total_cache_read_tokens'] += record['cache_read_input_tokens']
            total_stats['total_output_tokens'] += record['output_tokens']
            total_stats['unique_models'].add(record['model'])

            total = (record['input_tokens'] +
                    record['cache_creation_input_tokens'] +
                    record['cache_read_input_tokens'] +
                    record['output_tokens'])
            total_stats['total_tokens'] += total

            try:
                timestamp = datetime.strptime(record['timestamp'], '%Y-%m-%d %H:%M:%S')
                timestamps.append(timestamp)
            except ValueError:
                continue

        # 确定日期范围
        if timestamps:
            total_stats['date_range']['start'] = min(timestamps).strftime('%Y-%m-%d %H:%M:%S')
            total_stats['date_range']['end'] = max(timestamps).strftime('%Y-%m-%d %H:%M:%S')

        # 转换set为list以便JSON序列化
        total_stats['unique_models'] = list(total_stats['unique_models'])

        return total_stats

    def _merge_stats(self, old_stats: Dict[str, Any], new_stats: Dict[str, Any]) -> Dict[str, Any]:
        """合并新旧统计数据（累计模式）

        Args:
            old_stats: 旧的统计数据
            new_stats: 新的统计数据

        Returns:
            合并后的统计数据
        """
        merged = new_stats.copy()

        # 1. 合并 summary
        if 'summary' in old_stats and 'summary' in new_stats:
            old_summary = old_stats['summary']
            new_summary = new_stats['summary']

            merged['summary'] = {
                'total_input_tokens': old_summary.get('total_input_tokens', 0) + new_summary.get('total_input_tokens', 0),
                'total_cache_creation_tokens': old_summary.get('total_cache_creation_tokens', 0) + new_summary.get('total_cache_creation_tokens', 0),
                'total_cache_read_tokens': old_summary.get('total_cache_read_tokens', 0) + new_summary.get('total_cache_read_tokens', 0),
                'total_output_tokens': old_summary.get('total_output_tokens', 0) + new_summary.get('total_output_tokens', 0),
                'total_requests': old_summary.get('total_requests', 0) + new_summary.get('total_requests', 0),
                'total_tokens': old_summary.get('total_tokens', 0) + new_summary.get('total_tokens', 0),
                'unique_models': list(set(old_summary.get('unique_models', []) + new_summary.get('unique_models', []))),
                'date_range': {
                    'start': min(old_summary.get('date_range', {}).get('start', '9999-99-99 99:99:99'),
                               new_summary.get('date_range', {}).get('start', '9999-99-99 99:99:99')),
                    'end': max(old_summary.get('date_range', {}).get('end', '0000-00-00 00:00:00'),
                              new_summary.get('date_range', {}).get('end', '0000-00-00 00:00:00'))
                }
            }

        # 2. 合并 by_model
        if 'by_model' in old_stats and 'by_model' in new_stats:
            merged['by_model'] = {}
            all_models = set(old_stats['by_model'].keys()) | set(new_stats['by_model'].keys())

            for model in all_models:
                old_model = old_stats['by_model'].get(model, {})
                new_model = new_stats['by_model'].get(model, {})

                merged['by_model'][model] = {
                    'total_input_tokens': old_model.get('total_input_tokens', 0) + new_model.get('total_input_tokens', 0),
                    'total_cache_creation_tokens': old_model.get('total_cache_creation_tokens', 0) + new_model.get('total_cache_creation_tokens', 0),
                    'total_cache_read_tokens': old_model.get('total_cache_read_tokens', 0) + new_model.get('total_cache_read_tokens', 0),
                    'total_output_tokens': old_model.get('total_output_tokens', 0) + new_model.get('total_output_tokens', 0),
                    'total_requests': old_model.get('total_requests', 0) + new_model.get('total_requests', 0),
                    'total_tokens': old_model.get('total_tokens', 0) + new_model.get('total_tokens', 0)
                }

        # 3. 合并 daily/weekly/monthly 统计
        for period in ['daily', 'weekly', 'monthly']:
            if period in old_stats and period in new_stats:
                merged[period] = {}
                all_dates = set(old_stats[period].keys()) | set(new_stats[period].keys())

                for date_key in all_dates:
                    old_date = old_stats[period].get(date_key, {})
                    new_date = new_stats[period].get(date_key, {})

                    # 合并models统计
                    old_models = old_date.get('models', {})
                    new_models = new_date.get('models', {})
                    merged_models = {}
                    all_model_names = set(old_models.keys()) | set(new_models.keys())
                    for model_name in all_model_names:
                        merged_models[model_name] = old_models.get(model_name, 0) + new_models.get(model_name, 0)

                    merged[period][date_key] = {
                        'total_input_tokens': old_date.get('total_input_tokens', 0) + new_date.get('total_input_tokens', 0),
                        'total_cache_creation_tokens': old_date.get('total_cache_creation_tokens', 0) + new_date.get('total_cache_creation_tokens', 0),
                        'total_cache_read_tokens': old_date.get('total_cache_read_tokens', 0) + new_date.get('total_cache_read_tokens', 0),
                        'total_output_tokens': old_date.get('total_output_tokens', 0) + new_date.get('total_output_tokens', 0),
                        'total_requests': old_date.get('total_requests', 0) + new_date.get('total_requests', 0),
                        'total_tokens': old_date.get('total_tokens', 0) + new_date.get('total_tokens', 0),
                        'models': merged_models
                    }

        return merged

    def export_to_json(self, output_file: str = 'json_data/token_stats.json', cumulative: bool = True):
        """导出统计结果为JSON文件

        Args:
            output_file: 输出文件路径
            cumulative: 是否启用累计模式（默认True，合并历史数据而不是覆盖）
        """
        # 如果是相对路径，转换为基于脚本目录的绝对路径
        if not os.path.isabs(output_file):
            script_dir = os.path.dirname(os.path.abspath(__file__))
            output_file = os.path.join(script_dir, output_file)

        # 生成当前日志的统计数据
        new_stats = {
            'summary': self.get_summary(),
            'by_model': self.get_stats_by_model(),
            'daily': self.get_stats_by_date('daily'),
            'weekly': self.get_stats_by_date('weekly'),
            'monthly': self.get_stats_by_date('monthly'),
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        # 如果启用累计模式，且存在旧的统计文件，则合并数据
        if cumulative and os.path.exists(output_file):
            try:
                with open(output_file, 'r', encoding='utf-8') as f:
                    old_stats = json.load(f)

                print(f"检测到现有统计文件，启用累计模式...")
                print(f"  - 旧统计: {old_stats.get('summary', {}).get('total_requests', 0)} 次请求")
                print(f"  - 新增: {new_stats.get('summary', {}).get('total_requests', 0)} 次请求")

                # 合并新旧数据
                stats_result = self._merge_stats(old_stats, new_stats)

                print(f"  - 累计: {stats_result.get('summary', {}).get('total_requests', 0)} 次请求")
            except Exception as e:
                print(f"读取旧统计文件失败: {e}，将覆盖写入")
                stats_result = new_stats
        else:
            stats_result = new_stats

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(stats_result, f, ensure_ascii=False, indent=2)

        print(f"统计结果已导出到: {output_file}")
        return stats_result


# ============================================================
# TokenStatsManager - 实时统计管理器（新增）
# ============================================================

class TokenStatsManager:
    """Token统计管理器 - 线程安全、原子写入、实时统计"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        """单例模式"""
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, stats_file="json_data/token_stats.json"):
        if hasattr(self, '_initialized'):
            return

        # 如果是相对路径，转换为基于脚本目录的绝对路径
        if not os.path.isabs(stats_file):
            script_dir = os.path.dirname(os.path.abspath(__file__))
            stats_file = os.path.join(script_dir, stats_file)

        self.stats_file = os.path.abspath(stats_file)
        self.file_lock = threading.Lock()  # 文件操作锁
        self._initialized = True

        # 确保文件存在
        if not os.path.exists(self.stats_file):
            self._init_empty_stats()

    def _init_empty_stats(self):
        """初始化空的统计文件"""
        empty_stats = {
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
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self._atomic_write(empty_stats)

    def _atomic_write(self, data: Dict[str, Any]):
        """原子写入JSON文件（临时文件+rename）"""
        # 0. 确保目录存在
        stats_dir = os.path.dirname(self.stats_file)
        if stats_dir and not os.path.exists(stats_dir):
            os.makedirs(stats_dir, exist_ok=True)

        # 1. 写入临时文件
        fd, temp_path = tempfile.mkstemp(
            dir=stats_dir,
            prefix='.tmp_stats_',
            suffix='.json'
        )

        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            # 2. 原子重命名（这是原子操作）
            os.replace(temp_path, self.stats_file)
        except Exception as e:
            # 清理临时文件
            try:
                os.remove(temp_path)
            except:
                pass
            raise e

    def _get_empty_stats_structure(self):
        """获取空的统计数据结构"""
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
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    def record_usage(self, model: str, usage_data: Dict[str, int], request_id: str = ""):
        """
        记录单次API调用的usage（实时统计）

        Args:
            model: 模型名称
            usage_data: usage数据字典，支持两种格式：
                - Claude格式：{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}
                - Codex格式：{prompt_tokens, completion_tokens, total_tokens}
            request_id: 请求ID（可选）
        """
        with self.file_lock:  # 并发安全
            try:
                # 1. 读取现有数据
                if os.path.exists(self.stats_file):
                    with open(self.stats_file, 'r', encoding='utf-8') as f:
                        stats = json.load(f)
                else:
                    stats = self._get_empty_stats_structure()

                # 2. 规范化usage数据（兼容两种格式）
                input_tokens = usage_data.get('input_tokens') or usage_data.get('prompt_tokens', 0)
                output_tokens = usage_data.get('output_tokens') or usage_data.get('completion_tokens', 0)
                cache_creation = usage_data.get('cache_creation_input_tokens', 0)
                cache_read = usage_data.get('cache_read_input_tokens', 0)
                total_tokens = usage_data.get('total_tokens', input_tokens + output_tokens)

                # 3. 更新summary
                stats['summary']['total_requests'] += 1
                stats['summary']['total_tokens'] += total_tokens
                stats['summary']['total_input_tokens'] += input_tokens
                stats['summary']['total_output_tokens'] += output_tokens
                stats['summary']['total_cache_creation_tokens'] += cache_creation
                stats['summary']['total_cache_read_tokens'] += cache_read

                # 更新unique_models
                if model not in stats['summary']['unique_models']:
                    stats['summary']['unique_models'].append(model)

                # 4. 更新by_model
                if model not in stats['by_model']:
                    stats['by_model'][model] = {
                        'total_requests': 0,
                        'total_tokens': 0,
                        'total_input_tokens': 0,
                        'total_output_tokens': 0,
                        'total_cache_creation_tokens': 0,
                        'total_cache_read_tokens': 0
                    }

                stats['by_model'][model]['total_requests'] += 1
                stats['by_model'][model]['total_tokens'] += total_tokens
                stats['by_model'][model]['total_input_tokens'] += input_tokens
                stats['by_model'][model]['total_output_tokens'] += output_tokens
                stats['by_model'][model]['total_cache_creation_tokens'] += cache_creation
                stats['by_model'][model]['total_cache_read_tokens'] += cache_read

                # 5. 更新daily
                today = datetime.now().strftime("%Y-%m-%d")

                if today not in stats['daily']:
                    stats['daily'][today] = {
                        'total_requests': 0,
                        'total_tokens': 0,
                        'total_input_tokens': 0,
                        'total_output_tokens': 0,
                        'total_cache_creation_tokens': 0,
                        'total_cache_read_tokens': 0,
                        'models': {}
                    }

                stats['daily'][today]['total_requests'] += 1
                stats['daily'][today]['total_tokens'] += total_tokens
                stats['daily'][today]['total_input_tokens'] += input_tokens
                stats['daily'][today]['total_output_tokens'] += output_tokens
                stats['daily'][today]['total_cache_creation_tokens'] += cache_creation
                stats['daily'][today]['total_cache_read_tokens'] += cache_read

                # 更新daily中的models
                if model not in stats['daily'][today]['models']:
                    stats['daily'][today]['models'][model] = 0
                stats['daily'][today]['models'][model] += 1

                # 6. 更新生成时间
                stats['generated_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # 7. 原子写入
                self._atomic_write(stats)

            except Exception as e:
                print(f"记录统计数据失败: {e}")
                import traceback
                traceback.print_exc()


# 全局单例（用于 app.py 导入）
stats_mgr = None

def get_stats_manager(stats_file=None):
    """获取统计管理器单例"""
    global stats_mgr
    if stats_mgr is None:
        if stats_file is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            stats_file = os.path.join(script_dir, "json_data", "token_stats.json")
        stats_mgr = TokenStatsManager(stats_file)
    return stats_mgr


def main():
    """主函数 - 已改为实时统计模式，此脚本不再用于日志解析"""
    print("=" * 60)
    print("Claude API Token使用量统计分析器")
    print("=" * 60)
    print()
    print("⚠️  注意：统计功能已改为实时记录模式")
    print()
    print("📊 统计数据现在通过以下方式自动更新：")
    print("   - 每次API调用后自动记录usage数据")
    print("   - 数据实时保存到 token_stats.json")
    print("   - 无需手动执行此脚本进行统计")
    print()
    print("💡 如需查看当前统计数据：")
    print("   - 访问管理页面：http://localhost:8080/admin")
    print("   - 或直接查看：token_stats.json 文件")
    print()

    # 显示当前统计文件的状态
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_file = os.path.join(script_dir, "json_data", "token_stats.json")

    if os.path.exists(output_file):
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            summary = data.get('summary', {})
            print("=" * 60)
            print("当前统计数据概览：")
            print("=" * 60)
            print(f"  * 总请求数: {summary.get('total_requests', 0):,}")
            print(f"  * 总Token数: {summary.get('total_tokens', 0):,}")
            print(f"  * 最后更新: {data.get('generated_at', '未知')}")
            print(f"  * 使用模型数: {len(summary.get('unique_models', []))}")
        except Exception as e:
            print(f"读取统计文件时出错: {e}")
    else:
        print("⚠️  统计文件尚未生成")
        print("   在首次API调用后将自动创建")

    print()
    print("=" * 60)
    print()
    print("💡 提示：如需清空统计数据，请使用管理页面的'清空数据'按钮")


if __name__ == "__main__":
    main()
