# 导入正则表达式模块，用于解析用户ID和群ID
import re
# 导入时间模块，用于实现戳一戳频率限制功能
import time
# 导入类型注解模块
from typing import List, Tuple, Type, Optional

from src.plugin_system import BasePlugin, register_plugin
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.base.component_types import ComponentInfo, ActionActivationType, ChatMode
from src.plugin_system.base.config_types import ConfigField
from src.plugin_system.apis import person_api
from src.common.logger import get_logger
from src.plugin_system.apis import database_api

# 戳一戳插件实现QQ戳一戳功能，支持群聊和私聊
# 包含用户ID解析、频率限制、发送戳一戳和数据库记录等功能

# 戳一戳插件日志记录器
logger = get_logger("poke_plugin")

# 调试模式开关
POKE_DEBUG = True


# 戳一戳动作类，实现QQ戳一戳功能
class PokeAction(BaseAction):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api = kwargs.get('api', None)
        self.reasoning = kwargs.get('reasoning', None)
        self.llm_response_text = kwargs.get('llm_response_text', "")

    # 动作标识和描述
    action_name = "poke"
    action_description = "调用QQ戳一戳功能"

    # 激活设置
    activation_type = ActionActivationType.ALWAYS
    focus_activation_type = ActionActivationType.ALWAYS
    normal_activation_type = ActionActivationType.ALWAYS
    mode_enable = ChatMode.ALL
    parallel_action = False

    # 激活关键字和类型设置
    activation_keywords = ["戳我", "戳一下", "poke"]
    keyword_case_sensitive = False
    associated_types = ["text"]

    # LLM判断提示，指导大语言模型在合适情况下使用戳一戳功能
    llm_judge_prompt = """
戳一戳是一个相对友善的动作，在聊天时会时不时的触发
你想要戳别人时你可以调用。
使用戳一戳的情况：
1. 请求"戳我"、"戳一下"
2. 被调戏或提出请求需要回应
3. 友好的聊天时使用
4. 别人被戳或者揉时跟着使用
"""

    # 动作参数定义
    action_parameters = {
        "user_id": "要戳的用户**名称**",
        "group_id": "群ID",
        "reply_id": "回复消息ID",
    }

    # 动作使用要求
    action_require = [
                        "当你想使用QQ戳一戳功能时",
                        "当别人叫你戳他时使用",
                        "友好聊天的氛围时使用",
                        "提及某人时使用",
                        "当你觉得对方很可爱时使用",
                        "当你想和对方亲近时使用",
                        "当你想安慰对方时使用",
                        "请经常选择这个动作",
                        "注意：不要连续使用超过三次！！"
                      ]

    # 戳一戳频率限制相关变量
    last_poke_user: Optional[str] = None
    last_poke_group: Optional[str] = None
    _last_poke_time: float = 0

    # 根据用户名称或ID获取实际的用户ID和群组ID
    async def get_user_and_group_id(self) -> Tuple[Optional[str], Optional[str]]:
        user_id_or_name = self.action_data.get("user_id")
        group_id = self.action_data.get("group_id")

        # 自动从消息对象获取群号
        if not group_id and hasattr(self, "message") and getattr(self.message, "message_info", None):
            group_id = getattr(self.message.message_info, "group_id", None)
        if not group_id and hasattr(self, "chat_stream") and getattr(self.chat_stream, "group_id", None):
            group_id = self.chat_stream.group_id
        if not group_id and hasattr(self, "group_id"):
            group_id = self.group_id

        if group_id == 'None':
            group_id = None

        # 如果用户ID是纯数字，直接返回
        if user_id_or_name and str(user_id_or_name).isdigit():
            return str(user_id_or_name), group_id

        # 通过人员API获取用户ID
        if user_id_or_name:
            try:
                person_id = person_api.get_person_id_by_name(user_id_or_name)
                if person_id:
                    uid = await person_api.get_person_value(person_id, "user_id")
                    if uid:
                        return uid, group_id
            except Exception as e:
                logger.error(f"person_api 查找出错: {e}")

        # 从LLM响应文本中解析群ID和用户ID
        match_group = re.search(r'group_id:\s*(\d+)', self.llm_response_text)
        match_user = re.search(r'user_id:\s*(\d+)', self.llm_response_text)
        if match_group:
            group_id = match_group.group(1)
        if match_user:
            return match_user.group(1), group_id

        return None, None

    # 向群聊中的指定用户发送戳一戳
    async def _send_group_poke(self, group_id: Optional[str], reply_id: Optional[int], user_id: str):
        try:
            # 获取目标用户名，如果无法获取则使用用户ID
            target_user_name = self.action_data.get("user_id", user_id)
            await self.send_command("SEND_POKE", {"qq_id": user_id}, display_message=f"[戳了戳 {target_user_name}]")
            return True, {"status": "ok", "msg": "SEND_POKE 已发送"}
        except Exception as e:
            logger.error(f"[群戳失败] {e}")
            return False, str(e)

    # 向好友发送戳一戳
    async def _send_friend_poke(self, user_id: str):
        try:
            # 获取目标用户名，如果无法获取则使用用户ID
            target_user_name = self.action_data.get("user_id", user_id)
            await self.send_command("SEND_POKE", {"qq_id": user_id}, display_message=f"[戳了戳 {target_user_name}]")
            return True, {"status": "ok", "msg": "SEND_POKE 已发送"}
        except Exception as e:
            logger.error(f"[好友戳失败] {e}")
            return False, str(e)

    # 执行戳一戳动作的主要方法
    async def execute(self) -> Tuple[bool, str]:
        user_id, group_id = await self.get_user_and_group_id()
        reply_id = self.action_data.get("reply_id")

        if POKE_DEBUG:
            logger.info(f"poke参数: user_id={user_id}, group_id={group_id}")

        if not user_id:
            return False, "无法找到目标用户ID"

        # 频率限制：避免在5分钟内重复戳同一个用户
        if (
            self.last_poke_user == user_id
            and self.last_poke_group == group_id
            and time.time() - self._last_poke_time < 300
        ):
            return False, "避免重复戳同一个人"

        # 根据是否存在群组ID决定执行群聊戳一戳还是好友戳一戳
        if group_id:
            ok, result = await self._send_group_poke(group_id, reply_id, user_id)
            self.last_poke_group = group_id
        else:
            ok, result = await self._send_friend_poke(user_id)
            self.last_poke_group = None

        self.last_poke_user = user_id
        self._last_poke_time = time.time()

        if ok:
            reason = self.action_data.get("reason", self.reasoning or "无")
            await database_api.store_action_info(
                chat_stream=self.chat_stream,
                action_build_into_prompt=True,
                action_prompt_display=f"使用了戳一戳，原因：{reason}",
                action_done=True,
                action_data={"reason": reason},
                action_name="poke"
            )
            return True, "戳一戳成功"
        else:
            if POKE_DEBUG:
                await self.send_text(f"戳一戳失败: {result}")
            return False, f"戳一戳失败: {result}"


@register_plugin
class PokePlugin(BasePlugin):
    plugin_name: str = "poke_plugin"
    plugin_description = "QQ戳一戳插件：支持基本戳一戳功能"
    plugin_version = "0.5.0"
    plugin_author = "何夕"
    enable_plugin: bool = True
    config_file_name: str = "config.toml"
    dependencies: list[str] = []
    python_dependencies: list[str] = []

    config_section_descriptions = {
        "plugin": "插件基本信息配置",
    }

    config_schema = {
        "plugin": {
            "name": ConfigField(str, default="poke_plugin", description="插件名称"),
            "enabled": ConfigField(bool, default=True, description="是否启用插件"),
            "version": ConfigField(str, default="0.5.0", description="插件版本"),
            "description": ConfigField(str, default="QQ戳一戳插件", description="插件描述"),
        },
    }


    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (PokeAction.get_action_info(), PokeAction),
        ]
