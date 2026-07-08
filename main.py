from pathlib import Path
from datetime import datetime, timedelta
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
        self.pending = {}  # {user_id: {'original': str, 'question': str, 'timestamp': datetime}}

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

        # ---- 提取写作类型（排除《》内的词） ----
        text_no_book = re.sub(r'《[^》]+》', '', text)
        m = re.search(r'(正文|随笔|设计|设定|大纲)', text_no_book)
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

    # ──────────── 完美匹配检查 ────────────

    def _try_perfect_match(self, text: str, now: str, today: str):
        """
        尝试本地完美匹配。成功返回 (markdown, dict)，有歧义返回 None。
        歧义条件：类型关键词冲突、作品名与 DB 中已有记录模糊匹配。
        """
        writing_kw = ['写了', '码了', '码字', '写作', '在写', '完成了', '写了稿', '更了']
        reading_kw = ['读了', '看了', '阅读', '在读', '在看', '翻看']

        is_writing = any(kw in text for kw in writing_kw)
        is_reading = any(kw in text for kw in reading_kw)

        if not is_writing and not is_reading:
            return self._local_other(text, now)

        # 检查类型关键词冲突（排除书名号内的词）
        text_no_book = re.sub(r'《[^》]+》', '', text)
        writing_types = ['正文', '随笔', '设计', '设定', '大纲']
        reading_types = ['精读', '随读']

        found_wt = [t for t in writing_types if t in text_no_book]
        found_rt = [t for t in reading_types if t in text_no_book]

        if len(found_wt) > 1:
            return None  # 多个写作类型关键词
        if len(found_rt) > 1:
            return None  # 多个阅读类型关键词

        # 检查作品名歧义（与 DB 中已有作品模糊匹配）
        mentioned = re.findall(r'《([^》]+)》', text)
        for work in mentioned:
            if self._find_similar_works(work):
                return None  # 存在相似作品名

        # 通过检查，本地解析
        if is_writing and is_reading:
            if re.search(r'\d+\s*章', text) and not re.search(r'\d+\s*(字|千字|万字)', text):
                return self._local_reading(text, now, today)
            return self._local_writing(text, now, today)
        elif is_writing:
            return self._local_writing(text, now, today)
        else:
            return self._local_reading(text, now, today)

    def _find_similar_works(self, work_name: str) -> list:
        """在 DB 中查找与给定名称相似但不完全相同的作品名。"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        similar = []
        for table in ['writing_records', 'reading_records']:
            cursor.execute(
                f"SELECT DISTINCT work_name FROM {table} WHERE work_name LIKE ? AND work_name != ?",
                (f'%{work_name}%', work_name)
            )
            for row in cursor.fetchall():
                if row[0] and row[0] not in similar:
                    similar.append(row[0])
        conn.close()
        return similar

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

    # ──────────── 记录回复格式化 ────────────

    def _format_record_response(self, records: list) -> str:
        """格式化记录成功回复：新增内容 + 今日总计"""
        today_str = datetime.now().strftime('%Y-%m-%d')

        new_items = []
        for record in records:
            if record is None:
                continue
            table = record['db_table']
            work_name = record.get('work_name', '')
            record_type = record.get('record_type', '')
            output_count = record.get('output_count')
            duration = record.get('duration')

            prefix = f'《{work_name}》' if work_name else ''
            prefix += record_type if record_type else ''

            if table == 'writing_records':
                if output_count:
                    s = f'{prefix}新增{output_count}字'
                    if duration:
                        s += f'，耗时{duration:.2f}小时'
                elif duration:
                    s = f'{prefix}新增写作{duration:.2f}小时'
                else:
                    s = f'{prefix}已记录'
                new_items.append(s)
            elif table == 'reading_records':
                if output_count:
                    s = f'{prefix}新增{output_count}章'
                    if duration:
                        s += f'，耗时{duration:.2f}小时'
                elif duration:
                    s = f'{prefix}新增阅读{duration:.2f}小时'
                else:
                    s = f'{prefix}已记录'
                new_items.append(s)

        result = '已成功记录，' + '，'.join(new_items) if new_items else '已成功记录'

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        cursor.execute(
            "SELECT COALESCE(SUM(duration), 0), COALESCE(SUM(output_count), 0) FROM writing_records WHERE record_date = ?",
            (today_str,)
        )
        w_dur, w_out = cursor.fetchone()

        cursor.execute(
            "SELECT COALESCE(SUM(duration), 0), COALESCE(SUM(output_count), 0) FROM reading_records WHERE record_date = ?",
            (today_str,)
        )
        r_dur, r_out = cursor.fetchone()
        conn.close()

        today_parts = []
        if w_out > 0 or w_dur > 0:
            wp = []
            if w_out > 0:
                wp.append(f'{int(w_out)}个字')
            if w_dur > 0:
                wp.append(f'{w_dur:.2f}个小时')
            today_parts.append(f'您今日共写了{"，".join(wp)}')

        if r_out > 0 or r_dur > 0:
            rp = []
            if r_out > 0:
                rp.append(f'{int(r_out)}章')
            if r_dur > 0:
                rp.append(f'{r_dur:.2f}个小时')
            today_parts.append(f'您今日共读了{"，".join(rp)}')

        if today_parts:
            result += '！' + '！'.join(today_parts) + '！'

        return result

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

    # ──────────── LLM 记录辅助 ────────────

    async def _call_llm_for_record(self, event: AstrMessageEvent, rule_text: str, content: str,
                                     similar_works: str = '') -> str:
        """LLM 优先的记录调用。返回 LLM 输出文本或 None。"""
        system_prompt = (
            "你是 LifeOS 记录助手。请严格按照下方规则处理用户输入。\n"
            "如果信息有歧义，输出 QUESTION 格式询问用户。\n"
            "只输出记录条目、QUESTION 或 ERROR，不要添加额外说明。"
        )

        user_prompt = f"## 记录规则\n\n{rule_text}\n\n"
        if similar_works:
            user_prompt += f"## 已有相似作品\n\n{similar_works}\n\n"
        user_prompt += f"## 用户输入\n\n{content}\n\n请根据规则处理。"

        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                return None
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=f"{system_prompt}\n\n{user_prompt}",
            )
            if llm_resp and hasattr(llm_resp, 'completion_text') and llm_resp.completion_text.strip():
                return llm_resp.completion_text.strip()
        except Exception as e:
            logger.warning(f"LLM 记录调用失败：{e}")
        return None

    def _cleanup_pending(self):
        """清理超过 10 分钟的待确认记录"""
        now = datetime.now()
        stale = [uid for uid, v in self.pending.items()
                 if (now - v['timestamp']).total_seconds() > 600]
        for uid in stale:
            del self.pending[uid]

    def _get_similar_works_context(self, text: str) -> str:
        """获取与用户输入中作品名相似的已有作品列表，格式化为 LLM 上下文。"""
        mentioned = re.findall(r'《([^》]+)》', text)
        if not mentioned:
            return ''
        all_similar = []
        for work in mentioned:
            sim = self._find_similar_works(work)
            for s in sim:
                if s not in all_similar:
                    all_similar.append(s)
        if not all_similar:
            return ''
        return '已记录过的相似作品：\n' + '\n'.join(f'- {s}' for s in all_similar)

    async def _process_confirmation(self, event: AstrMessageEvent, user_id: str, reply: str):
        """处理用户对 LLM 追问的确认回复。返回 MessageEventResult 或 None。"""
        pending = self.pending.pop(user_id)
        rule_text = self._read_rule('record_rule.md')
        if not rule_text:
            yield event.plain_result("⚠️ 规则文件未找到。")
            return

        user_prompt = (
            f"## 记录规则\n\n{rule_text}\n\n"
            f"## 对话历史\n\n"
            f"助手：{pending['question']}\n"
            f"用户：{reply}\n\n"
            f"如果信息已明确，生成记录条目。如果仍有歧义，输出 QUESTION。"
        )

        system_prompt = (
            "你是 LifeOS 记录助手。根据用户的澄清回复，如果信息明确则生成记录，"
            "仍有歧义则输出 QUESTION。只输出记录或 QUESTION，不要额外说明。"
        )

        llm_output = await self._call_llm_for_query(event, system_prompt, user_prompt)

        if not llm_output:
            yield event.plain_result("⚠️ LLM 不可用，请重新发起 /记录。")
            return

        async for result in self._handle_llm_record_output(event, user_id, reply, llm_output):
            yield result

    async def _handle_llm_record_output(self, event: AstrMessageEvent, user_id: str,
                                          raw_text: str, llm_output: str):
        """统一处理 LLM 记录输出：QUESTION → 待确认，记录 → 写入，ERROR → 提示"""
        q_match = re.search(r'---QUESTION---\n(.*?)\n---QUESTION---', llm_output, re.DOTALL)
        if q_match:
            self.pending[user_id] = {
                'original': raw_text,
                'question': q_match.group(1).strip(),
                'timestamp': datetime.now(),
            }
            yield event.plain_result(f'🤔 {q_match.group(1).strip()}')
            return

        if '---ERROR---' in llm_output:
            errors = re.findall(r'---ERROR---\n(.*?)\n---ERROR---', llm_output, re.DOTALL)
            error_msg = '\n'.join(e.strip() for e in errors)
            yield event.plain_result(f"⚠️ {error_msg}")
            return

        # 有效记录：修正时间、提取字段、写入
        now = datetime.now().strftime('%H:%M')
        today = datetime.now().strftime('%Y-%m-%d')
        llm_output = self._force_current_time(llm_output)

        # 按 —————— 拆分多条记录
        entries = [e.strip() for e in re.split(r'——————', llm_output) if e.strip()]
        all_md = []
        all_records = []
        for entry in entries:
            full_entry = entry.strip() + '\n——————'
            all_md.append(full_entry)
            record = self._extract_record_from_markdown(full_entry, today)
            all_records.append(record)

        db_count = self._write_to_db(all_records)
        file_path = self._write_records(all_md, ['LLM'] * len(all_md), [raw_text] * len(all_md))

        yield event.plain_result(self._format_record_response(all_records))

    # ──────────── 查询功能 ────────────

    def _query_local(self, clean_input: str):
        """白名单本地查询。匹配返回格式化文本，不匹配返回 None。"""
        today_str = datetime.now().strftime('%Y-%m-%d')

        day_kw = {'今天', '本日', '今日'}
        week_kw = {'本周', '这周'}
        month_kw = {'本月', '这个月'}
        last7_kw = {'近一周', '近7天', '最近一周', '最近7天'}
        last30_kw = {'近一月', '近30天', '最近一月', '最近30天'}

        if not clean_input:
            parts = []
            parts.append(self._do_local_query('today', today_str))
            parts.append(self._do_local_query('week', today_str))
            parts.append(self._do_local_query('month', today_str))
            return ('\n\n' + '─' * 30 + '\n\n').join(parts)

        qtype = None
        if clean_input in day_kw:
            qtype = 'today'
        elif clean_input in week_kw:
            qtype = 'week'
        elif clean_input in month_kw:
            qtype = 'month'
        elif clean_input in last7_kw:
            qtype = 'last7'
        elif clean_input in last30_kw:
            qtype = 'last30'

        if qtype:
            return self._do_local_query(qtype, today_str)

        return None

    def _do_local_query(self, qtype: str, today_str: str) -> str:
        """执行本地查询并格式化结果"""
        today = datetime.strptime(today_str, '%Y-%m-%d')

        titles = {
            'today': '📅 今日统计',
            'week': '📅 本周统计',
            'month': '📅 本月统计',
            'last7': '📅 近7天统计',
            'last30': '📅 近30天统计',
        }

        is_daily = (qtype == 'today')

        if qtype == 'today':
            date_cond = 'WHERE record_date = ?'
            params = [today_str]
            days_col = '1'
        elif qtype == 'week':
            monday = today - timedelta(days=today.weekday())
            date_cond = 'WHERE record_date >= ?'
            params = [monday.strftime('%Y-%m-%d')]
            days_col = 'COUNT(DISTINCT record_date)'
        elif qtype == 'month':
            date_cond = 'WHERE record_date >= ?'
            params = [today.replace(day=1).strftime('%Y-%m-%d')]
            days_col = 'COUNT(DISTINCT record_date)'
        elif qtype == 'last7':
            date_cond = 'WHERE record_date >= ?'
            params = [(today - timedelta(days=6)).strftime('%Y-%m-%d')]
            days_col = 'COUNT(DISTINCT record_date)'
        else:  # last30
            date_cond = 'WHERE record_date >= ?'
            params = [(today - timedelta(days=29)).strftime('%Y-%m-%d')]
            days_col = 'COUNT(DISTINCT record_date)'

        sql_w_detail = f"SELECT work_name, GROUP_CONCAT(DISTINCT record_type), SUM(duration), SUM(output_count), {days_col} FROM writing_records {date_cond} GROUP BY work_name"
        sql_r_detail = f"SELECT work_name, GROUP_CONCAT(DISTINCT record_type), SUM(duration), SUM(output_count), {days_col} FROM reading_records {date_cond} GROUP BY work_name"
        sql_w_total = f"SELECT SUM(duration), SUM(output_count) FROM writing_records {date_cond}"
        sql_r_total = f"SELECT SUM(duration), SUM(output_count) FROM reading_records {date_cond}"

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute(sql_w_detail, params)
        w_detail = cursor.fetchall()
        cursor.execute(sql_r_detail, params)
        r_detail = cursor.fetchall()
        cursor.execute(sql_w_total, params)
        w_total = cursor.fetchone()
        cursor.execute(sql_r_total, params)
        r_total = cursor.fetchone()
        conn.close()

        if is_daily:
            return self._format_daily(w_total, r_total, w_detail, r_detail, titles[qtype])
        else:
            return self._format_period(w_total, r_total, w_detail, r_detail, titles[qtype])

    def _format_daily(self, w_total, r_total, w_detail, r_detail, title: str) -> str:
        """格式化每日查询结果：总量行 + 详情（全部展示，合并同类项）"""
        lines = [title, '']
        lines.append(self._build_total_line(w_total, r_total, '本日'))
        lines.append('')

        if w_detail:
            lines.append('✍️ 写作详情：')
            for item in self._sort_and_rank(w_detail, 'write'):
                lines.append(f'  {item}')
            lines.append('')

        if r_detail:
            lines.append('📖 阅读详情：')
            for item in self._sort_and_rank(r_detail, 'read'):
                lines.append(f'  {item}')

        return '\n'.join(lines)

    def _format_period(self, w_total, r_total, w_detail, r_detail, title: str) -> str:
        """格式化周期查询结果：总量行 + TOP 3（合并同类项，有作品名绝对优先）"""
        lines = [title, '']

        prefix = title.replace('📅 ', '')
        lines.append(self._build_total_line(w_total, r_total, prefix))
        lines.append('')

        if w_detail:
            lines.append('✍️ 写作 TOP 3：')
            for i, item in enumerate(self._sort_and_rank(w_detail, 'write', top_n=3), 1):
                lines.append(f'  {i}. {item}')
            lines.append('')

        if r_detail:
            lines.append('📖 阅读 TOP 3：')
            for i, item in enumerate(self._sort_and_rank(r_detail, 'read', top_n=3), 1):
                lines.append(f'  {i}. {item}')

        return '\n'.join(lines)

    def _build_total_line(self, w_total, r_total, prefix: str) -> str:
        """构建总量行。不计作品名分类，只分读写两个大类。
        格式：'本日你阅读了X小时看了X章，写了X小时X字。'"""
        w_dur = w_total[0] or 0 if w_total else 0
        w_out = w_total[1] or 0 if w_total else 0
        r_dur = r_total[0] or 0 if r_total else 0
        r_out = r_total[1] or 0 if r_total else 0

        read_str = ''
        if r_out > 0:
            read_str = f'共阅读了{int(r_out)}章'
        elif r_dur > 0:
            read_str = f'共阅读了{r_dur:.2f}小时'

        write_str = ''
        if w_out > 0:
            write_str = f'共写下了{int(w_out)}字'
        elif w_dur > 0:
            write_str = f'共写了{w_dur:.2f}小时'

        if read_str and write_str:
            return f'{prefix}你{read_str}，{write_str}。'
        elif read_str:
            return f'{prefix}你{read_str}。'
        elif write_str:
            return f'{prefix}你{write_str}。'
        else:
            return f'{prefix}暂无记录。'

    def _sort_and_rank(self, rows: list, metric_type: str, top_n: int = None) -> list:
        """
        排序并格式化详情行。
        排序规则：有作品名绝对优先 > 有 output_count 优先 > 按数值降序。
        metric_type: 'write' | 'read'
        top_n: 取前 N 名，附加 (N天) 后缀
        """
        coefficient = 2000 if metric_type == 'write' else 20

        def sort_key(row):
            work_name = row[0]
            output = row[3] or 0
            duration = row[2] or 0
            has_work = 1 if work_name else 0
            has_output = 1 if output else 0
            score = output if output else duration * coefficient
            return (has_work, has_output, score)

        sorted_rows = sorted(rows, key=sort_key, reverse=True)

        if top_n:
            sorted_rows = sorted_rows[:top_n]

        items = []
        for row in sorted_rows:
            item = self._format_detail_item(row, metric_type)
            if top_n:
                days = row[4] or 1
                item += f'（{days}天）'
            items.append(item)

        return items

    def _format_detail_item(self, row: tuple, metric_type: str) -> str:
        """
        格式化单条详情。优先用字数/章节，没有时才用时长。
        row: (work_name, record_type, duration, output_count, day_count)
        """
        work_name = row[0] or ''
        record_type = row[1] or ''
        duration = row[2] or 0
        output = row[3] or 0

        unit = '字' if metric_type == 'write' else '章'

        prefix = ''
        if work_name:
            prefix += f'《{work_name}》'
        if record_type:
            prefix += record_type

        if output:
            value = f'{int(output)} {unit}'
        elif duration:
            value = f'{duration:.2f} 小时'
        else:
            value = '0'

        return f'{prefix} {value}'

    def _parse_intent_from_markdown(self, markdown: str) -> dict:
        """解析 LLM 意图输出为 dict"""
        intent = {}
        for line in markdown.strip().split('\n'):
            line = line.strip()
            if not line or line == '---':
                continue
            if '：' in line:
                key, _, value = line.partition('：')
                intent[key.strip()] = value.strip()
        return intent

    def _validate_intent(self, intent: dict) -> dict:
        """校验并修正意图，确保所有字段都有合法值"""
        defaults = {
            '查询类型': '全部',
            '时间范围': 'this_month',
            '作品名称': '',
            '统计指标': 'all',
            '分组': 'none',
            '排序': 'desc',
            '对比模式': 'none',
            '分析主题': '数据查询',
        }

        valid_sets = {
            '查询类型': {'写', '读', '全部'},
            '统计指标': {'word_count', 'chapters', 'duration', 'count', 'all'},
            '分组': {'date', 'work', 'type', 'none'},
            '排序': {'desc', 'asc'},
            '对比模式': {'none', 'period_over_period', 'by_work', 'by_type'},
        }

        result = {}
        for key, default in defaults.items():
            val = intent.get(key, '').strip()
            if not val:
                result[key] = default
            elif key in valid_sets and val not in valid_sets[key]:
                result[key] = default
            else:
                result[key] = val

        return result

    def _build_sql_for_intent(self, intent: dict, today_str: str) -> list:
        """从意图 dict 构建 SQL 查询列表。返回 [(table_name, sql, params), ...]"""
        today = datetime.strptime(today_str, '%Y-%m-%d')
        queries = []

        # 处理多时间段（对比模式）
        time_parts = intent['时间范围'].split(';')
        all_time_ranges = [t.strip() for t in time_parts if t.strip()]

        for tr in all_time_ranges:
            date_cond, params_base = self._time_range_to_sql(tr, today)

            # 作品过滤
            if intent['作品名称']:
                date_cond += ' AND work_name = ?'
                params_base.append(intent['作品名称'])

            # 分组与排序
            grouping = intent['分组']
            order = 'DESC' if intent['排序'] == 'desc' else 'ASC'

            if grouping == 'date':
                select = 'record_date, SUM(duration), SUM(output_count), COUNT(*)'
                group_clause = ' GROUP BY record_date ORDER BY record_date ' + order
            elif grouping == 'work':
                select = 'work_name, SUM(duration), SUM(output_count), COUNT(*)'
                group_clause = ' GROUP BY work_name ORDER BY SUM(output_count) ' + order
            elif grouping == 'type':
                select = 'record_type, SUM(duration), SUM(output_count), COUNT(*)'
                group_clause = ' GROUP BY record_type ORDER BY SUM(output_count) ' + order
            else:
                select = 'SUM(duration), SUM(output_count), COUNT(*)'
                group_clause = ''

            # 选表
            qtype = intent['查询类型']
            tables = []
            if qtype in ('写', '全部'):
                tables.append('writing_records')
            if qtype in ('读', '全部'):
                tables.append('reading_records')

            for table in tables:
                sql = f"SELECT {select} FROM {table} {date_cond} {group_clause}"
                queries.append((f'{tr}:{table}', sql, params_base.copy()))

        return queries

    def _time_range_to_sql(self, tr: str, today) -> tuple:
        """时间范围字符串 → (WHERE clause, params list)"""
        if tr == 'today':
            return ('WHERE record_date = ?', [today.strftime('%Y-%m-%d')])
        elif tr == 'yesterday':
            y = today - timedelta(days=1)
            return ('WHERE record_date = ?', [y.strftime('%Y-%m-%d')])
        elif tr == 'this_week':
            monday = today - timedelta(days=today.weekday())
            return ('WHERE record_date >= ?', [monday.strftime('%Y-%m-%d')])
        elif tr == 'this_month':
            return ('WHERE record_date >= ?', [today.replace(day=1).strftime('%Y-%m-%d')])
        elif tr == 'this_year':
            return ('WHERE record_date >= ?', [today.replace(month=1, day=1).strftime('%Y-%m-%d')])
        elif tr.startswith('last_'):
            match = re.match(r'last_(\d+)_(days?|weeks?|months?)', tr)
            if match:
                n = int(match.group(1))
                unit = match.group(2)
                if unit.startswith('day'):
                    start = today - timedelta(days=n - 1)
                elif unit.startswith('week'):
                    start = today - timedelta(weeks=n)
                else:
                    # 近 N 月：简单用 30*n 天近似
                    start = today - timedelta(days=30 * n)
                return ('WHERE record_date >= ?', [start.strftime('%Y-%m-%d')])
        elif tr == 'all':
            return ('', [])

        # 兜底：本月
        return ('WHERE record_date >= ?', [today.replace(day=1).strftime('%Y-%m-%d')])

    async def _call_llm_for_query(self, event: AstrMessageEvent, system_prompt: str, user_prompt: str) -> str:
        """通用的 LLM 调用（查询路径）。返回响应文本或 None。"""
        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                return None

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=f"{system_prompt}\n\n{user_prompt}",
            )

            if llm_resp and hasattr(llm_resp, 'completion_text') and llm_resp.completion_text.strip():
                return llm_resp.completion_text.strip()
        except Exception as e:
            logger.warning(f"LLM 查询调用失败：{e}")

        return None

    def _format_data_for_llm(self, data: dict) -> str:
        """将查询数据转为 LLM 可读的文本"""
        parts = []
        for label, rows in data.items():
            parts.append(f'【{label}】')
            if rows:
                for row in rows:
                    parts.append(', '.join(str(v) for v in row))
            else:
                parts.append('（无数据）')
        return '\n'.join(parts)

    def _format_response_fallback(self, data: dict, intent: dict) -> str:
        """本地兜底格式化查询结果"""
        lines = ['📊 查询结果']
        theme = intent.get('分析主题', '')
        if theme and theme != '数据查询':
            lines.append(f'主题：{theme}')
        lines.append('')

        for label, rows in data.items():
            tag = '✍️' if 'writing' in label else '📖'
            name = '写作' if 'writing' in label else '阅读'
            lines.append(f'{tag} {name}（{label}）：')
            if not rows:
                lines.append('  暂无数据')
            else:
                for row in rows:
                    lines.append('  ' + ', '.join(str(v) for v in row))
            lines.append('')

        return '\n'.join(lines)

    # ──────────── 命令入口 ────────────

    @filter.command("查询", alias={"query"})
    async def query(self, event: AstrMessageEvent):
        """
        查询活动记录。简单查询走本地白名单，复杂查询走 LLM。
        """
        message = event.message_str.strip()

        if message.startswith('查询'):
            content = message[2:].strip()
        elif message.startswith('query'):
            content = message[5:].strip()
        else:
            content = message

        # 1. 尝试本地白名单
        local_result = self._query_local(content)
        if local_result is not None:
            yield event.plain_result(local_result)
            return

        # 2. 复杂查询：LLM 路径
        intent_rule = self._read_rule('query_intent_rule.md')
        if not intent_rule:
            yield event.plain_result("⚠️ 查询规则文件 query_intent_rule.md 未找到，请检查配置。")
            return

        # 2a. LLM 提取意图
        llm_intent_md = await self._call_llm_for_query(
            event,
            system_prompt=(
                "你是 LifeOS 查询解析助手。请严格按照下方规则，"
                "将用户的查询转换为 key:value 格式。\n"
                "只输出 key:value，以 --- 结尾，不要添加任何额外说明。"
            ),
            user_prompt=(
                f"## 查询意图解析规则\n\n"
                f"{intent_rule}\n\n"
                f"## 用户查询\n\n"
                f"{content}\n\n"
                f"请根据上述规则转换。"
            ),
        )

        if not llm_intent_md:
            yield event.plain_result(
                "⚠️ 查询意图解析失败，请尝试更简洁的表述。\n\n"
                "支持的关键词：今天 / 本周 / 本月 / 近7天 / 近30天"
            )
            return

        # 2b. 解析并校验意图
        intent = self._parse_intent_from_markdown(llm_intent_md)
        intent = self._validate_intent(intent)
        logger.info(f"查询意图：{intent}")

        # 2c. 构建并执行查询
        today_str = datetime.now().strftime('%Y-%m-%d')
        queries = self._build_sql_for_intent(intent, today_str)

        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        data = {}
        for label, sql, params in queries:
            cursor.execute(sql, params)
            data[label] = cursor.fetchall()
        conn.close()

        # 2d. 格式化回复（LLM 优先，本地兜底）
        format_rule = self._read_rule('query_format_rule.md')
        if format_rule:
            data_text = self._format_data_for_llm(data)
            response = await self._call_llm_for_query(
                event,
                system_prompt="你是 LifeOS 数据分析助手。请严格按照下方规则生成回复。",
                user_prompt=(
                    f"{format_rule}\n\n"
                    f"<用户问题>\n{content}\n</用户问题>\n\n"
                    f"<分析主题>\n{intent.get('分析主题', '数据查询')}\n</分析主题>\n\n"
                    f"<查询结果>\n{data_text}\n</查询结果>\n\n"
                    f"请生成回复。"
                ),
            )
        else:
            response = None

        if not response:
            response = self._format_response_fallback(data, intent)

        yield event.plain_result(response)

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

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """拦截非命令消息，处理待确认的记录"""
        message = event.message_str.strip()

        cmd_prefixes = ('记录', '查询', 'query', 'helloworld', 'hello', 'hi',
                        '测试', 't', 'test', '/')
        if any(message.startswith(p) for p in cmd_prefixes):
            return

        user_key = event.unified_msg_origin
        self._cleanup_pending()

        if user_key not in self.pending:
            return

        await self._process_confirmation(event, user_key, message)

    @filter.command("记录")
    async def record(self, event: AstrMessageEvent):
        """
        记录活动：完美匹配走本地 → 歧义走 LLM（含追问确认）
        """
        message = event.message_str.strip()
        content = message[len("记录"):].strip()
        user_key = event.unified_msg_origin

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

        self._cleanup_pending()

        # 有待确认记录 → 视为对追问的回复
        if user_key in self.pending:
            await self._process_confirmation(event, user_key, content)
            return

        rule_text = self._read_rule('record_rule.md')
        if not rule_text:
            yield event.plain_result("⚠️ 规则文件 record_rule.md 未找到，请检查配置。")
            return

        now = datetime.now().strftime('%H:%M')
        today = datetime.now().strftime('%Y-%m-%d')

        # 1. 拆分活动
        activities = self._split_activities(content)

        # 2. 尝试本地完美匹配
        all_clear = True
        local_results = []
        for activity in activities:
            result = self._try_perfect_match(activity, now, today)
            if result is None:
                all_clear = False
                break
            md, record = result
            if '---ERROR---' in md:
                all_clear = False
                break
            local_results.append((md, record, activity))

        if all_clear and local_results:
            entries = [r[0] for r in local_results]
            records_list = [r[1] for r in local_results]
            raw_texts = [r[2] for r in local_results]
            self._write_records(entries, ['本地'] * len(entries), raw_texts)
            db_count = self._write_to_db(records_list)
            yield event.plain_result(self._format_record_response(records_list))
            return

        # 3. 有歧义 → LLM 路径
        similar_works = self._get_similar_works_context(content)
        llm_output = await self._call_llm_for_record(event, rule_text, content, similar_works)

        if not llm_output:
            # LLM 不可用，尝试本地兜底
            if local_results:
                entries = [r[0] for r in local_results]
                records_list = [r[1] for r in local_results]
                raw_texts = [r[2] for r in local_results]
                self._write_records(entries, ['本地(兜底)'] * len(entries), raw_texts)
                db_count = self._write_to_db(records_list)
                yield event.plain_result(self._format_record_response(records_list))
                return
            yield event.plain_result("⚠️ LLM 不可用且无法本地解析，请尝试更简洁的表述。")
            return

        await self._handle_llm_record_output(event, user_key, content, llm_output)

    async def terminate(self):
        """插件卸载时调用"""
        logger.info("LifeOS 插件已卸载")
