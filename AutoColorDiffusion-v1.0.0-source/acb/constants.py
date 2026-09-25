# -*- coding: utf-8 -*-
"""全局常量与"魔法数字"的唯一权威来源。

本模块的每一条数值常量都必须在注释里写明：
    (1) 数值本身为什么是这个值（依据是什么）；
    (2) 若被改动会有什么后果。
这是本项目的硬性代码规范，目的是避免后续维护者"看着不顺眼就调一下"
从而破坏批量调色的稳定性。
"""

from __future__ import annotations

import re
from typing import Final

# ============================================================================
# 一、RAW 文件白名单
# ============================================================================

# 界面文件选择对话框的白名单后缀（硬约束 #1），大小写不敏感、递归扫描子目录。
# 这份清单覆盖了主流相机厂商的 RAW 容器格式：
#   Canon: CR3 / CR2      Nikon: NEF / NRW        Sony: ARW / SRF / SR2
#   Fujifilm: RAF         Olympus/OM: ORF        Panasonic: RW2
#   Pentax: PEF           Adobe/Apple/DJI/Leica: DNG
RAW_EXTENSIONS: Final[tuple[str, ...]] = (
    ".cr3",
    ".cr2",
    ".nef",
    ".nrw",
    ".arw",
    ".srf",
    ".sr2",
    ".raf",
    ".orf",
    ".rw2",
    ".pef",
    ".dng",
)

# DNG 例外集合（硬约束 #1）：
#   DNG 的 XMP 已嵌入文件内部，ACR 优先读取嵌入元数据，
#   旁侧 .xmp 可能被忽略或产生冲突。
#   因此对 .dng 一律采用「读出嵌入 XMP → 修改 → 写回文件内部」策略，
#   不得生成 .dng.xmp 旁侧文件。
DNG_EXTENSIONS: Final[frozenset[str]] = frozenset({".dng"})


def all_suffixes_for_dialog() -> list[str]:
    """给 QFileDialog 用的后缀清单（硬约束 #17）。

    Qt 的 setNameFilters 在 Windows 上对后缀大小写敏感（不敏感的是文件系统，
    不是筛选器字符串），因此必须同时列出大写与小写两套后缀，
    否则用户看不到 .CR3 这类大写扩展名的文件——真实相机产出的恰
    恰绝大多数是大写，这是必须落地的关键细节。

    返回形如 ["*.cr3", "*.CR3", "*.cr2", "*.CR2", ...]。
    大小写两套 × 12 种格式 = 24 个模式。
    """
    patterns: list[str] = []
    for ext in RAW_EXTENSIONS:
        patterns.append(f"*{ext}")
        patterns.append(f"*{ext.upper()}")
    return patterns


def is_supported_raw(name: str) -> bool:
    """判断文件名是否属于白名单（大小写不敏感）。"""
    lower = name.lower()
    return any(lower.endswith(ext) for ext in RAW_EXTENSIONS)


def is_dng(name: str) -> bool:
    """判断文件名是否为 DNG（大小写不敏感）。"""
    return name.lower().endswith(tuple(DNG_EXTENSIONS))


# ============================================================================
# 二、缩略图（AI 判断专用，不参与出片）
# ============================================================================

# 长边缩放目标：1024 像素。
# 依据：主流视觉语言模型在约 1MP（1024×1024）以内已能可靠判断曝光、
#       对比度、白平衡与色偏这四类本工具需要的信号；继续放大只增加
#       token 成本与请求延迟，对参数判断精度没有可观测提升。
#       1024 同时是 2 的幂，用 LANCZOS 缩放时不会产生半像素采样偏差
#       （非 2 的幂目标尺寸在极端宽高比下更容易出现摩尔纹）。
# 后果：调大会显著增加 API 费用；调小会让暗部噪点与细微色偏不易被模型发现。
THUMB_LONG_EDGE: Final[int] = 1024

# “嵌入式预览偏小、细节不可信”的判定比例（相对 THUMB_LONG_EDGE）。
#
# 依据（实测 + 目的）：我们最终就是要一张长边 1024 的缩略图，所以
# “预览本身是否够用”应当相对这个目标来衡量，而不是相对原片分辨率
# （按原片的 1/4 去要求只会把 990×660 这类完全够用的预览判成不合格，
# 白白回退到每张慢 1–5 秒的 rawpy）。
#   实测：Canon CR3 的 PreviewImage = 1620×1080（够用，不标低可信）；
#         DJI DNG 的 PreviewImage = 960×720（是目标的 0.94，仍然够用）；
#         而 320×213 这类（0.2 倍）放大后细节全是插值产物。
# 取 0.5：明显小于目标才算“不可信”，避免在 0.9 倍这种几乎达标的档位上误报。
PREVIEW_LOW_CONFIDENCE_RATIO: Final[float] = 0.5

# 缩略图 JPEG 质量：85。
# 依据：q85 处的压缩损失已低于"AI 对色彩/曝光/白平衡的判断阈值"，
#       即再提高质量模型也判断不出差异；同时典型体积约 150–250KB，
#       base64 后（×1.37）仍在各家 1MB 单图上限内且留足余量。
# 后果：调低到 70 以下时暗部会出现压缩块，可能被模型误判为噪点/色带；
#       调高到 95 则体积翻倍、费用上升而无精度收益。
THUMB_QUALITY: Final[int] = 85

# 缩略图缓存有效性下限：小于 1KB 的文件必然是写坏的残片。
# 依据：1024 长边的 q85 JPEG 即使全黑也有数 KB。
THUMB_MIN_VALID_BYTES: Final[int] = 1024

# 缩略图 MIME 类型（全部统一编码为 JPEG）。
THUMB_MIME: Final[str] = "image/jpeg"


# ============================================================================
# 三、网络请求与重试
# ============================================================================

# 单张请求超时：5 秒。
# 依据（用户指定）：希望失败得足够快——密钥是占位值 / 网关不可用时，
#       等 60 秒才失败会让整批任务看起来像卡死。
# ⚠ 重要提醒：5 秒对**真实的视觉模型请求**几乎必然超时。
#       视觉模型处理 1MP 图并输出结构化 JSON 的 P95 延迟通常在 10–30 秒，
#       5 秒会全部失败。跑真实 API 前请把 config/models.yaml 里对应条目的
#       request_timeout_s 调到 20–60——该字段可覆盖本常量，无需改代码。
REQUEST_TIMEOUT_S: Final[int] = 5

# 连接建立阶段的超时：5 秒。
# 依据：必须 ≤ REQUEST_TIMEOUT_S，否则“5 秒超时”是句空话——
#       目标地址不可达时会先耗掉更长的连接超时。
#       TCP 握手 + TLS 协商在正常网络下 <2 秒；5 秒仍未连上说明网络或 DNS 有问题。
CONNECT_TIMEOUT_S: Final[int] = 5

# 读超时在**重试时**的放大倍数与天花板。
# 依据（2026-09-24 用户实测 GLM 报错排查）：
#   · 智谱 glm-5.3-flash 是**思考模型**，一次带图请求实测 51 秒才返回（成功那次）；
#   · 而配置里 request_timeout_s 当时是 60 → 同一批 4 并发时几张刚好越过 60 秒；
#   · 重试仍用 60 秒，等于必然再失败一次 —— 白花一次预算。
# 所以：超时后的那一次重试把**读超时翻倍**（不是重发同样的请求），
# 上限 600 秒（防配置里写了一个离谱的小值导致翻倍也不够）。
# ⚠ 这不会改变 MAX_PARSE_RETRIES / MAX_ATTEMPTS_PER_FILE 的对齐关系（仍是 2 次尝试）。
TIMEOUT_ESCALATION_FACTOR: Final[float] = 2.0
TIMEOUT_ESCALATION_CEILING: Final[int] = 600

# 默认并发数：2（硬约束 #7 允许配置）。
#
# 【从 4 降到 2 的依据（用户 2026-09-24 实测）】
#   · DeepSeek Flash：并发 4 时他仍看到零星报错；实际日志显示那两次报错是
#     我们自己的 DNG 写回核对误报（蒙版，已修），而不是 API 失败 ——
#     但这批图在并发 4 下的**读超时风险与 TPM 峰值**确实更高：
#     qwen/DashScope 在 18:08 那轮分批撞了 429（免费额度/TPM），GLM/Kimi 更严，
#     GLM 又是思考模型、单发就慢 —— 并发越高，同样几分钟里撞限流与超时的概率越大。
#   · 4→2 的代价是总耗时变长，但「失败 → 重试 → 拆单重发」不只慢，还多花钱；
#     用户要的是「跑完」，不是「跑得快但有几张红」。
#   · 仍可在界面/CLI 调大（1–16）；需要冲量时自己加，默认保守。
DEFAULT_WORKERS: Final[int] = 2

# Kimi（月之暗面）账户档位 → 限速，取自官方《充值与限速》表（2026-09-24 核对）：
#   Tier0（累计充值 ¥0）   并发 1、RPM 3
#   Tier1（累计充值 ¥50）  并发 15、RPM 100
#   TikTok：代金券余额**不计入**累计充值总额 —— 所以“账上有钱”不等于 Tier1。
# 为什么写在这里：官方**没有**提供查询档位的接口（实测 /v1/users/me/balance 只返回
# 可用/代金券/现金余额，对话响应里也没有任何 ratelimit 头），所以只能：
#   ① 默认按 Tier1 跑（否则充过钱的用户享受不到应得的并发）；
#   ② 界面上给一个一键切换（未充值的用户点一下变 Tier0，就不必先白撞几次 429）。
KIMI_PROVIDER_ID: Final[str] = "kimi"
KIMI_TIER_LIMITS: Final[dict[str, tuple[int, int]]] = {
    "tier0": (1, 3),
    "tier1": (15, 100),
}

# 失败后自动重试次数：1（用户指定），即最多 2 次尝试（首发 + 1 次重试）。
# 硬约束 #5 要求“解析失败要能自动重试”，1 次是满足该要求的最小值。
MAX_PARSE_RETRIES: Final[int] = 1

# 同一文件累计失败达到此值即永久跳过，不再进入 --resume（硬约束 #18）。
# 依据：2 = 1 次正常 + 1 次重试，**必须与 MAX_PARSE_RETRIES 对齐**；
#       两套阈值若不一致会出现“重试还有名额但已被永久跳过”的矛盾状态。
MAX_ATTEMPTS_PER_FILE: Final[int] = 2

# 指数退避：base=1 秒，factor=2。
# 依据：足以跨过常见的瞬时限流窗口（多数限流是秒级计数器）。
#       重试次数已降为 1（见 MAX_PARSE_RETRIES），因此实际只会用到一档：
#       约 1 秒（含 ±20% 抖动）。
BACKOFF_BASE_S: Final[float] = 1.0
BACKOFF_FACTOR: Final[float] = 2.0

# 退避抖动比例：±20%。
# 依据：并发 4 时若所有失败请求同时以 1s/2s/4s 重试，会形成同步脉冲反复
#       撞上限流。±20% 抖动把重试时间打散，是消除"惊群"的最小代价手段。
BACKOFF_JITTER: Final[float] = 0.2

# 单批最大请求数倍数：文件数 × 2（硬约束 #18）。
# 依据：正常 1 次 + 重试 1 次 = 2，正好覆盖单文件最坏情况
#       （已随 MAX_PARSE_RETRIES 同步下调）；超出这个总量说明存在系统性故障
#       （如 key 失效、模型名写错），此时继续请求只会白烧钱，应当停止并告警。
BUDGET_MULTIPLIER: Final[int] = 2

# 训练模式的请求预算（总次数，不是倍数）。
#
# 为什么不是 1：训练只发**一次**归纳请求（全部样本的参数明细 + 缩略图都在这一发里），
# 但“一次请求”不等于“只花一个预算位” —— 每次真正发出的 HTTP 尝试都要计费：
#   ① 首发；② 读超时后的重试（读超时会翻倍，见 TIMEOUT_ESCALATION_FACTOR）；
#   ③ 解析/校验失败后的修复重试（MAX_PARSE_RETRIES=1）。
# 实测（2026-09-24）用户 44 个样本的训练：首发在 180s 读超时上挂掉，
# 重试需要第二个预算位，而当时预算是 1 → 重试直接被拒，
# 整次训练连同风格档案一起被丢弃，用户端只看到一个“请求超时”，
# 之后几轮输出依然用的是旧风格（他以为已经重训过了）。
TRAIN_REQUEST_BUDGET: Final[int] = 3

# 输出上限失败重试时的“翻倍天花板”。
#
# 背景：思考模式的推理 token 也算在 max_tokens 里，所以“空 content”与
# “finish_reason=length”两个症状都可能是输出上限不够导致的。
# 遇到这两种失败时，程序把 max_output_tokens 翻倍后重试一次（只一次，
# 因为重试次数硬约束是 1 次）；这个值就是“翻到多大就不再翻”的上限。
#
# 依据：DeepSeek 官方单次输出上限 384K、默认思考模式 64K，其他家普遍 16K–128K。
#       65536 足以覆盖“一份 JSON + 较长推理”，又不会因为“上限不等于花钱”
#       （按实际生成计费）而变成隐性成本风险；同时防护了配置里
#       把 max_output_tokens 写得很大的情况（翻倍后不会失控）。
MAX_OUTPUT_TOKENS_CEILING: Final[int] = 65536

# 进度条中"文件处理阶段"占的百分比，剩余部分留给收尾阶段
# （生成脚本 + 调用 Photoshop 导出）。
#
# 依据（真实反馈）：进度总步数原本只覆盖「预览 + 分析 + 写出 XMP」，
#   于是进度条在**还没开始调用 Photoshop** 时就已经冲到 100%，
#   而 Photoshop 批量导出往往是最慢的一步（几十秒到几分钟），
#   用户会以为程序卡死、或者以为已经出片了。
#
# 【为什么按比例留、而不是留固定几步】
#   固定留 2 步时 total = 文件步数 + 2：批量 100 张时收尾只占 0.66%，
#   进度条会贴着 99% 不动 —— 和"贴着 100%"是同一类毛病。
#   按比例留（90%）后，收尾阶段在任何批量规模下都占满 10% 的可见区间，
#   "调用 Photoshop 导出"期间稳定停在 95% 左右。
PROGRESS_FILE_STAGE_PERCENT: Final[int] = 90

# 收尾阶段至少预留的步数。收尾若被跳过（--no-photoshop、或本次没有产出），
# 结尾也会把进度强制推到 100%，不会停在 99%。
PROGRESS_POST_STEPS_MIN: Final[int] = 2

# --dry-run 只跑的张数（硬约束 #7）。
# 依据：3 张足以覆盖"提取预览 → 请求 API → 写 XMP → 生成脚本"这条链路，
#       同时把验证成本压到最低。
DRY_RUN_LIMIT: Final[int] = 3


# ============================================================================
# 四、输出（出片）参数
# ============================================================================

# ============================================================================
# 输出 JPG 质量：直接使用 Photoshop 的 0–12 刻度
# ============================================================================
# 【为什么把 Photoshop 的刻度作为唯一权威表示】
# 界面最初用的是 libjpeg 语义的四档（低=60/中=75/高=88/最高=95），
# 再映射到 Photoshop 的 0–12。这带来两个无法回避的问题：
#   1. 映射是**有损且不可逆**的：PS 只有 13 档，四档压进去后必然有档位撞车，
#      产出**完全相同的文件**（例如两个高值都落到上限 12），档位名就失去了意义；
#   2. 真实的 libjpeg q100 在 Photoshop 里**根本无法表达**
#      （PS 的 12 就是上限，约等于 libjpeg 97），"最高"这个档位名在说谎。
# 改成直接暴露 0–12 后，用户选什么、Photoshop 就得到什么，不再有落差。
#
# 刻度含义（0–12，越大画质越高、体积越大）：
#   0–3    极限压缩，块状伪影明显
#   4–6    低质量，适合屏幕预览
#   7–8    中等质量，日常快速交付
#   9–10   良好质量，肉眼基本看不出压缩痕迹
#   11–12  高质量 / 最高质量（12 是 Photoshop 的上限）
# 说明：这是 Photoshop 的私有刻度，与 libjpeg 的 0–100 不是线性对应关系。
PS_JPEG_QUALITY_MIN: Final[int] = 0
PS_JPEG_QUALITY_MAX: Final[int] = 12

# 默认值：12（Photoshop 上限）。
# 依据：本工具的产出会被用户拿去继续编辑或交付，默认给最高质量可以避免
#       "默认设置就把画质压掉一截"的意外。体积代价相对 11 只有约 1.3 倍，
#       对交付场景可以接受；想省体积的用户把滑块拉到 10 即可（肉眼几乎无差别）。
DEFAULT_PS_JPEG_QUALITY: Final[int] = 12

# libjpeg 等效值的近似对照，**仅供界面提示与日志**，不参与任何编码决策
# （实际编码只用 PS 刻度）。
# 来源：社区长期使用的经验对照；PS 的编码器与 libjpeg 并非字节级等价。
# 存在的意义：让熟悉 Lightroom / libjpeg 的用户有个体感参照，
# 例如"PS 11 约等于 libjpeg 92"，否则从 Lightroom 迁移过来的人会一头雾水。
LIBJPEG_EQUIVALENT: Final[dict[int, int]] = {
    0: 5, 1: 15, 2: 25, 3: 32, 4: 40, 5: 50, 6: 60,
    7: 68, 8: 75, 9: 82, 10: 88, 11: 92, 12: 97,
}

# PS 刻度的定性描述（用于滑块旁的说明文字与日志）。
# 括号里的补充说明（如 12 档的「（Photoshop 上限）」）在**空间紧张的界面文案**
# 里会被去掉，见 describe_ps_quality(compact=True)。
PS_JPEG_QUALITY_HINTS: Final[dict[int, str]] = {
    0: "极限压缩", 1: "极限压缩", 2: "极限压缩", 3: "极限压缩",
    4: "低质量", 5: "低质量", 6: "低质量",
    7: "中等质量", 8: "中等质量",
    9: "良好质量", 10: "良好质量",
    11: "高质量", 12: "最高质量（Photoshop 上限）",
}

# 极低画质告警阈值：≤ 此值即视为"极低画质"，界面必须显式提示，**不许静默放行**。
#
# 取值依据：与上方 PS_JPEG_QUALITY_HINTS 里 0–3 = "极限压缩" 的区间对齐。
#   0 约等于 libjpeg 5、3 约等于 libjpeg 32 —— 这一段在正常观看尺寸下就能看到
#   明显的块状伪影，属于"手滑毁片"区间。只保护 0 而放过 1/2/3 讲不通，
#   它们的定性描述本来就是同一个"极限压缩"。
#
# 为什么是"告警"而不是"禁止"：0 是 Photoshop 法定刻度内的合法值（见上方注释），
#   用户可能真的是在做缩略图 / 占位图。禁止等于替用户做了他没让你做的决定，
#   所以策略是**放行 + 必须看见警告**。
#
# 想收紧到只警告 0，把这里改成 0 即可——滑块的标红、导出前的确认框、
# 命令行的警告、以及两个自检脚本里的断言全部由它派生。
PS_JPEG_QUALITY_WARN_MAX: Final[int] = 3

# 滑块旁的红字前缀。与告警阈值一样属于"用户可见文案"，所以放常量表，
# 避免界面与命令行各写一份而逐渐失配。
LOW_QUALITY_WARN_BADGE: Final[str] = "⚠ 极低画质"

# 警示色（深红 #c0392b）。选它是因为在项目使用的浅色主题（白底）上对比度约
# 5.9:1，达到 WCAG AA 对正文的要求，且不像纯红 #ff0000 那样刺眼。
LOW_QUALITY_WARN_COLOR: Final[str] = "#c0392b"

# 旧档位名 → PS 刻度。**仅用于兼容已写好的命令行脚本**（例如 `--quality 高`）。
# 界面上不再出现档位名，已改用 0–12 滑块。
# 映射依据：取原 libjpeg 语义值最接近的 PS 等效档；
# 95/100 分别取 11/12，保持两者可区分（旧实现里它们都落到 12，产出完全相同）。
LEGACY_QUALITY_ALIASES: Final[dict[str, int]] = {
    "低": 7,
    "中": 9,
    "高": 11,
    "最高": 12,
}


def clamp_ps_quality(value: int) -> int:
    """把任意整数夹到合法的 Photoshop 质量区间 0–12。"""
    return max(PS_JPEG_QUALITY_MIN, min(PS_JPEG_QUALITY_MAX, int(value)))


def resolve_ps_quality(value: int | str) -> tuple[int, str | None]:
    """把用户输入（整数或旧档位名）解析为 Photoshop 刻度。

    返回 (PS 刻度, 提示文字)。提示文字非 None 表示发生了回退或兼容转换，
    调用方应把它写进日志——静默接受非法输入会让用户以为设置生效了。
    """
    if isinstance(value, str):
        text = value.strip()
        if text in LEGACY_QUALITY_ALIASES:
            converted = LEGACY_QUALITY_ALIASES[text]
            return converted, (
                f"质量参数收到的旧档位名「{text}」已转换为 Photoshop 刻度 {converted}；"
                f"建议改用 0–{PS_JPEG_QUALITY_MAX} 的整数"
            )
        try:
            number = int(text)
        except ValueError:
            return DEFAULT_PS_JPEG_QUALITY, (
                f"无法识别的质量值 {value!r}，已回退到默认 {DEFAULT_PS_JPEG_QUALITY}"
            )
        return resolve_ps_quality(number)

    clamped = clamp_ps_quality(value)
    if clamped != int(value):
        return clamped, f"质量值 {value} 超出 0–{PS_JPEG_QUALITY_MAX}，已夹到 {clamped}"
    return clamped, None


# 全角括号里的补充说明。只用于空间紧张的界面文案（见 describe_ps_quality）。
# 允许括号内出现「」这类符号，但不允许嵌套括号 —— 本项目的文案里没有嵌套。
_PAREN_NOTES: Final[re.Pattern[str]] = re.compile(r"（[^（）]*）")


def _strip_paren_notes(text: str) -> str:
    """去掉全角括号里的补充说明（只用于空间紧张的界面文案）。

    用正则而不是另维护一份"短文案表"：两份表早晚会分叉，
    而"界面文案与常量表各说一套"正是本项目踩过的坑。
    """
    return _PAREN_NOTES.sub("", text).strip()


def describe_ps_quality(value: int, *, compact: bool = False) -> str:
    """生成一行人类可读的质量说明。

    compact=True 用于滑块旁边那一行（横向空间紧张）：只保留
    "当前值 / 上限 + 定性描述"，并去掉括号里的补充说明与 libjpeg 等效值。

    【为什么要分两版】不加限制时 12 档是
    "12 / 12　最高质量（Photoshop 上限）（约等于 libjpeg 97）"——
    在底栏那一行里放不下，被截成"…（约等于 libj…"，
    反而看不清当前到底在哪一档。而 12 恰好就是默认档，一打开就能碰到。
    完整说明仍用于 tooltip 与日志：信息不丢，只是换个地方待着。
    """
    quality = clamp_ps_quality(value)
    hint = PS_JPEG_QUALITY_HINTS.get(quality, "")
    if compact:
        return f"{quality} / {PS_JPEG_QUALITY_MAX}　{_strip_paren_notes(hint)}"
    libjpeg = LIBJPEG_EQUIVALENT.get(quality)
    libjpeg_text = f"（约等于 libjpeg {libjpeg}）" if libjpeg is not None else ""
    return f"{quality} / {PS_JPEG_QUALITY_MAX}　{hint}{libjpeg_text}"

def is_very_low_quality(value: int) -> bool:
    """该质量值是否属于"极低画质"（≤ PS_JPEG_QUALITY_WARN_MAX）。

    界面据此标红、导出前弹确认、命令行据此打警告——三处共用一个判定，
    避免"界面标红了但命令行没警告"这种不一致。
    """
    return clamp_ps_quality(value) <= PS_JPEG_QUALITY_WARN_MAX


def describe_low_quality_warning(value: int) -> str:
    """极低画质的警告正文。界面确认框与命令行共用同一份文案。

    刻意写明"这个取值是合法的"：否则用户会以为程序在报错，
    而实际上 Photoshop 会老老实实按这个值编码。
    """
    quality = clamp_ps_quality(value)
    libjpeg = LIBJPEG_EQUIVALENT.get(quality, -1)
    return (
        f"当前输出质量为 Photoshop 刻度 {quality} / {PS_JPEG_QUALITY_MAX}"
        f"（约等于 libjpeg {libjpeg}），属于极低画质。\n\n"
        "压缩块状伪影在正常观看尺寸下肉眼可见，不适合交付或二次编辑。\n\n"
        "这个取值本身是合法的，Photoshop 会照此编码。"
        "请确认你确实要用它（例如生成极小的预览图）。"
    )


# 输出色彩空间（硬约束 #8）→ Photoshop 的 ICC 配置文件名称。
# 这些名称必须与系统已安装的 ICC 描述名完全一致，convertProfile 才能命中，
# 否则 jsx 会抛错（已用 try/catch 逐张兜底）。
COLOR_SPACE_TO_PS_PROFILE: Final[dict[str, str]] = {
    "sRGB": "sRGB IEC61966-2.1",
    "Adobe RGB(1998)": "Adobe RGB (1998)",
    "Display P3": "Display P3",
}
COLOR_SPACE_CHOICES: Final[tuple[str, ...]] = ("sRGB", "Adobe RGB(1998)", "Display P3")
DEFAULT_COLOR_SPACE: Final[str] = "sRGB"

# --- 导出文件格式 ---------------------------------------------------------
# 界面下拉与 CLI 都只认这三个值；扩展名与 Photoshop 的 SaveOptions 一一对应。
EXPORT_FORMATS: Final[tuple[str, ...]] = ("JPG", "PNG")
DEFAULT_EXPORT_FORMAT: Final[str] = "JPG"
EXPORT_FORMAT_EXTENSIONS: Final[dict[str, str]] = {
    "JPG": ".jpg",
    "PNG": ".png",
}
# 无损格式：画质（PS 0–12）对它们没有意义，界面据此把画质滑块灰掉，
# 而不是让它看起来有用（静默无效的控件比禁用的控件更误导）。
LOSSLESS_EXPORT_FORMATS: Final[tuple[str, ...]] = ("PNG",)

# 格式名别名：用户可能写 JPEG。
_EXPORT_FORMAT_ALIASES: Final[dict[str, str]] = {
    "JPG": "JPG",
    "JPEG": "JPG",
    "PNG": "PNG",
}

# 【为什么没有 TIFF】实测 Photoshop 2026 的脚本 DOM 里**没有 TIFFSaveOptions**
# （同一版本 JPEGSaveOptions / PNGSaveOptions 都在），因此无法脚本化导出 TIFF：
#   - doc.saveAs(File) 不带 options 会按默认格式存成 PSD（扩展名不决定格式）；
#   - 改用 ActionManager 的 executeAction('save', As=TIFF) 会弹出 TIFF 选项
#     模态框并**挂死**，比直接报错严重得多。
# 与其提供一个选中后必然失败的选项，不如干脆不提供。这两条错路都实测过。
_REMOVED_EXPORT_FORMATS: Final[tuple[str, ...]] = ("TIFF", "TIF")


def is_lossless_format(fmt: str) -> bool:
    """该格式是否无损（无损时画质滑块无意义）。"""
    return (fmt or "").upper() in LOSSLESS_EXPORT_FORMATS


def extension_for_format(fmt: str) -> str:
    """把格式名解析为扩展名。未知格式回退到默认格式的扩展名。

    刻意**不抛异常**：格式名可能来自旧配置文件或手打的命令行参数，
    为一个字符串让整批任务失败不值得；实际用的格式会写进日志与 manifest。
    """
    key = _EXPORT_FORMAT_ALIASES.get((fmt or "").upper())
    if key is None:
        key = DEFAULT_EXPORT_FORMAT
    return EXPORT_FORMAT_EXTENSIONS[key]


def resolve_export_format(value: str | None) -> tuple[str, str | None]:
    """把用户给的格式名规范化。返回 (格式, 提示文字或 None)。

    返回提示文字而不是静默纠正：旧脚本里写着 "--format jpeg" 时，
    用户应当从日志里看到"已按 JPG 处理"，而不是以为 jpeg 被原样支持。
    """
    text = (value or "").strip().upper()
    if not text:
        return DEFAULT_EXPORT_FORMAT, None
    if text in EXPORT_FORMATS:
        return text, None
    if text in _EXPORT_FORMAT_ALIASES:
        resolved = _EXPORT_FORMAT_ALIASES[text]
        return resolved, f"导出格式 {value!r} 已按 {resolved} 处理"
    if text in _REMOVED_EXPORT_FORMATS:
        # 旧配置 / 旧命令行脚本里可能还写着 TIFF，给一句明确的说明而不是
        # 含糊的"无法识别"，用户才知道该改成什么。
        return (
            DEFAULT_EXPORT_FORMAT,
            f"导出格式 {value!r} 已不再提供（TIFF 无法通过 Photoshop 脚本导出），"
            f"已回退到 {DEFAULT_EXPORT_FORMAT}",
        )
    return (
        DEFAULT_EXPORT_FORMAT,
        f"无法识别的导出格式 {value!r}，已回退到 {DEFAULT_EXPORT_FORMAT}",
    )


# --- 导出文件名尾缀（硬约束 #14 的修订版）--------------------------------
# 用户可以自定义（填 "edit" → 原主文件名 + "-edit"）；留空则用默认尾缀 "-1"。
EXPORT_SUFFIX_DEFAULT: Final[str] = "-1"
# 用户没写前导分隔符时补这个（填 "edit" → "-edit"）。
EXPORT_SUFFIX_SEPARATOR: Final[str] = "-"
# 自定义尾缀遇到重名时追加的计数分隔符：x-edit.jpg → x-edit_2.jpg。
EXPORT_SUFFIX_COLLISION_SEPARATOR: Final[str] = "_"
# 尾缀长度上限。依据：Windows 单段文件名上限 255 字符，
# 而"原主名 + 尾缀 + 计数"都要塞进去；32 已远超实际需要，只是防呆。
MAX_EXPORT_SUFFIX_LEN: Final[int] = 32
# Windows 文件名里不允许出现的字符。
EXPORT_SUFFIX_FORBIDDEN: Final[str] = '\\/:*?"<>|'

# 导出目录名（硬约束 #8 默认 <源目录>/_export/）。
EXPORT_DIR_NAME: Final[str] = "_export"
EXPORT_DIR_NAME_DRYRUN: Final[str] = "_export_dryrun"

# 去重计数上限（硬约束 #14）。
# 上限设 999 是为了防止在异常场景（如目录里有上万同名文件）下无限循环。
COLLISION_MAX_INDEX: Final[int] = 999


def normalize_export_suffix(raw: str | None) -> str:
    """把用户输入的尾缀规范化成可直接拼到文件名后的字符串。

    规则（对应用户的明确要求）：
        ""       -> "-1"      留空 → 默认尾缀
        "edit"   -> "-edit"   自动补前导 "-"
        "_edit"  -> "_edit"   用户自己写了分隔符就沿用
        "-e/dit" -> "-edit"   剔除 Windows 文件名非法字符

    非法字符是**剔除**而不是报错：尾缀只是个标记，为此打断整个导出不值得；
    剔除后的结果会回显在界面上，用户一眼能看出被改了。
    """
    text = (raw or "").strip()
    if not text:
        return EXPORT_SUFFIX_DEFAULT

    if text[0] in "-_":
        separator, body = text[0], text[1:]
    else:
        separator, body = EXPORT_SUFFIX_SEPARATOR, text

    body = "".join(ch for ch in body if ch not in EXPORT_SUFFIX_FORBIDDEN)
    # 去掉首尾的分隔符/空格/点，避免出现 "-edit-" 这种尾巴。
    body = body.strip().strip("-_. ").strip()
    if not body:
        return EXPORT_SUFFIX_DEFAULT
    return f"{separator}{body[:MAX_EXPORT_SUFFIX_LEN]}"


def resolve_export_suffix(raw: str | None) -> tuple[str, bool]:
    """返回 (实际使用的尾缀, 是否为默认尾缀)。

    "是否为默认"决定了重名时的处理方式（用户明确要求）：
        自定义尾缀：x-edit.jpg → x-edit_2.jpg → x-edit_3.jpg
        默认尾缀：  x-1.jpg    → x-2.jpg     → x-3.jpg（而不是 x-1_2.jpg）
    """
    suffix = normalize_export_suffix(raw)
    return suffix, suffix == EXPORT_SUFFIX_DEFAULT


# ============================================================================
# 五、训练模式
# ============================================================================

# 样本数低于此值提示"样本不足，结果可能不稳定"（硬约束 #15 / 第二节）。
# 依据：style_profile.json 的 param_ranges 需要均值/中位数/范围三类统计，
#       少于 10 个样本时中位数几乎等于样本自身，范围也完全由极值主导，
#       统计意义薄弱。10 是一个保守但足够有解释力的下限。
TRAIN_MIN_SAMPLES_WARN: Final[int] = 10

# 训练请求温度：0.2（硬约束 #19），低于输出模式的 0.6。
# 依据：训练是在"归纳用户偏好"，答案应当尽量确定；输出模式是在"按风格出参数"，
#       需要少量多样性以避免整批机械雷同。
TRAIN_TEMPERATURE: Final[float] = 0.2

# --- 训练模式：蒙版使用频率统计 -------------------------------------------
# 用户已裁决：忽略蒙版几何（坐标/digest/多边形动辄上千行且跨图不可比），
# 但把蒙版使用频率与局部调整倾向写进 text_rules。
#
# 蒙版名称分类规则。ACR 自动生成的名字有明显模式：
#   "蒙版 1"                 → 用户手动画笔/线性渐变/径向渐变
#   "人物 1 - 面部皮肤"       → AI 主体蒙版（人物识别）
#   "人物 1 - 头发" / "身体皮肤"
#   "对象 1"                 → AI 对象选择
# 注意：名称是本地化的，中文版 ACR 产出中文名。因此匹配时同时接受
#       英文默认名，避免用户的 ACR 是英文界面时统计失效。
MASK_NAME_MANUAL_PREFIXES: Final[tuple[str, ...]] = ("蒙版", "Mask")
MASK_NAME_SUBJECT_PREFIXES: Final[tuple[str, ...]] = ("人物", "Person", "People")
MASK_NAME_OBJECT_PREFIXES: Final[tuple[str, ...]] = ("对象", "Object")

# 局部调整字段在 text_rules 中报告"明显作用"的阈值。
# 依据：这些局部量在 ACR 里同样以 -100..+100 为刻度，±10 以下属于
#       面板拖动的噪声范围（滑块拖一格通常是 1–5），只有超过 ±10
#       才值得作为"风格倾向"写进自然语言描述。
LOCAL_ADJUST_NOTABLE_THRESHOLD: Final[int] = 10


# ============================================================================
# 六、日志
# ============================================================================

# 单日志文件上限 10MB（硬约束 #13）。
# 依据：单张图的完整 DEBUG 链路（exiftool 命令行、AI 请求耗时、
#       XMP 写回 diff 摘要）约 3–5KB；10MB 可容纳约 2000 张，
#       足够容纳一整天的重度批量工作而不滚动。
LOG_MAX_BYTES: Final[int] = 10 * 1024 * 1024

# 保留份数 5（硬约束 #13）。
# 依据：配合"文件名带日期"，5 份足以回溯最近 5 个工作日的问题现场，
#       同时把日志总占用钉在约 50MB 以内。
LOG_BACKUP_COUNT: Final[int] = 5


# ============================================================================
# 七、默认提示词（硬约束 e / 问题 e 的默认策略）
# ============================================================================

# 当用户既没填提示词、也没选风格时使用的默认提示词。
# 语义：保守的全局一致性校正，"把一组照片调到视觉一致"而非创作风格。
# 所有上界都写死在文本里，是为了让用户能在日志里看到确切约束、可审计。
DEFAULT_PROMPT: Final[str] = (
    "对全批图片做保守的全局校正：统一曝光与白平衡观感，优先保证一组照片视觉一致。"
    "硬性约束：曝光改动不超过 ±0.5 EV；对比度/高光/阴影/白色/黑色改动不超过 ±20；"
    "自然饱和度/饱和度不超过 ±20；不做 HSL 分区偏移、不做分离色调与颜色分级（保持 0）、"
    "不改动镜头校正开关、不改动相机配置文件。不追求创作风格，只求批内观感统一。"
)

# 当提示词为空时，注入"向批内中位数靠拢"的补充指令。
# 这是让"两样都空"从"无信息"变成有明确物理目标的关键：
# 程序用 numpy 在缩略图上算出本批的亮度/对比度/色温中位数并填入 %s。
AUTO_MATCH_INSTRUCTION_TEMPLATE: Final[str] = (
    "本批共 {count} 张，程序已用 numpy 在缩略图上算得本批统计中位数："
    "平均亮度 {luma:.1f}/255，对比度（亮度标准差）{contrast:.1f}，"
    "估算色温 {cct:.0f}K，平均饱和度（HSV S 通道均值）{sat:.1f}/255。"
    "请以这组中位数为目标，把每张图向其靠拢；偏离越大的图，"
    "允许的调整幅度越大，但仍受上文的硬性上限约束。"
)

# 未选择风格时使用的内置风格名（对应 styles/default_neutral.json）。
DEFAULT_STYLE_NAME: Final[str] = "default_neutral"

# 内置的「AI自主决策」风格（对应 styles/AI自主决策.json）。
#
# 用户要求（原话）："风格中再加入一个软件内置的选项叫做「AI自主决策」（同样在界面中不可删除），
# 选择该风格即让 AI 自主决策每一张图片的调整方向，不做限制。"
#
# ⚠ "不做限制"必须被正确理解：**只放开审美方向**，程序侧的硬规则一条都不放宽。
#   自由的只是"往哪个方向调"；不许动的是：
#     · 字段白名单（只能用 crs: 已登记且 ai_writable 的字段）；
#     · 机器/版本字段与相机配置（AI_FORBIDDEN_FIELDS，读-改-写时原样保留）；
#     · 取值域（越界由本地强校验拒收并触发重试）；
#     · 响应结构（只返回需要改动的字段，JSON 结构由 schema 约束）；
#     · 批内一致性（系统提示词的第 7 条规则）。
#   这些规则写在 system 提示词与 xmp/fields.py、xmp/validator.py 里，
#   **与选哪个风格无关** —— 所以任何风格（内置的、用户训练的、手写的）都绕不过它们。
AUTOPILOT_STYLE_NAME: Final[str] = "AI自主决策"

# 只在「离线调试」下可选的内置风格。
# 为什么要有这个名单：内置基准风格（default_neutral）是"中性回退"，
# 正常使用应该选「（不选，使用内置默认策略）」（效果一样，且不会被误当成训练成果），
# 所以它只在调试时可选；而「AI自主决策」是**给日常用的功能**，任何时候都能选。
DEBUG_ONLY_STYLE_NAMES: Final[tuple[str, ...]] = (DEFAULT_STYLE_NAME,)

# 日志中标记"进入了默认行为"的固定前缀，便于用户搜索与脚本断言。
WARN_AUTO_MATCH: Final[str] = "未提供提示词与风格 → 进入自动一致性校正模式"

# 【为什么需要这一条 —— 用户 2026-09 的真实反馈】
# 用户训练完风格后直接跑输出（提示词框是空的），结果「只调整了亮和颜色」，
# 他训练里明确有的 HSL / 颜色分级 / 分离色调一次都没用上。
#
# 实测根因（读日志 + 复现提示词）：旧代码在提示词框为空时**无条件**把
# DEFAULT_PROMPT 塞进请求，而那段话里写着「不做 HSL 分区偏移、不做分离色调与
# 颜色分级（保持 0）」——它与风格块里 21 条规则（“全局压绿”“分离色调走冷阴影
# +暖高光”）**正面矛盾**，而“不要做 X”比“请做 X”更容易被模型执行。
#
# 所以：选了风格就必须走这一条，而不是 DEFAULT_PROMPT。
STYLE_EXECUTION_PROMPT: Final[str] = (
    "严格按上面的风格规则逐条执行，**不要退化成只调基本面板**："
    "风格里出现过的分区参数（HSL 色相/饱和度/明亮度、分离色调、颜色分级、"
    "相机校准、效果）该用就用，幅度按规则里给出的范围取。"
    "同一批照片保持一致的观感（先定基准再让每张向基准靠拢）。\n"
    "两点必须知道：\n"
    "  1. 局部蒙版（画笔 / 渐变 / 主体蒙版）本程序**无法写入**，"
    "请用全局参数近似表达它的意图（例如用 HSL 控制某一颜色的去饱和、"
    "用分离色调定冷暖、用裁剪后晕影聚焦主体），不要因为画不了蒙版就退回只改曝光；\n"
    "  2. 相机配置（配置文件）与镜头校正由相机/ACR 自身决定，你不需要也不应该返回它们。"
)


# 「AI自主决策」风格在执行阶段用的提示词。
#
# 为什么不复用 STYLE_EXECUTION_PROMPT：那一条的语义是"严格按**上面的风格规则**逐条执行"，
# 它假定存在一份训练出来的偏好规则；而自主决策模式**没有**风格规则，
# 要表达的恰恰是"没有要先遵守的个人偏好，按图像本身判断"。
# 两者混用会让模型收到自相矛盾的指令（一边要求"逐条遵守"，一边没有任何规则可遵守）。
AUTOPILOT_PROMPT: Final[str] = (
    "本次不使用任何已训练的个人风格规则：请你**自主决策**每一张图的调整方向与幅度，"
    "按你对这张照片本身的判断给出最合适的处理 —— 该动影调就动影调、"
    "该做分区色彩（HSL / 相机校准）就做、该用颜色分级与分离色调就用，"
    "不要套固定套路，也不要为了保守而只调基本面板。\n"
    "不能动的两类东西（它们不是审美决策，而是程序与相机的约定）：\n"
    "  1. 机器/版本字段与相机配置：不要返回它们（系统提示词里已列出禁用字段）；\n"
    "  2. 字段名与取值范围：必须使用系统提示词给出的字段名与范围，越界会被本地校验拒收。\n"
    "同一批照片仍然要保持基本一致的观感：先判断这批判什么、再让每张按自己的需要调整。"
)


# ============================================================================
# 八、AI 不得产出的字段（读-改-写时原样保留）
# ============================================================================

# 这些字段与"机器/版本/工具状态"绑定，不是审美参数。
# 若允许 AI 产出，会带来两类真实危害：
#   1. crs:ProcessVersion 在样本中同时存在 15.4（PV6）与 11.0（PV5），
#      乱写会让同一批内渲染不一致，甚至被 ACR 判为不支持设置而整份失效；
#   2. crs:CameraProfile / LensProfile* 依赖本机已安装的相机配置文件与 LCP，
#      换一台机器打开就可能找不到对应 profile，导致色彩突变。
# 因此一律"只保留、不生成"。
AI_FORBIDDEN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "Version",
        "CompatibleVersion",
        "ProcessVersion",
        "CameraProfile",
        "CameraProfileDigest",
        "LensProfileSetup",
        "LensProfileName",
        "LensProfileFilename",
        "LensProfileDigest",
        "LensProfileIsEmbedded",
        "HasSettings",
        "HasCrop",
        "AlreadyApplied",
        "WhiteBalance",  # 白平衡模式由 writer 依据"是否改了 Temperature/Tint"自动决定
        "OverrideLookVignette",
        "ConvertToGrayscale",  # 转灰度属于输出决定，不是调色参数，避免误把照片变黑白
    }
)

# 训练模式下完全排除在偏好统计之外的字段（非 crs 命名空间 + 蒙版段）。
# 这些是相机/工具产生的元数据，与用户审美无关。
TRAINING_EXCLUDED_NAMESPACES: Final[tuple[str, ...]] = (
    "xmp",
    "tiff",
    "exif",
    "exifEX",
    "dc",
    "aux",
    "photoshop",
    "xmpMM",
    "stEvt",
    "crd",  # camera-raw-defaults：相机出厂默认基线，用于差分而非统计
)
