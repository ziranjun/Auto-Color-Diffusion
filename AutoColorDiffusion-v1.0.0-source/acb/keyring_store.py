# -*- coding: utf-8 -*-
"""API key 管理。

硬约束 #3（逐条落地）：
    - API key 禁止写入任何配置文件或代码；
    - 优先使用 keyring 存入系统密钥链（Windows = 凭据管理器）；
    - 界面输入框仅在 keyring 中无值时弹出；
    - 回填时掩码显示 sk-****xxxx；
    - 不得自行实现 AES 等对称加密落文件；
    - 若 keyring 不可用，应报错提示安装，或仅在本次会话内存中持有（重启需重输）。

实现：
    KeyStore 封装三类来源，按优先级：
        1. keyring（Windows 凭据管理器）—— 持久、加密、由操作系统保护；
        2. 进程内内存字典 —— 本次会话有效，重启即失效（keyring 不可用时的降级）；
        3. 环境变量（名为 key_env 的值）—— 兼容 CI / 已有 .env 工作流的用户。
    注意优先级设计：内存优先于环境变量，因为用户在界面上刚输入的 key
    应当立刻覆盖旧的环境变量值，否则用户会遇到"改了不生效"的困惑。
"""

from __future__ import annotations

import os

from . import APP_ID
from .errors import KeyringUnavailableError
from .logging_setup import get_logger

log = get_logger("keyring")

# keyring 的"服务名"。与 %APPDATA% 文件夹名保持一致（acb.paths.APP_NAME），
# 让用户在凭据管理器里一眼能认出这是哪个程序的条目。
# 真值来自 acb/__init__.py::APP_ID —— 改名时只改那一处，这里自动跟随。
SERVICE_NAME = APP_ID

# 掩码前后保留的字符数。
# 依据：前 3 位能显示提供商前缀（如 "sk-"、"sk-ant-"、"glm-"），
#       便于用户确认自己填的是哪家的 key；后 4 位是业界惯例的"可辨识后缀"
#       （各家控制台通常也只显示末 4 位），便于与列表中的其它 key 区分。
MASK_PREFIX_LEN = 3
MASK_SUFFIX_LEN = 4

# 粘贴密钥时常见的"包裹物"。都是真实会遇到的：
#   · 从各家控制台/网页复制时带上首尾引号或反引号；
#   · 从 curl 示例里连 "Bearer " 一起复制；
#   · 行尾的换行/不换行空格（U+00A0）、全角空格（U+3000）。
# 这些字符混进 Authorization 头 → 401，用户看到的是"密钥无效"，
# 但真正原因是他自己复制多了东西 —— 所以必须由程序清掉并说清楚。
_STRIPPABLE_CHARS = " \t\r\n\u00a0\u3000\u200b\u200e\u200f\ufeff"
_QUOTE_PAIRS = (("\"", "\""), ("'", "'"), ("`", "`"),
                ("\u201c", "\u201d"), ("\u2018", "\u2019"),
                ("\uff02", "\uff02"), ("\u300c", "\u300d"))


def normalize_api_key(raw: str) -> tuple[str, list[str]]:
    """清理粘贴带入的杂质，返回 (规范化后的密钥, 做了什么修改的说明)。

    只做"能确定是粘贴产物"的清理，不做"猜"：
      · 去掉首尾空白（含全角空格/换行/零宽字符）；
      · 去掉首尾成对引号（ASCII / 中文引号 / 反引号）；
      · 去掉前缀 "Bearer "（大小写不敏感）。

    内部的空白或非 ASCII 字符**不猜**，而是报错：
    所有内置服务商的密钥都是 ASCII 字母数字加常见符号，出现全角字符或中间空格
    一定是复制错了。此处宁可拦住并让用户重拷，也不要把一个 401 留到后面 ——
    那种"密钥无效"的报错会让人往余额/权限上去找，白跑很久。
    """
    notes: list[str] = []
    value = raw
    trimmed = value.strip(_STRIPPABLE_CHARS)
    if trimmed != value:
        notes.append("已去掉首尾空白字符")
    value = trimmed

    changed = True
    while changed and len(value) >= 2:
        changed = False
        for opener, closer in _QUOTE_PAIRS:
            if value.startswith(opener) and value.endswith(closer):
                value = value[len(opener): len(value) - len(closer)].strip(_STRIPPABLE_CHARS)
                notes.append("已去掉包裹的引号")
                changed = True
                break

    if value[:7].lower() == "bearer ":
        value = value[7:].strip(_STRIPPABLE_CHARS)
        notes.append("已去掉多余的「Bearer 」前缀")

    return value, notes


def key_problem(secret: str) -> str | None:
    """检查"看起来不像密钥"的情况，返回给用户的中文原因（没问题返回 None）。

    这里只拦截**一定是复制错误**的形状，不对"像哪家的密钥"做任何判断
    （见 config.detect_provider_from_key：宁可认不出，也不猜错）。
    """
    if not secret:
        return "密钥为空。"
    bad = [(i, ch) for i, ch in enumerate(secret) if ord(ch) > 0x7F]
    if bad:
        i, ch = bad[0]
        return (
            f"第 {i + 1} 个字符是「{ch}」（全角/非 ASCII，U+{ord(ch):04X}）——"
            "密钥里不可能出现这种字符，多半是复制粘贴时带进来的。请重新复制一次。"
        )
    for i, ch in enumerate(secret):
        if ch in " \t\r\n\u00a0\u3000":
            return (
                f"第 {i + 1} 个字符是空白符 —— 密钥中间不应有空格或换行。"
                "常见原因：复制时把两行/两个字段一起复制了。"
            )
    return None


def mask(secret: str) -> str:
    """把密钥变成可安全打印的掩码形式，例如 "sk-****a1b2"。

    对短字符串做整体掩码，避免"前 3 后 4"重叠导致泄漏全部字符。
    要求 len 至少满足 prefix + suffix + 1 才做前后保留。
    """
    if not secret:
        return "(空)"
    if len(secret) <= MASK_PREFIX_LEN + MASK_SUFFIX_LEN:
        return "*" * len(secret)
    return f"{secret[:MASK_PREFIX_LEN]}****{secret[-MASK_SUFFIX_LEN:]}"


class KeyStore:
    """密钥存取门面。

    生命周期：整个进程共用一个实例（由界面/CLI 创建后传入各模块），
    这样"内存降级"的 key 才能在整个会话内被后续请求复用。
    """

    def __init__(self, service: str = SERVICE_NAME) -> None:
        self._service = service
        # 内存降级存储：keyring 不可用时使用。绝不落盘。
        self._memory: dict[str, str] = {}
        self._keyring_module = None
        self._keyring_error: str | None = None
        self._probe_keyring()

    # --- keyring 可用性探测 --------------------------------------------------

    def _probe_keyring(self) -> None:
        """探测 keyring 后端是否真的可用。

        只 import keyring 是不够的：在部分环境下 keyring 能导入但找不到后端，
        调用时才抛 NoKeyringError。因此这里额外做一次 get_keyring() 检查。
        """
        try:
            import keyring  # 延迟导入：非 Windows 或未安装时也能给出友好提示
            from keyring import get_keyring  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - 环境相关
            self._keyring_error = f"keyring 导入失败：{exc}"
            log.warning("keyring 不可用，将仅在内存中保存密钥。原因：%s", exc)
            return

        try:
            backend = get_keyring()
            if backend is None:
                raise RuntimeError("未找到可用的 keyring 后端")
            # Windows 上期望拿到 keyring.backends.Windows.WinVaultKeyring。
            # 若拿到的是 fail 后端（keyring.backends.fail.Keyring），说明后端缺失。
            backend_name = type(backend).__name__
            if backend_name.lower().startswith("fail"):
                raise RuntimeError(f"keyring 回退到了不可用后端 {backend_name}")
            self._keyring_module = keyring
            log.debug("keyring 后端可用：%s", backend_name)
        except Exception as exc:  # pragma: no cover - 环境相关
            self._keyring_error = f"keyring 后端不可用：{exc}"
            log.warning("keyring 后端不可用，将仅在内存中保存密钥。原因：%s", exc)

    @property
    def keyring_available(self) -> bool:
        """keyring 是否可用（界面据此决定是否显示降级提示）。"""
        return self._keyring_module is not None

    @property
    def keyring_error(self) -> str | None:
        """不可用原因，用于界面提示与 README 指引。"""
        return self._keyring_error

    def unavailable_message(self) -> str:
        """生成给用户看的安装指引（硬约束 #3 要求"报错提示安装"）。"""
        return (
            "系统密钥链（Windows 凭据管理器）不可用，密钥无法安全保存。\n"
            f"原因：{self._keyring_error or '未知'}\n"
            "解决方法（任选其一）：\n"
            "  1) 安装/修复 keyring 及其 Windows 后端：\n"
            "     python -m pip install --upgrade keyring pywin32\n"
            "  2) 直接把密钥写入环境变量（程序会在 keyring 无值时读取），\n"
            "     例如在 PowerShell 中： $env:OPENAI_API_KEY=\"sk-...\"\n"
            "当前将仅在本次会话内存中保存密钥：程序关闭后需要重新输入，"
            "但密钥绝不会写入任何文件。"
        )

    # --- 读写 ---------------------------------------------------------------

    def get(self, account: str) -> str | None:
        """按优先级读取密钥：内存 → keyring → 环境变量。

        返回 None 表示完全没有可用密钥，界面据此弹出输入框（硬约束 #3）。
        """
        if not account:
            return None

        # 1) 本次会话内存（用户刚在界面输入的，优先级最高）
        cached = self._memory.get(account)
        if cached:
            return cached

        # 2) keyring 持久存储
        if self._keyring_module is not None:
            try:
                secret = self._keyring_module.get_password(self._service, account)
                if secret:
                    log.debug("已从 keyring 读取密钥：%s", mask(secret))
                    return secret
            except Exception as exc:
                # 读取失败不阻断流程：退到环境变量，并记 WARN 供排查。
                log.warning("从 keyring 读取密钥失败（account=%s）：%s", account, exc)

        # 3) 环境变量（变量名就是 account，即 models.yaml 里的 key_env）
        env_secret = os.environ.get(account)
        if env_secret:
            log.info("已从环境变量 %s 读取密钥：%s", account, mask(env_secret))
            return env_secret

        return None

    def set(self, account: str, secret: str) -> bool:
        """写入密钥。返回 True 表示已持久化，False 表示仅存在于内存。

        注意：无论哪种情况都**不写任何文件**（硬约束 #3，
        禁止自行实现 AES 落盘加密）。
        """
        if not account:
            raise ValueError("account 不能为空")
        secret, notes = normalize_api_key(secret)
        if notes:
            log.info("保存密钥时清理了粘贴杂质（%s）。", "、".join(notes))
        problem = key_problem(secret)
        if problem:
            raise ValueError(problem)

        self._memory[account] = secret

        if self._keyring_module is None:
            log.warning("keyring 不可用，密钥 %s 仅保存在本次会话内存中。", mask(secret))
            return False

        try:
            self._keyring_module.set_password(self._service, account, secret)
            log.info("密钥已保存到系统密钥链：%s", mask(secret))
            return True
        except Exception as exc:
            log.warning(
                "写入 keyring 失败（account=%s）：%s。密钥仅保存在本次会话内存中，程序关闭后需重输。",
                account,
                exc,
            )
            return False

    def sources(self, account: str) -> dict[str, bool]:
        """这个账户的密钥**分别**存在于哪些来源：{"memory":…, "keyring":…, "env":…}。

        为什么必须有它：界面上"删除密钥"必须说真话 ——
        keyring 里的条目删得掉，而**环境变量删不掉**（那是用户在系统里设的）。
        如果只报告"已删除"，而程序下次请求又从环境变量读到同一把密钥，
        用户会认为删除功能坏了（或者更糟：以为密钥已经失效）。
        """
        found = {"memory": bool(account) and bool(self._memory.get(account)),
                 "keyring": False,
                 "env": bool(account) and bool(os.environ.get(account))}
        if account and self._keyring_module is not None:
            try:
                found["keyring"] = bool(
                    self._keyring_module.get_password(self._service, account)
                )
            except Exception as exc:
                log.debug("查询 keyring 条目失败（account=%s）：%s", account, exc)
        return found

    def delete(self, account: str) -> bool:
        """删除本次会话内存与系统密钥链里的密钥；返回"是否真的动过"。

        ⚠ 它**删不掉环境变量**（那是用户在系统里设置的，程序无权修改）。
        调用方应当用 sources() 先看清来源，删完再查一次，把真实结果告诉用户，
        不要把"我执行了删除"当成"密钥已经不存在"。
        """
        existed = False
        if account in self._memory:
            del self._memory[account]
            existed = True
            log.info("已清除本次会话内存中的密钥（account=%s）。", account)
        if self._keyring_module is not None:
            try:
                self._keyring_module.delete_password(self._service, account)
                existed = True
                log.info("已从系统密钥链删除密钥（account=%s）。", account)
            except Exception as exc:
                # 条目不存在时 keyring 也会抛异常，属正常情况，只记 DEBUG。
                log.debug("删除 keyring 条目失败（account=%s）：%s", account, exc)
        return existed

    def require(self, account: str) -> str:
        """必须拿到密钥，否则抛异常（CLI 免界面路径使用）。"""
        secret = self.get(account)
        if not secret:
            raise KeyringUnavailableError(
                f"未找到 API 密钥（account={account}）。\n"
                "请通过界面输入并保存，或把密钥写入同名环境变量。\n"
                f"密钥绝不写入任何配置文件，仅存放于系统密钥链（{SERVICE_NAME}）。"
            )
        return secret
