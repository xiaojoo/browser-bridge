"""Web LLM Provider 注册表。

每个 Provider 定义: 站点、输入框选择器、登录探测、捕获方式与新建对话文案。
捕获方式:
  stream - 网络层 fetch 流式解析(DeepSeek 主通道, DOM 兜底)
  dom    - DOM 快照整段替换(ChatGPT 当前主通道; 界面结构稳定时可换 stream)
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    url: str
    short: str                    # UI 头像/标签缩写
    composer_selectors: tuple = ()   # 依次尝试
    send_selectors: tuple = ()       # 站点的"发送"键(判断"现在能不能发"), 空 = 认不出就放行
    capture_mode: str = "stream"
    cookie_accept: bool = False   # 是否有"接受全部 Cookie"按钮可点
    new_chat_selectors: tuple = ()   # 优先按选择器点站内"新对话"(比文案匹配稳)
    new_chat_names: tuple = ("New chat", "新对话")
    conversation_pattern: str = ""  # URL 中会话 id 的路径段, 如 "/chat/" 或 "/c/"
    snapshot_selector: str = ".ds-markdown, [class*=\"ds-markdown\"]"  # DOM 快照正文节点
    conv_selector: str = ""          # 读回整段对话用: 每条消息的选择器(空 = 该站点不支持)


PROVIDERS: list[Provider] = [
    Provider(
        id="deepseek",
        name="DeepSeek Web",
        url="https://chat.deepseek.com/",
        short="DS",
        composer_selectors=("#chat-input", 'div[contenteditable="true"]', "textarea"),
        capture_mode="stream",
        cookie_accept=True,
        new_chat_selectors=('[data-testid="new-chat-button"]',
                            'a[href="/"][aria-label]',
                            '.sidebar a[href="/"]'),
        new_chat_names=("新对话", "新建对话", "New chat"),
        conversation_pattern="/chat/",
    ),
    Provider(
        id="chatgpt",
        name="ChatGPT Web",
        url="https://chatgpt.com/",
        short="GPT",
        composer_selectors=("#prompt-textarea", "div#prompt-textarea",
                            'div[contenteditable="true"]', "textarea"),
        send_selectors=('button[data-testid="send-button"]',
                        'button#composer-submit-button',
                        'button[aria-label="Send prompt"]',
                        'button[aria-label*="Send message"]'),
        capture_mode="dom",          # 当前用 DOM 快照; 后续可切 stream
        cookie_accept=False,
        new_chat_selectors=('[data-testid="create-new-chat-button"]',
                            'a[aria-label="New chat"]',
                            'a[aria-label="新对话"]',
                            '#sidebar a[href="/"]',
                            'a[href="/"]'),
        new_chat_names=("New chat", "新对话", "New Chat"),
        conversation_pattern="/c/",
        snapshot_selector='[data-message-author-role="assistant"] .markdown, '
                          '[data-message-author-role="assistant"]',
        conv_selector='[data-message-author-role]',       # 用户/助手每条都带这个属性
    ),
]

_BY_ID = {p.id: p for p in PROVIDERS}
DEFAULT_ID = "deepseek"


def get(provider_id: str | None) -> Provider:
    return _BY_ID.get(provider_id or "", _BY_ID[DEFAULT_ID])


def info(p: Provider) -> dict:
    return {"id": p.id, "name": p.name, "url": p.url, "short": p.short,
            "capture_mode": p.capture_mode, "conversation_pattern": p.conversation_pattern}
