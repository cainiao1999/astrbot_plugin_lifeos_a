from pathlib import Path
from datetime import datetime
import shutil
import re
import sqlite3

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "lifeos",
    "Dad Zhang",
    "LifeOS 个人数据系统",
    "0.1.0",
)
class LifeOSPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.data_path = Path(self.config.get('data_path', '/data/lifeos/Data'))
        self.config_path = Path(self.config.get('config_path', '/data/lifeos/Config'))
        self.db_path = Path(self.config.get('db_path', '/data/lifeos/Database/lifeos.db'))

    async def initialize(self):
        """插件初始化：创建目录结构，复制默认规则"""
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.config_path.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_rule_files()
        logger.info(f"LifeOS 初始化完成")
        logger.info(f"  数据目录：{self.data_path}")
        logger.info(f"  配置目录：{self.config_path}")
        logger.info(f"  数据库：{self.db_path}")

    # ──────────── 规则文件管理 ────────────

    def _ensure_rule_files(self):
        """将插件自带的 rules/ 复制到配置目录（仅首次）"""
        default_rules_dir = Path(__file__).parent / 'rules'
        if not default_rules_dir.exists():
            logger.warning(f"默认规则目录不存在：{default_rules_dir}")
            return
        for rule_file in default_rules_dir.iterdir():
            if rule_file.suffix == '.md':
                target = self.config_path / rule_file.name
                if not target.exists():
                    shutil.copy2(rule_file, target)
                    logger.info(f"已复制默认规则：{rule_file.name}")

    def _read_rule(self, name: str) -> str:
        """从配置目录读取规则文件"""
        path = self.config_path / name
        if not path.exists():
            logger.error(f"规则文件未找到：{path}")
            return ''
        return path.read_text(encoding='utf-8')

    # ──────────── 消息拆分（本地，不依赖 LLM） ────────────

    def _split_activities(self, text: str) -> list:
        """
        将一条包含多个活动的消息拆分为独立描述。
        """
        split_pattern = (
            r'(?<=[，,。.！!？?；;])'
            r'\s*'
            r'(?='
            r'(?:我)?(?:今天)?'
            r'(?:写了|码了|码字|写作|在写|完成了|写了稿|更了|'
            r'读了|看了|阅读|在读|在看|翻看)'
            r')'
        )
        parts = re.split(split_pattern, text)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) <= 1:
            return [text.strip()]
        return parts

    # ──────────── LLM 调用（官方正确 API） ────────────

    async def _call_llm_single(self, event: AstrMessageEvent, rule_text: str, activity_text: str) -> str:
        """
        将单个活动的描述发给 LLM，返回生成的 Markdown。
        使用 AstrBot 官方推荐的 v4.5.7+ API。
        返回 None 表示 LLM 不可用/出错。
        """
        system_prompt = (
            "你是 LifeOS 记录助手。请严格按照下方规则，"
            "将用户的自然语言描述转换为标准格式。\n"
            "注意：你只负责处理一个活动的描述。\n"
            "只输出 Markdown，不要添加任何额外说明。"
        )

        user_prompt = (
            f"## 记录规则\n\n"
            f"{rule_text}\n\n"
            f"## 用户输入\n\n"
            f"{activity_text}\n\n"
            f"请根据上述规则转换。"
        )

        try:
            # 获取当前会话的 LLM 提供商 ID
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)

            if not provider_id:
                logger.warning("未获取到 LLM 提供商 ID，降级为本地解析")
                return None

            # 调用 LLM（官方推荐 API）
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=(
                    f"{system_prompt}\n\n"
                    f"{user_prompt}"
                ),
            )

            if llm_resp and hasattr(llm_resp, 'completion_text') and llm_resp.completion_text.strip():
                logger.info(f"LLM 调用成功")
                return llm_resp.completion_text.strip()

        except Exception as e:
            logger.warning(f"LLM 调用失败，降级为本地解析：{e}")

        return None  # 降级标志

    # ──────────── 强制修正时间 ────────────

    def _force_current_time(self, markdown_text: str) -> str:
        """
        将 Markdown 中的所有时间行替换为当前系统时间。
        """
        now = datetime.now().strftime('%H:%M')
        return re.sub(r'时间：.*', f'时间：{now}', markdown_text)

    # ──────────── 从 Markdown 提取结构化字段（LLM 路径用） ────────────

    def _extract_record_from_markdown(self, markdown: str, record_date: str):
        """
        从 LLM 生成的 Markdown 中提取结构化字段，用于写入数据库。
        返回 None 表示该记录不需要写入 DB（ERROR 或 其他 类型）。
        """
        if '---ERROR---' in markdown:
            return None

        fields = {}
        for line in markdown.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            if '：' in line:
                key, _, value = line.partition('：')
                fields[key.strip()] = value.strip()

        behavior = fields.get('行为', '')

        if behavior == '写':
            duration = fields.get('写作时长', '')
            output_count = fields.get('产出字数', '')
            return {
                'db_table': 'writing_records',
                'record_date': record_date,
                'record_time': fields.get('时间', ''),
                'duration': float(duration) if duration else None,
                'output_count': int(output_count) if output_count else None,
                'work_name': fields.get('产出作品', ''),
                'record_type': fields.get('写作类型', '随笔'),
                'remark': '',
                'source': 'qq',
            }
        elif behavior == '读':
            duration = fields.get('阅读时长', '')
            output_count = fields.get('阅读章节', '')
            return {
                'db_table': 'reading_records',
                'record_date': record_date,
                'record_time': fields.get('时间', ''),
                'duration': float(duration) if duration else None,
                'output_count': int(float(output_count)) if output_count else None,
                'work_name': fields.get('阅读作品', ''),
                'record_type': fields.get('阅读类型', '随读'),
                'remark': '',
                'source': 'qq',
            }
        else:
            # "其他" 类型不写入 DB
            return None

    # ──────────── 本地解析降级（单个活动） ────────────

    def _local_parse_single(self, text: str, now: str, today: str) -> tuple:
        """本地解析单个活动描述，返回 (markdown, record_dict)"""
        writing_kw = ['写了', '码了', '码字', '写作', '在写', '完成了', '写了稿', '更了']
        reading_kw = ['读了', '看了', '阅读', '在读', '在看', '翻看']

        is_writing = any(kw in text for kw in writing_kw)
        is_reading = any(kw in text for kw in reading_kw)

        if is_writing and not is_reading:
            return self._local_writing(text, now, today)
        elif is_reading and not is_writing:
            return self._local_reading(text, now, today)
        elif is_writing and is_reading:
            if re.search(r'\d+\s*章', text) and not re.search(r'\d+\s*(字|千字|万字)', text):
                return self._local_reading(text, now, today)
            return self._local_writing(text, now, today)
        else:
            return self._local_other(text, now)

    def _local_writing(self, text: str, now: str, today: str) -> tuple:
        lines = ['---']
        lines.append(f'时间：{now}')
        lines.append('')
        lines.append('行为：写')
        lines.append('')

        # ---- 提取时长（支持同时有小时和分钟） ----
        total_hours = 0.0
        has_duration = False

        m = re.search(r'(\d+(?:\.\d+)?)\s*(小时|h)', text)
        if m:
            total_hours += float(m.group(1))
            has_duration = True

        m = re.search(r'(\d+(?:\.\d+)?)\s*(分钟|min)', text)
        if m:
            total_hours += float(m.group(1)) / 60
            has_duration = True

        # 中文数字：三小时、五小时
        cn_num_map = {
            '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
            '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '半': 0.5,
        }
        m = re.search(r'([一二两三四五六七八九十半])\s*(小时|h)', text)
        if m:
            cn_char = m.group(1)
            if cn_char == '半':
                total_hours += 0.5
            elif cn_char == '十':
                total_hours += 10
            else:
                total_hours += cn_num_map.get(cn_char, 0)
            has_duration = True

        # 半小时
        if re.search(r'半\s*(小时|h)', text) and not has_duration:
            total_hours += 0.5
            has_duration = True

        duration = round(total_hours, 2) if has_duration else None

        # ---- 提取字数 ----
        word_count = None
        m = re.search(r'(\d+(?:\.\d+)?)\s*(万字|千字|字)', text)
        if m:
            v = float(m.group(1))
            if m.group(2) == '千字':
                word_count = int(v * 1000)
            elif m.group(2) == '万字':
                word_count = int(v * 10000)
            else:
                word_count = int(v)

        # ---- 提取作品名 ----
        m = re.search(r'《([^》]+)》', text)
        work = m.group(1) if m else ''
        if not work:
            m = re.search(r'"([^"]+)"', text)
            if m:
                work = m.group(1)

        # ---- 提取写作类型 ----
        m = re.search(r'(正文|随笔|设计)', text)
        wtype = m.group(1) if m else '随笔'

        # 校验
        if duration is None and word_count is None:
            return ('---ERROR---\n'
                    '写作时长和产出字数不能同时为空，烦请作者大大补充。\n'
                    '---ERROR---', None)

        lines.append(f'写作时长：{duration}' if duration is not None else '写作时长：')
        lines.append('')
        lines.append(f'产出字数：{word_count}' if word_count is not None else '产出字数：')
        lines.append('')
        lines.append(f'产出作品：{work}')
        lines.append('')
        lines.append(f'写作类型：{wtype}')
        lines.append('')
        lines.append('——————')

        record = {
            'db_table': 'writing_records',
            'record_date': today,
            'record_time': now,
            'duration': duration,
            'output_count': word_count,
            'work_name': work,
            'record_type': wtype,
            'remark': '',
            'source': 'qq',
        }

        return ('\n'.join(lines), record)

    def _local_reading(self, text: str, now: str, today: str) -> tuple:
        lines = ['---']
        lines.append(f'时间：{now}')
        lines.append('')
        lines.append('行为：读')
        lines.append('')

        # ---- 提取时长（支持同时有小时和分钟） ----
        total_hours = 0.0
        has_duration = False

        m = re.search(r'(\d+(?:\.\d+)?)\s*(小时|h)', text)
        if m:
            total_hours += float(m.group(1))
            has_duration = True

        m = re.search(r'(\d+(?:\.\d+)?)\s*(分钟|min)', text)
        if m:
            total_hours += float(m.group(1)) / 60
            has_duration = True

        cn_num_map = {
            '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
            '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '半': 0.5,
        }
        m = re.search(r'([一二两三四五六七八九十半])\s*(小时|h)', text)
        if m:
            cn_char = m.group(1)
            if cn_char == '半':
                total_hours += 0.5
            elif cn_char == '十':
                total_hours += 10
            else:
                total_hours += cn_num_map.get(cn_char, 0)
            has_duration = True

        if re.search(r'半\s*(小时|h)', text) and not has_duration:
            total_hours += 0.5
            has_duration = True

        duration = round(total_hours, 2) if has_duration else None

        # ---- 章节 ----
        chapters = None
        m = re.search(r'(\d+)\s*章', text)
        if m:
            chapters = int(m.group(1))

        if duration is None and chapters is None:
            return ('---ERROR---\n'
                    '阅读时长和阅读章节不能同时为空，烦请读者大大补充。\n'
                    '---ERROR---', None)

        lines.append(f'阅读时长：{duration}' if duration is not None else '阅读时长：')
        lines.append('')
        lines.append(f'阅读章节：{chapters}' if chapters is not None else '阅读章节：')
        lines.append('')

        m = re.search(r'《([^》]+)》', text)
        book = m.group(1) if m else ''
        if not book:
            m = re.search(r'"([^"]+)"', text)
            if m:
                book = m.group(1)
        lines.append(f'阅读作品：{book}')
        lines.append('')

        m = re.search(r'(精读|随读)', text)
        rtype = m.group(1) if m else '随读'
        lines.append(f'阅读类型：{rtype}')
        lines.append('')
        lines.append('——————')

        record = {
            'db_table': 'reading_records',
            'record_date': today,
            'record_time': now,
            'duration': duration,
            'output_count': chapters,
            'work_name': book,
            'record_type': rtype,
            'remark': '',
            'source': 'qq',
        }

        return ('\n'.join(lines), record)

    def _local_other(self, text: str, now: str) -> tuple:
        lines = ['---']
        lines.append(f'时间：{now}')
        lines.append('')
        lines.append('行为：其他')
        lines.append('')

        total_hours = 0.0
        has_duration = False
        note = text

        m = re.search(r'(\d+(?:\.\d+)?)\s*(小时|h)', text)
        if m:
            total_hours += float(m.group(1))
            has_duration = True

        m = re.search(r'(\d+(?:\.\d+)?)\s*(分钟|min)', text)
        if m:
            total_hours += float(m.group(1)) / 60
            has_duration = True

        cn_num_map = {
            '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
            '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '半': 0.5,
        }
        m = re.search(r'([一二两三四五六七八九十半])\s*(小时|h)', text)
        if m:
            cn_char = m.group(1)
            if cn_char == '半':
                total_hours += 0.5
            elif cn_char == '十':
                total_hours += 10
            else:
                total_hours += cn_num_map.get(cn_char, 0)
            has_duration = True

        if re.search(r'半\s*(小时|h)', text) and not has_duration:
            total_hours += 0.5
            has_duration = True

        duration = round(total_hours, 2) if has_duration else None

        if duration is not None:
            note = re.sub(
                r'\d+(?:\.\d+)?\s*(?:小时|分钟|h|min)|[一二两三四五六七八九十半]\s*(?:小时|h)',
                '', note
            ).strip()

        lines.append(f'时长：{duration}' if duration is not None else '时长：')
        lines.append('')
        lines.append(f'备注：{note}')
        lines.append('')
        lines.append('——————')

        return ('\n'.join(lines), None)

    # ──────────── 核心处理管道 ────────────

    async def _process_activities(self, event: AstrMessageEvent, rule_text: str, raw_input: str) -> tuple:
        """
        处理一条可能包含多个活动的输入。
        返回: (entries列表, methods列表, records列表, raw_texts列表)
        """
        now = datetime.now().strftime('%H:%M')
        today = datetime.now().strftime('%Y-%m-%d')
        activities = self._split_activities(raw_input)
        all_entries = []
        methods = []
        records = []

        for activity in activities:
            llm_result = await self._call_llm_single(event, rule_text, activity)

            if llm_result and '---ERROR---' not in llm_result:
                entry = self._force_current_time(llm_result)
                methods.append('LLM')
                record = self._extract_record_from_markdown(entry, today)
            else:
                entry, record = self._local_parse_single(activity, now, today)
                if llm_result is None:
                    methods.append('本地(LLM不可用)')
                else:
                    methods.append('本地(LLM返回异常)')

            all_entries.append(entry)
            records.append(record)

        return all_entries, methods, records, activities

    # ──────────── 数据库写入 ────────────

    def _write_to_db(self, records: list) -> int:
        """将结构化记录写入 SQLite。跳过 None。返回实际写入条数。"""
        count = 0
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        for record in records:
            if record is None:
                continue
            table = record['db_table']
            columns = {k: v for k, v in record.items() if k != 'db_table'}
            names = ', '.join(columns.keys())
            placeholders = ', '.join(['?' for _ in columns])
            sql = f'INSERT INTO {table} ({names}) VALUES ({placeholders})'
            cursor.execute(sql, list(columns.values()))
            count += 1

        conn.commit()
        conn.close()
        logger.info(f'LifeOS 写入数据库：{count} 条')
        return count

    # ──────────── 文件写入 ────────────

    def _write_records(self, entries: list, methods: list, raw_texts: list) -> Path:
        """将记录以增强日志格式追加到当日文件"""
        today = datetime.now().strftime('%Y-%m-%d')
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        file_path = self.data_path / f'{today}.md'

        is_new = not file_path.exists()
        with open(file_path, 'a', encoding='utf-8') as f:
            if is_new:
                f.write(f'# {today}\n\n')

            for i, (entry, method, raw) in enumerate(zip(entries, methods, raw_texts), 1):
                f.write(f'### [{timestamp}] 记录 #{i} [{method}]\n')
                f.write(f'> 原始输入：{raw}\n\n')
                f.write(entry.strip() + '\n\n')

        logger.info(f'LifeOS 写入日志：{file_path}')
        return file_path

    # ──────────── 命令入口 ────────────

    @filter.command("helloworld", alias={"hello", "hi"})
    async def helloworld(self, event: AstrMessageEvent):
        """HelloWorld 测试"""
        user_name = event.get_sender_name()
        message_str = event.message_str
        logger.info(event.get_messages())
        yield event.plain_result(
            f"Hello大作家，{user_name}，你发了 {message_str}!"
        )

    @filter.command("test", alias={"测试", "t"})
    async def test(self, event: AstrMessageEvent):
        """测试插件是否正常运行"""
        user_name = event.get_sender_name()
        message_str = event.message_str
        logger.info(event.get_messages())
        yield event.plain_result(
            f"Hello大作家，你成功执行了 Test！\n"
            f"用户：{user_name}\n"
            f"输入：{message_str}"
        )

    @filter.command("记录")
    async def record(self, event: AstrMessageEvent):
        """
        记录活动：拆分 → 逐条处理（LLM/本地）→ 写入 DB + 日志
        """
        message = event.message_str.strip()
        content = message[len("记录"):].strip()

        if not content:
            yield event.plain_result(
                "📝 请告诉我你要记录的内容！\n\n"
                "例如：\n"
                "  /记录 写了《嘉豪》正文1小时，1800字\n"
                "  /记录 读了《蛊真人》精读2小时12章\n"
                "  /记录 写了A1小时，读了B2章\n"
                "  /记录 今天重新部署了AstrBot"
            )
            return

        # 1. 读取规则文件
        rule_text = self._read_rule('record_rule.md')
        if not rule_text:
            yield event.plain_result("⚠️ 规则文件 record_rule.md 未找到，请检查配置。")
            return

        # 2. 核心处理
        entries, methods, records, raw_texts = await self._process_activities(event, rule_text, content)

        # 3. 检查错误
        generated = '\n'.join(entries)
        if '---ERROR---' in generated:
            errors = re.findall(r'---ERROR---\n(.*?)\n---ERROR---', generated, re.DOTALL)
            error_msg = '\n'.join(e.strip() for e in errors)
            yield event.plain_result(f"⚠️ {error_msg}")
            return

        # 4. 写入数据库
        db_count = self._write_to_db(records)

        # 5. 写入日志文件
        file_path = self._write_records(entries, methods, raw_texts)

        # 6. 统计与回复
        method_info = ' | '.join([f'活动{i+1}: {m}' for i, m in enumerate(methods)])

        yield event.plain_result(
            f"✅ 已记录！共 {len(entries)} 条\n"
            f"💾 数据库写入 {db_count} 条\n"
            f"🔧 {method_info}\n"
            f"📂 {file_path}"
        )

    async def terminate(self):
        """插件卸载时调用"""
        logger.info("LifeOS 插件已卸载")
