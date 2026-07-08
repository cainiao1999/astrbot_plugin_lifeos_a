# LifeOS Plugin

LifeOS 是一个 AstrBot 插件。

目标不是做聊天机器人，而是作为 **LifeOS 的数据采集层(Data Collector)**。

职责非常简单：

> 接收用户自然语言 → 解析 → 写入 Markdown 日志 → 写入 SQLite 数据库

后续所有统计、分析、Agent，都只读取数据库，而不是解析 Markdown。

---

# 当前目录结构

```
astrbot_plugin_lifeos/

│
├── main.py                 # AstrBot 插件入口（尽量保持精简）
│
├── parser.py               # 记录解析（LLM + 本地降级）
│
├── storage.py              # Markdown / SQLite 写入
│
├── utils.py                # 通用工具函数
│
├── rules/
│   └── record_rule.md
│
├── metadata.yaml
├── _conf_schema.json
├── README.md
└── LICENSE
```

---

# 各文件职责

---

## main.py

插件入口。

负责：

- 注册插件
- 注册命令
- 调用 parser
- 调用 storage
- 返回消息

原则：

> **不要在这里写业务逻辑。**

以后如果 main.py 超过 300 行，说明又写乱了。

---

## parser.py

负责：

把用户一句自然语言

例如：

```
写了《嘉豪》正文2小时1800字
```

转换成统一的数据。

流程：

```
用户输入

↓

LLM解析
（优先）

↓

失败

↓

本地规则解析

↓

得到统一结构
```

例如：

```python
{
    "action": "writing",
    "date": "2026-07-08",
    "time": "21:30",
    "duration": 2,
    "output_count": 1800,
    "work_name": "嘉豪",
    "record_type": "正文",
    "remark": ""
}
```

parser 只负责解析。

**绝不能写数据库。**

---

## storage.py

负责：

保存数据。

包括：

- 写 Markdown
- 写 SQLite

以后如果：

增加 MySQL

增加 PostgreSQL

增加 HTTP API

全部只改这里。

parser 不需要修改。

---

## utils.py

放各种公共函数。

例如：

```
获取当前时间

Markdown字段解析

正则工具

文本清洗

数字转换

日志工具
```

特点：

任何地方都可以 import。

---

# 数据流

整个插件的数据流固定如下：

```
QQ消息

↓

main.py

↓

parser.py

↓

统一数据(dict)

↓

storage.py

↓

Markdown

+

SQLite
```

以后所有功能都遵循这个流程。

不要跨层。

例如：

❌ parser 写数据库

❌ storage 调 LLM

都属于错误设计。

---

# Markdown 的定位

Markdown：

仅作为日志。

方便：

- 查看历史
- Obsidian
- Git版本管理
- 手动修改
- AI阅读

不参与统计。

---

# SQLite 的定位

SQLite：

唯一统计来源。

所有：

日报

周报

排行榜

分析

Agent

全部读取 SQLite。

不要统计 Markdown。

---

# Config

配置目录：

```
/data/lifeos/Config
```

默认：

```
record_rule.md
```

用户可以自行修改。

插件升级不会覆盖。

---

# Data

Markdown 日志：

```
/data/lifeos/Data

2026-07-08.md
2026-07-09.md
...
```

每天一个文件。

---

# Database

数据库：

```
/data/lifeos/Database/lifeos.db
```

目前包含：

```
writing_records

reading_records
```

后续可以继续增加：

```
habit_records

expense_records

exercise_records
```

互不影响。

---

# Logs

插件日志。

```
/data/lifeos/Logs
```

以后：

错误日志

运行日志

Agent日志

全部放这里。

---

# 新增功能应该改哪里？

## 新增一种记录类型

例如：

运动

修改：

```
parser.py
```

数据库增加：

```
schema.sql
```

storage 基本不用改。

---

## 修改 AI 提示词

修改：

```
Config/record_rule.md
```

无需修改 Python。

---

## 修改数据库

修改：

```
schema.sql
```

以及：

```
storage.py
```

---

## 修改 Markdown 格式

修改：

```
storage.py
```

---

## 新增日报命令

例如：

```
/今日

/本周

/本月
```

修改：

```
main.py
```

调用：

```
storage.py
```

查询数据库即可。

---

# 设计原则

整个项目遵循四条原则。

---

## ① Markdown 与 SQLite 双存储

Markdown：

日志。

SQLite：

统计。

两者职责不同。

---

## ② Markdown 永远允许人工修改

数据库保持结构化。

Markdown保持可读。

---

## ③ parser 不负责存储

parser：

只解析。

storage：

只保存。

---

## ④ main.py 永远保持简单

main 应该像这样：

```
收到消息

↓

parser

↓

storage

↓

回复用户
```

除此之外，不做其它事情。

---

# 当前开发路线

目前：

✅ Markdown记录

✅ SQLite记录

下一阶段：

- 今日统计
- 本周统计
- 月统计
- 写作排行榜
- 阅读排行榜
- Agent 自动分析

最终目标：

LifeOS 成为自己的个人数据中枢（Personal Life Operating System）。
