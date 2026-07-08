"""
LifeOS Bootstrap

负责插件启动时的一次性初始化工作。

职责：
1. 创建目录
2. 创建数据库目录
3. 创建配置目录
4. 创建日志目录
5. 拷贝默认规则文件

除初始化外，不负责任何业务逻辑。
"""

from pathlib import Path
import shutil

from astrbot.api import logger


class Bootstrap:
    def __init__(self, config: dict):
        self.config = config or {}

        self.data_path = Path(
            self.config.get("data_path", "/data/lifeos/Data")
        )

        self.config_path = Path(
            self.config.get("config_path", "/data/lifeos/Config")
        )

        self.logs_path = Path(
            self.config.get("logs_path", "/data/lifeos/Logs")
        )

        self.database_path = Path(
            self.config.get("database_path", "/data/lifeos/Database")
        )

    async def initialize(self):
        """
        LifeOS 初始化入口
        """

        self._create_directories()
        self._copy_default_rules()

        logger.info("LifeOS 初始化完成")
        logger.info(f"Data      : {self.data_path}")
        logger.info(f"Config    : {self.config_path}")
        logger.info(f"Logs      : {self.logs_path}")
        logger.info(f"Database  : {self.database_path}")

    # --------------------------------------------------

    def _create_directories(self):
        """
        创建目录结构
        """

        self.data_path.mkdir(parents=True, exist_ok=True)
        self.config_path.mkdir(parents=True, exist_ok=True)
        self.logs_path.mkdir(parents=True, exist_ok=True)
        self.database_path.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------

    def _copy_default_rules(self):
        """
        首次运行复制默认规则文件
        """

        default_rules = Path(__file__).parent / "rules"

        if not default_rules.exists():
            logger.warning(f"默认规则目录不存在：{default_rules}")
            return

        for file in default_rules.iterdir():

            if file.suffix != ".md":
                continue

            target = self.config_path / file.name

            if target.exists():
                continue

            shutil.copy2(file, target)

            logger.info(f"已复制规则：{file.name}")

    # --------------------------------------------------

    def read_rule(self, filename: str) -> str:
        """
        读取配置目录中的规则文件
        """

        path = self.config_path / filename

        if not path.exists():
            logger.error(f"规则不存在：{path}")
            return ""

        return path.read_text(encoding="utf-8")
