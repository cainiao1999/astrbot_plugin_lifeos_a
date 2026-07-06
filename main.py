from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

@register("helloworld", "YourName", "一个简单的 Hello World 插件", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    # 注册指令的装饰器。指令名为 helloworld。注册成功后，发送 `/helloworld` 就会触发这个指令，并回复 `你好, {user_name}!`
    @filter.command("helloworld", alias={'hello', 'hi'})
    async def helloworld(self, event: AstrMessageEvent):
        """这是一个 hello world 指令""" # 这是 handler 的描述，将会被解析方便用户了解插件内容。建议填写。
        user_name = event.get_sender_name()
        message_str = event.message_str # 用户发的纯文本消息字符串
        message_chain = event.get_messages() # 用户所发的消息的消息链 # from astrbot.api.message_components import *
        logger.info(message_chain)
        yield event.plain_result(f"Hello大作家, {user_name}, 你发了 {message_str}!") # 发送一条纯文本消息
    # 注册一个 test 指令
    @filter.command("test", alias={'测试', 't'})
    async def test(self, event: AstrMessageEvent):
        """这是一个测试指令"""
        user_name = event.get_sender_name()
        message_str = event.message_str # 用户发的纯文本消息字符串
        message_chain = event.get_messages() # 用户所发的消息的消息链 # from astrbot.api.message_components import *
        logger.info(message_chain)
        yield event.plain_result(f"Hello大作家e3,你成功的test! {user_name}, 你发了 {message_str}!")    
    '''
    math
    ├── calc
    │   ├── add (a(int),b(int),)
    │   ├── sub (a(int),b(int),)
    │   ├── help (无参数指令)
    '''

    @filter.command_group("math")
    def math():
        pass

    @math.group("calc") # 请注意，这里是 group，而不是 command_group
    def calc():
        pass

    @calc.command("add")
    async def add(self, event: AstrMessageEvent, a: int, b: int):
        yield event.plain_result(f"结果是: {a + b}")

    @calc.command("sub")
    async def sub(self, event: AstrMessageEvent, a: int, b: int):
        yield event.plain_result(f"结果是: {a - b}")

    @calc.command("help")
    async def calc_help(self, event: AstrMessageEvent):
        # /math calc help
        yield event.plain_result("这是一个计算器插件，拥有 add, sub 指令。")
    
    @filter.command("help", alias={'帮助', 'helpme'})
    async def help(self, event: AstrMessageEvent):
        yield event.plain_result("这是一个计算器插件，拥有 add, sub 指令。")
    
    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
