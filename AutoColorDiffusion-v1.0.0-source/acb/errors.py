# -*- coding: utf-8 -*-
"""异常类型集中定义。

设计原则：
1. 所有"可预期的失败"都有独立异常类，便于调用方精确分类处理，
   而不是靠 except Exception 一把抓（那会让硬约束 #6/#18 的失败计数失真）。
2. 异常消息必须是"给用户看的中文 + 可执行的下一步"，因为绝大多数会直接
   落到界面日志区或 failed.json 的 reason 字段。
"""

from __future__ import annotations


class AcbError(Exception):
    """所有本程序自定义异常的基类。"""


class UnsupportedPlatformError(AcbError):
    """非 Windows 平台。

    硬约束：本项目仅支持 Windows，不编写 macOS/Linux 分支。
    非 Windows 上直接抛出并给出明确提示，而不是静默降级。
    """


class ExiftoolUnavailableError(AcbError):
    """exiftool 不可用（未找到或无法执行 -ver）。

    对应硬约束 #10：不可用时给出清晰的安装指引并降级使用 rawpy 提取预览。
    这个异常只在"确实需要 exiftool 且无任何替代路径"时抛出，
    例如 DNG 内嵌 XMP 的读写（没有 exiftool 就无法安全改写 DNG）。
    """


class KeyringUnavailableError(AcbError):
    """keyring 后端不可用。

    对应硬约束 #3：若 keyring 不可用，应报错提示安装，
    或仅在本次会话内存中持有密钥（重启需重输）。
    """


class ModelConfigError(AcbError):
    """config/models.yaml 缺失或内容非法。"""


class PhotoshopUnavailableError(AcbError):
    """Photoshop 未安装 / 未授权 / COM 接口被禁用。

    对应问题 (a) 的降级分支：此时程序仍会产出 XMP + jsx + manifest + run_export.bat。
    """


class BudgetExceededError(AcbError):
    """本批请求数超过上限。

    对应硬约束 #18：默认上限 = 文件数 × 2（含重试），超出则停止并告警。
    """


class StopRequestedError(AcbError):
    """用户点了「停止」。

    这不是错误，而是控制流信号。用异常是为了能从
    线程池 worker 的任意深度立刻脱出，避免逐层判断返回值。
    调用方必须捕获并当作正常结束处理。
    """


class SchemaValidationError(AcbError):
    """AI 返回值不符合 JSON Schema / pydantic 强校验。

    对应硬约束 #5：解析失败自动重试 1 次，仍失败则记入 failed.json，
    不得中断整批任务。
    """


class QuotaExceededError(AcbError):
    """服务端说"你的账户配额/额度不够了"。

    【为什么不能用状态码区分 —— 实测】阿里云百炼把这一类也用 **HTTP 429** 返回，
    正文是：`You exceeded your current quota, please check your plan and billing details.`
    官方《错误码》把这两类分在两个条目里（2026-09-22 版）：
      · `429-Throttling.RateQuota/LimitRequests` —— 调用频率（RPS/RPM）限流 → 降并发/降频而后重试；
      · `429-Throttling.AllocationQuota/insufficient_quota` —— 消耗的 Token 配额（TPS/TPM）
        或免费额度/账单额度用尽 → **重试无效**，要等窗口滑过或去控制台处理额度。
    只看 429 会把后者当成前者：白重试、白冷却，还可能把“额度问题”学成“并发 1”，
    之后即使用户充值了也被压着跑。所以必须单独分类。
    """


class EmptyContentError(SchemaValidationError):
    """服务端返回了空的 content。

    【为什么单独分类 —— 它和“模型输出格式不对”不是同一回事】
    官方文档（DeepSeek《JSON Output》注意事项）明写：使用 JSON Output 时
    API 有概率返回空的 content。而另一个常见原因是 **max_tokens 不够**：
    思考模式的推理 token 也算在 max_tokens 里，推理把额度吃光后就只剩空 content。
    所以遇到它时该做的是“把输出上限翻倍再试一次”，而不是无脑重试同样的参数
    （那样会陷入必然失败的循环，日志里还只写“解析失败”）。
    """


class TruncatedOutputError(SchemaValidationError):
    """输出因达到 max_tokens 被截断（finish_reason=length）。

    同样值得单独分类：半截 JSON 永远解析不了，
    唯一有效的动作是抬高出上限后重试，而不是重发同样的请求。
    """


class IccProfileMissingError(AcbError):
    """源色空间已确定、但拿不到它对应的 ICC profile。

    目前唯一的触发点是：预览图的 EXIF 声明为 **Adobe RGB (1998)**
    （ColorSpace=Uncalibrated + InteropIndex=R03），而程序在
    `assets/icc/` 与系统色彩目录里都找不到 `AdobeRGB1998.icc`。

    **为什么这里选择"报错"而不是"按 sRGB 凑合"**：
        Adobe RGB 的数值体系与 sRGB 不同，同一块中等红色在 Adobe RGB 里
        数值更"淡"。把它当 sRGB 用（只打一条 WARNING 然后继续）会让 AI
        判成"欠饱和"，进而输出 +Vibrance —— 而该参数作用在 RAW 上会让
        真实出片**过饱和**。这与本项目"宁可认不出，也不猜错"的原则一致
        （同一条原则见 config.detect_provider_from_key）。
        上层收到这个异常后会改用 rawpy 全解码（它直接输出 sRGB，颜色是对的），
        代价只是每张慢 1–5 秒。
    """


class PreviewExtractionError(AcbError):
    """单张 RAW 的预览提取全部路径均失败（含 rawpy 兜底）。

    对应硬约束 #10：不得中断整批任务，调用方记入 failed.json。
    """
