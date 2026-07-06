from pathlib import Path
from datetime import datetime

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
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        """插件初始化"""

    # ----------------------------
    # HelloWorld 示例
    # ----------------------------
    @filter.command("helloworld", alias={"hello", "hi"})
    async def helloworld(self, event: AstrMessageEvent):
        """HelloWorld 测试"""

        user_name = event.get_sender_name()
        message_str = event.message_str

        logger.info(event.get_messages())

        yield event.plain_result(
            f"Hello大作家，{user_name}，你发了 {message_str}!"
        )

    # ----------------------------
    # Test 示例
    # ----------------------------
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

    # ----------------------------
    # 记录
    # ----------------------------
    @filter.command("记录")
    async def record(self, event: AstrMessageEvent):
        """
        将输入内容追加写入：
        /data/lifeos/Data/testYYYYMMDD.md
        """

        message = event.message_str.strip()

        # 去掉命令"记录"
        content = message[len("记录"):].strip()

        if not content:
            yield event.plain_result(
                "请输入要记录的内容。\n例如：\n/记录 7月6日 写了一句话"
            )
            return

        # LifeOS 数据目录
        data_dir = Path("/data/lifeos/Data")
        data_dir.mkdir(parents=True, exist_ok=True)

        # 今日日期，例如：20260706
        today = datetime.now().strftime("%Y%m%d")

        # 文件名：test20260706.md
        file_path = data_dir / f"test{today}.md"

        # 追加写入
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(content + "\n")

        logger.info(f"LifeOS 已记录：{content}")
        logger.info(f"写入文件：{file_path}")

        yield event.plain_result("✅ 已成功记录。")

    async def terminate(self):
        """插件卸载时调用"""
