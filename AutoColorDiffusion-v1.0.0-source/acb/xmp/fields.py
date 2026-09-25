# -*- coding: utf-8 -*-
"""crs: 字段清单 —— 本项目唯一的字段权威来源（问题 c 的落地点）。

设计要点
--------
1. **绝不自行发明字段名**。ACR 对不认识的 crs 字段是**静默忽略**的，
   这是最危险的失败模式：任务"成功"了，参数却没生效，用户直到出片才发现。
   因此所有字段必须在本表登记，AI 返回的未登记字段一律拒收（见 validator.py）。

2. **observed_in_samples 标记**。每字段标注是否已在项目自带的
   test_data/*.xmp（8 个真实 ACR 15.0 / 15.4 产物）中实测出现：
       True  —— 字段名、类型、值域都有真实样本佐证；
       False —— 仅来自 Adobe XMP 规范 / exiftool 标签库，尚未实测。
   运行 tools/dump_crs_fields.py 会自动校验"样本里出现但未登记"的字段，
   防止版本升级后漏字段。

3. **面板位置**（panel）不是装饰性信息。它决定了写进 JSON Schema 的 description，
   从而影响模型能否把这些英文参数名与它学到的 UI 语义对应起来。
   例如 Clarity2012 在 ACR 10+ 中位于「效果」面板而非「基本」面板。

值域来源说明
------------
- 三个样本族交叉验证：Canon EOS 850D（IMG_4820，ACR 15.0）、Canon EOS 7D
  （IMG_7648，ACR 15.3）、Canon EOS R6 II（_G4A*，ACR 15.4）。
- 范围端点与 ACR 面板滑块的物理端点一致（例如 Exposure 的 ±5 EV
  正是面板滑块的行程两端）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 字段类型
KIND_INT = "int"       # 整数，写回为不带小数的字符串（正数带 + 号）
KIND_REAL = "real"     # 浮点，写回为固定小数位的字符串（正数带 + 号）
KIND_ENUM = "enum"     # 枚举字符串
KIND_BOOL = "bool"     # 布尔，写回必须是大写 "True" / "False"
KIND_CURVE = "curve"   # 曲线控制点序列（元素型，非属性）
# 自由字符串：用于 digest（哈希）、版本号、LCP 文件名、Upright 变换矩阵等
# **机器产出的不透明文本**。这类字段一律 ai_writable=False，只保留不生成，
# 因此不需要值域约束——但必须登记，否则会触发"漏字段门禁"报警，
# 更重要的是会被误判为未知字段从而在日志里刷屏、掩盖真正的异常。
KIND_STR = "str"


@dataclass(frozen=True)
class FieldSpec:
    """单个 crs 字段的元数据。"""

    name: str
    kind: str
    panel: str
    group: str
    minimum: float | None = None
    maximum: float | None = None
    decimals: int = 0
    enum_values: tuple[str, ...] = ()
    ai_writable: bool = True
    observed_in_samples: bool = True
    description: str = ""

    @property
    def is_numeric(self) -> bool:
        return self.kind in (KIND_INT, KIND_REAL)

    def range_text(self) -> str:
        """给提示词/JSON Schema 用的范围描述。"""
        if self.kind == KIND_ENUM:
            return " / ".join(self.enum_values)
        if self.kind == KIND_BOOL:
            return 'True 或 False（字符串大写字面量）'
        if self.kind == KIND_CURVE:
            return "0..255 区间内的 (x, y) 整数控制点数组，2..16 个，按 x 递增"
        if self.kind == KIND_STR:
            return "不透明字符串（本工具只保留原值，不生成）"
        if self.kind == KIND_INT:
            return f"{int(self.minimum)} 到 {int(self.maximum)} 的整数"
        return f"{self.minimum} 到 {self.maximum} 的浮点数（{self.decimals} 位小数）"

    def json_schema_fragment(self) -> dict[str, Any]:
        """生成 JSON Schema 片段（硬约束 #5：AI 输出必须用 JSON Schema 约束）。"""
        desc = f"{self.description}（ACR 面板：{self.panel}；取值：{self.range_text()}）"
        if self.kind == KIND_ENUM:
            return {"type": "string", "enum": list(self.enum_values), "description": desc}
        if self.kind == KIND_BOOL:
            return {"type": "string", "enum": ["True", "False"], "description": desc}
        if self.kind == KIND_CURVE:
            return {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": 255},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "minItems": 2,
                "maxItems": 16,
                "description": desc,
            }
        if self.kind == KIND_STR:
            return {"type": "string", "description": desc}
        if self.kind == KIND_INT:
            return {
                "type": "integer",
                "minimum": int(self.minimum),
                "maximum": int(self.maximum),
                "description": desc,
            }
        return {
            "type": "number",
            "minimum": self.minimum,
            "maximum": self.maximum,
            "description": desc,
        }


_SPECS: list[FieldSpec] = []


def _int(
    name: str,
    lo: int,
    hi: int,
    panel: str,
    group: str,
    desc: str = "",
    *,
    ai: bool = True,
    obs: bool = True,
) -> None:
    _SPECS.append(
        FieldSpec(
            name=name,
            kind=KIND_INT,
            panel=panel,
            group=group,
            minimum=lo,
            maximum=hi,
            ai_writable=ai,
            observed_in_samples=obs,
            description=desc,
        )
    )


def _real(
    name: str,
    lo: float,
    hi: float,
    decimals: int,
    panel: str,
    group: str,
    desc: str = "",
    *,
    ai: bool = True,
    obs: bool = True,
) -> None:
    _SPECS.append(
        FieldSpec(
            name=name,
            kind=KIND_REAL,
            panel=panel,
            group=group,
            minimum=lo,
            maximum=hi,
            decimals=decimals,
            ai_writable=ai,
            observed_in_samples=obs,
            description=desc,
        )
    )


def _enum(
    name: str,
    values: tuple[str, ...],
    panel: str,
    group: str,
    desc: str = "",
    *,
    ai: bool = True,
    obs: bool = True,
) -> None:
    _SPECS.append(
        FieldSpec(
            name=name,
            kind=KIND_ENUM,
            panel=panel,
            group=group,
            enum_values=values,
            ai_writable=ai,
            observed_in_samples=obs,
            description=desc,
        )
    )


def _bool(
    name: str,
    panel: str,
    group: str,
    desc: str = "",
    *,
    ai: bool = True,
    obs: bool = True,
) -> None:
    _SPECS.append(
        FieldSpec(
            name=name,
            kind=KIND_BOOL,
            panel=panel,
            group=group,
            ai_writable=ai,
            observed_in_samples=obs,
            description=desc,
        )
    )


def _str(
    name: str,
    panel: str,
    group: str,
    desc: str = "",
    *,
    ai: bool = False,
    obs: bool = True,
) -> None:
    """登记一个自由字符串字段（digest / 版本号 / 变换矩阵等不透明文本）。

    默认 ai=False：这类字段全部是机器产出的哈希或二进制编码，
    让 AI 生成它们既无意义（可能是任意 32 位十六进制），
    又极危险（写错会让 ACR 认为设置与文件不匹配从而整份忽略）。
    """
    _SPECS.append(
        FieldSpec(
            name=name,
            kind=KIND_STR,
            panel=panel,
            group=group,
            ai_writable=ai,
            observed_in_samples=obs,
            description=desc,
        )
    )


# ============================================================================
# 基本面板 · 白平衡
# ============================================================================
# WhiteBalance 是"白平衡模式"。实测样本中 6/8 为 "As Shot"、2/8 为 "Custom"。
# 关键行为：当值为 "As Shot" 时，ACR **忽略** Temperature 与 Tint
# （它们只是记录性的机内数值）。因此 writer 在写入 Temperature/Tint 时
# 必须同时把 WhiteBalance 置为 "Custom"，否则用户会看到"参数写了但没生效"。
_enum("WhiteBalance", ("As Shot", "Auto", "Daylight", "Cloudy", "Shade", "Tungsten",
                       "Fluorescent", "Flash", "Custom"),
      "基本 · 白平衡", "wb", "白平衡模式；改为 Custom 后 Temperature/Tint 才生效",
      ai=False)
_int("Temperature", 2000, 50000, "基本 · 白平衡", "wb",
     "色温，单位开尔文（K）；自动白平衡模式下无效")
_int("Tint", -150, 150, "基本 · 白平衡", "wb",
     "色调（绿—洋红轴）；自动白平衡模式下无效")

# ============================================================================
# 基本面板 · 影调
# ============================================================================
# 注意：这些字段带 "2012" 后缀，是 Process Version 2012（PV2012）之后的命名。
# 老版本对应的是不带 2012 的 Exposure/Contrast/... ，本工具不写老字段，
# 因为一旦混用会被 ACR 判为不一致。
_real("Exposure2012", -5.0, 5.0, 2, "基本 · 曝光", "basic",
      "整体曝光，单位为 EV（光圈级）")
_int("Contrast2012", -100, 100, "基本 · 对比度", "basic", "影调对比度")
_int("Highlights2012", -100, 100, "基本 · 高光", "basic", "高光区域明暗")
_int("Shadows2012", -100, 100, "基本 · 阴影", "basic", "阴影区域明暗")
_int("Whites2012", -100, 100, "基本 · 白色", "basic", "白场裁切点")
_int("Blacks2012", -100, 100, "基本 · 黑色", "basic", "黑场裁切点")

# ============================================================================
# 基本面板 · 偏好（自然饱和度 / 饱和度）
# ============================================================================
# Vibrance 与 Saturation 的区别是调色时的核心概念：
#   Vibrance 会保护已经饱和的颜色与肤色，Saturation 则一视同仁地推高所有颜色。
# 这两条语义写进 description，模型才能做出"人像别用 Saturation"这类判断。
_int("Vibrance", -100, 100, "基本 · 自然饱和度", "basic",
     "自然饱和度，对已饱和色与肤色有保护；人像场景优先用它而非 Saturation")
_int("Saturation", -100, 100, "基本 · 饱和度", "basic",
     "全局饱和度，一视同仁地影响所有颜色，容易造成肤色过饱和")

# ============================================================================
# 参数曲线（基本面板下方的四个滑块）
# ============================================================================
_int("ParametricShadows", -100, 100, "色调曲线 · 参数 · 阴影", "parametric", "参数曲线阴影区")
_int("ParametricDarks", -100, 100, "色调曲线 · 参数 · 暗色调", "parametric", "参数曲线暗色调区")
_int("ParametricLights", -100, 100, "色调曲线 · 参数 · 亮色调", "parametric", "参数曲线亮色调区")
_int("ParametricHighlights", -100, 100, "色调曲线 · 参数 · 高光", "parametric", "参数曲线高光区")
_int("ParametricShadowSplit", 0, 100, "色调曲线 · 参数 · 阴影分割", "parametric",
     "阴影区与暗色调区的分割点占位")
_int("ParametricMidtoneSplit", 0, 100, "色调曲线 · 参数 · 中间调分割", "parametric",
     "暗色调与亮色调的分割点占位")
_int("ParametricHighlightSplit", 0, 100, "色调曲线 · 参数 · 高光分割", "parametric",
     "亮色调与高光区的分割点占位")

# ============================================================================
# 效果面板
# ============================================================================
# Clarity2012 / Texture / Dehaze 在 ACR 10 之后归入「效果」面板，
# 但它们在视觉语义上非常容易混淆，因此 description 必须写清区别。
_int("Texture", -100, 100, "效果 · 纹理", "effects",
     "纹理，作用于中频细节，适合增强/柔化皮肤与材质质感")
_int("Clarity2012", -100, 100, "效果 · 清晰度", "effects",
     "清晰度，作用于中间调的局部对比，负值产生柔焦感")
_int("Dehaze", -100, 100, "效果 · 去薄雾", "effects",
     "去薄雾，正值增强通透度并加深蓝天空，负值可制造雾感")
_int("PostCropVignetteAmount", -100, 100, "效果 · 裁剪后晕影 · 数量", "effects",
     "裁剪后晕影强度，负值压暗四角")
_int("PostCropVignetteMidpoint", 0, 100, "效果 · 裁剪后晕影 · 中点", "effects", "晕影影响范围中点",
     obs=False)
_int("PostCropVignetteFeather", 0, 100, "效果 · 裁剪后晕影 · 羽化", "effects", "晕影边缘羽化",
     obs=False)
_int("PostCropVignetteRoundness", -100, 100, "效果 · 裁剪后晕影 · 圆度", "effects", "晕影形状圆度",
     obs=False)
_int("PostCropVignetteHighlightContrast", 0, 100, "效果 · 裁剪后晕影 · 高光对比", "effects",
     "保护高光不被晕影压暗", obs=False)
_int("GrainAmount", 0, 100, "效果 · 颗粒 · 数量", "effects", "胶片颗粒数量")
_int("GrainSize", 0, 100, "效果 · 颗粒 · 大小", "effects", "胶片颗粒大小",
     obs=False)
_int("GrainFrequency", 0, 100, "效果 · 颗粒 · 粗糙度", "effects", "胶片颗粒粗糙度",
     obs=False)

# ============================================================================
# 色调曲线（曲线名称是属性，曲线点是独立的 XML 元素）
# ============================================================================
# 实测：ToneCurveName2012 在样本中出现两种值——"Linear"（未调曲线）
# 与 "Custom"（用户手动拖过点曲线）。这是训练模式判断"是否用过曲线"的关键信号。
_enum("ToneCurveName2012",
      ("Linear", "Medium Contrast", "Strong Contrast", "Custom"),
      "色调曲线 · 曲线名称", "curve",
      "曲线预设名；用户手动编辑点曲线后 ACR 会写成 Custom")

# ============================================================================
# HSL / 颜色混合（三组 × 8 色，共 24 个字段）
# ============================================================================
# 这里必须区分两组容易混淆的字段：
#   HueAdjustment*  范围 -100..100，是相对偏移量（面板上的"调整色相"滑块）
#   SplitToning*Hue 范围 0..360，  是绝对色相角
# 二者量纲不同，训练统计时禁止合并求均值（见 pipeline/style_profile.py）。
_HSL_COLORS = (
    ("Red", "红", "红色"),
    ("Orange", "橙", "橙色"),
    ("Yellow", "黄", "黄色"),
    ("Green", "绿", "绿色"),
    ("Aqua", "浅绿", "浅绿色"),
    ("Blue", "蓝", "蓝色"),
    ("Purple", "紫", "紫色"),
    ("Magenta", "洋红", "洋红色"),
)

for _suffix, _cn, _cn_full in _HSL_COLORS:
    _int(f"HueAdjustment{_suffix}", -100, 100, f"HSL · 色相 · {_cn}", "hsl",
         f"{_cn_full}的色相偏移（相对量）")
    _int(f"SaturationAdjustment{_suffix}", -100, 100, f"HSL · 饱和度 · {_cn}", "hsl",
         f"{_cn_full}的饱和度偏移")
    _int(f"LuminanceAdjustment{_suffix}", -100, 100, f"HSL · 明亮度 · {_cn}", "hsl",
         f"{_cn_full}的明亮度偏移")

# ============================================================================
# 分离色调（PV2012 后功能上被颜色分级取代，但字段仍然有效）
# ============================================================================
_int("SplitToningShadowHue", 0, 360, "分离色调 · 阴影 · 色相", "split",
     "阴影染色色相（绝对角度 0..360）")
_int("SplitToningShadowSaturation", 0, 100, "分离色调 · 阴影 · 饱和度", "split", "阴影染色强度")
_int("SplitToningHighlightHue", 0, 360, "分离色调 · 高光 · 色相", "split",
     "高光染色色相（绝对角度 0..360）")
_int("SplitToningHighlightSaturation", 0, 100, "分离色调 · 高光 · 饱和度", "split", "高光染色强度")
_int("SplitToningBalance", -100, 100, "分离色调 · 平衡", "split", "阴影与高光染色的权重平衡")

# ============================================================================
# 颜色分级（分离色调的现代替代，ACR 10+）
# ============================================================================
# 实测发现一个值得注意的现象：样本中只出现了 MidtoneHue/MidtoneSat/
# ShadowLum/MidtoneLum/HighlightLum/GlobalHue/GlobalSat/GlobalLum 与 Blending，
# 而没有 ShadowHue/ShadowSat/HighlightHue/HighlightSat。
# 说明 ACR 对 ColorGrade 系列是"仅在非默认值时才写出"的，而不是像基本面板那样
# 全量写。因此这些字段标 observed_in_samples=False，并在 reader 中
# 把"字段缺失"一律视为默认值 0，而不是当作解析失败。
_int("ColorGradeShadowHue", 0, 360, "颜色分级 · 阴影 · 色相", "colorgrade", "阴影区染色色相",
     obs=False)
_int("ColorGradeShadowSat", 0, 100, "颜色分级 · 阴影 · 饱和度", "colorgrade", "阴影区染色强度",
     obs=False)
_int("ColorGradeShadowLum", -100, 100, "颜色分级 · 阴影 · 明亮度", "colorgrade", "阴影区明暗调整")
_int("ColorGradeMidtoneHue", 0, 360, "颜色分级 · 中间调 · 色相", "colorgrade",
     "中间调染色色相（人像肤色分离最常用）")
_int("ColorGradeMidtoneSat", 0, 100, "颜色分级 · 中间调 · 饱和度", "colorgrade", "中间调染色强度")
_int("ColorGradeMidtoneLum", -100, 100, "颜色分级 · 中间调 · 明亮度", "colorgrade", "中间调明暗调整")
_int("ColorGradeHighlightHue", 0, 360, "颜色分级 · 高光 · 色相", "colorgrade", "高光区染色色相",
     obs=False)
_int("ColorGradeHighlightSat", 0, 100, "颜色分级 · 高光 · 饱和度", "colorgrade", "高光区染色强度",
     obs=False)
_int("ColorGradeHighlightLum", -100, 100, "颜色分级 · 高光 · 明亮度", "colorgrade", "高光区明暗调整")
_int("ColorGradeGlobalHue", 0, 360, "颜色分级 · 全局 · 色相", "colorgrade", "全局染色色相")
_int("ColorGradeGlobalSat", 0, 100, "颜色分级 · 全局 · 饱和度", "colorgrade", "全局染色强度")
_int("ColorGradeGlobalLum", -100, 100, "颜色分级 · 全局 · 明亮度", "colorgrade", "全局明暗调整")
_int("ColorGradeBlending", 0, 100, "颜色分级 · 混合", "colorgrade",
     "三区染色的过渡平滑度，50 为默认")

# ============================================================================
# 细节面板（锐化与降噪）
# ============================================================================
_int("Sharpness", 0, 150, "细节 · 锐化 · 数量", "detail",
     "锐化数量；ACR 默认 40，范围上限 150")
_real("SharpenRadius", 0.5, 3.0, 1, "细节 · 锐化 · 半径", "detail",
      "锐化半径（像素）；ACR 默认 1.0")
_int("SharpenDetail", 0, 100, "细节 · 锐化 · 细节", "detail", "锐化细节保留程度")
_int("SharpenEdgeMasking", 0, 100, "细节 · 锐化 · 蒙版", "detail",
     "锐化边缘蒙版；正值把锐化限制在边缘，避免放大平滑区噪点")
_int("LuminanceSmoothing", 0, 100, "细节 · 减少杂色 · 明亮度", "detail", "明亮度降噪", ai=False)
_int("LuminanceNoiseReductionDetail", 0, 100, "细节 · 减少杂色 · 明亮度细节", "detail",
     "明亮度降噪的细节保留", obs=False, ai=False)
_int("LuminanceNoiseReductionContrast", 0, 100, "细节 · 减少杂色 · 明亮度对比", "detail",
     "明亮度降噪的对比保留", obs=False, ai=False)
_int("ColorNoiseReduction", 0, 100, "细节 · 减少杂色 · 颜色", "detail", "颜色降噪", ai=False)
_int("ColorNoiseReductionDetail", 0, 100, "细节 · 减少杂色 · 颜色细节", "detail", "颜色降噪细节", ai=False)
_int("ColorNoiseReductionSmoothness", 0, 100, "细节 · 减少杂色 · 颜色平滑度", "detail",
     "颜色降噪平滑度", ai=False)
# 降噪（全局 6 个 + 局部 LocalLuminanceNoise）一律 ai=False：
# 这是**技术校正**而不是调色方向 —— 该降多少取决于 ISO / 机身 / 是否长曝，模型看缩略图判不准，
# 用户的指令也很明确：降噪不要让 AI 动。程序保留读写能力（会原样保留用户已有设置）。

# ============================================================================
# 镜头校正面板
# ============================================================================
# 实测样本中出现两种"启用配置文件校正"的状态，判别依据见 reader.py：
#   默认（跟随机内）：LensProfileName="Camera Settings" + IsEmbedded="True"
#   用户显式选择：    LensProfileName="Adobe (Canon EF-S 18-135mm ...)" + LCP 文件名
#
# 【2026-09-23 步骤 4 的裁决】镜头这一组字段**全部由程序按每张照片自己的基线处理**，
# 不交 AI：ACR 会自己匹配配置文件（实测样本里既有 "Camera Settings"+IsEmbedded，
# 也有 "Adobe (…)-RAW.lcp"，还配着 LensProfileDigest 摘要），
# 我们去写 Name/Filename/Digest 只会让 ACR 认为"配置与摘要不匹配"；
# 而开关（是否启用、是否自动消除色差、畸变/晕影缩放、去边）应当保持每张照片
# 自己的状态（用户可能对某张主动关掉过）。实现见 acb/raw/lens.py：
# 逐图读基线 → 只补缺失的开关 → 已有值一律不覆盖。
_int("LensProfileEnable", 0, 1, "镜头校正 · 启用配置文件校正", "lens",
     "0=关闭，1=开启；由程序按逐图基线补缺，不交 AI（统一改写会覆盖用户的单张取舍）",
     ai=False)
_int("LensManualDistortionAmount", -100, 100, "镜头校正 · 扭曲度", "lens",
     "手动畸变校正（由程序按逐图基线补缺）", ai=False)
_int("AutoLateralCA", 0, 2, "镜头校正 · 消除色差", "lens",
     "横向色差校正：0=关闭，1=开启，2=自动（由程序按逐图基线补缺）", ai=False)
_int("VignetteAmount", -100, 100, "镜头校正 · 晕影", "lens",
     "镜头暗角补偿；负值压暗四角。"
     "**【2026-09-23 用户裁决】也收进程序组**：他改这里是他自己的判断，"
     "AI 只允许动「效果」菜单里的裁剪后晕影（PostCropVignetteAmount）",
     ai=False)
_int("LensProfileDistortionScale", 0, 200, "镜头校正 · 扭曲度缩放", "lens",
     "配置文件畸变校正的应用比例（程序按逐图基线处理）", ai=False)
_int("LensProfileVignettingScale", 0, 200, "镜头校正 · 晕影缩放", "lens",
     "配置文件晕影校正的应用比例（程序按逐图基线处理）", ai=False)
# 去边（Defringe）：整体由程序按逐图基线补缺；AI 不直接写这些色相边界与强度，
# 需要去边的意图由风格规则表达（提示词里已写明）。
for _defringe, _panel, _desc in (
    ("DefringePurpleAmount", "紫色数量", "紫边去除强度"),
    ("DefringePurpleHueLo", "紫色色相下限", "紫边判定色相下界"),
    ("DefringePurpleHueHi", "紫色色相上限", "紫边判定色相上界"),
    ("DefringeGreenAmount", "绿色数量", "绿边去除强度"),
    ("DefringeGreenHueLo", "绿色色相下限", "绿边判定色相下界"),
    ("DefringeGreenHueHi", "绿色色相上限", "绿边判定色相上界"),
):
    _int(_defringe, 0, 360 if "Hue" in _defringe else 100,
         f"镜头校正 · 去边 · {_panel}", "lens", f"{_desc}（程序按逐图基线处理）", ai=False)

# ============================================================================
# 变换面板（透视校正）
# ============================================================================
_int("PerspectiveUpright", 0, 4, "变换 · Upright", "perspective",
     "自动透视校正模式：0=关闭/基本，1=水平，2=垂直，3=完全，4=完整")
_int("PerspectiveVertical", -100, 100, "变换 · 垂直", "perspective", "垂直透视校正")
_int("PerspectiveHorizontal", -100, 100, "变换 · 水平", "perspective", "水平透视校正")
_real("PerspectiveRotate", -10.0, 10.0, 1, "变换 · 旋转", "perspective", "旋转角度（度）")
_int("PerspectiveAspect", -100, 100, "变换 · 长宽比", "perspective", "长宽比调整")
_int("PerspectiveScale", 1, 200, "变换 · 缩放", "perspective", "缩放百分比，100 为原始大小")
_real("PerspectiveX", -1.0, 1.0, 2, "变换 · X 位移", "perspective", "水平位移比例")
_real("PerspectiveY", -1.0, 1.0, 2, "变换 · Y 位移", "perspective", "垂直位移比例")

# ============================================================================
# 相机校准面板
# ============================================================================
_int("ShadowTint", -100, 100, "相机校准 · 阴影色调", "calibration", "阴影区绿色—洋红平衡")
for _c, _cn in (("Red", "红"), ("Green", "绿"), ("Blue", "蓝")):
    _int(f"{_c}Hue", -100, 100, f"相机校准 · {_cn}原色 · 色相", "calibration",
         f"{_cn}原色通道的色相偏移（影响整体色彩配方）")
    _int(f"{_c}Saturation", -100, 100, f"相机校准 · {_cn}原色 · 饱和度", "calibration",
         f"{_cn}原色通道的饱和度")

# ============================================================================
# 裁剪（本工具不主动裁剪，但需识别以避免破坏用户已有裁剪）
# ============================================================================
_real("CropTop", 0.0, 1.0, 4, "裁剪 · 上", "crop", "裁剪上边界（归一化）", ai=False, obs=False)
_real("CropLeft", 0.0, 1.0, 4, "裁剪 · 左", "crop", "裁剪左边界（归一化）", ai=False, obs=False)
_real("CropBottom", 0.0, 1.0, 4, "裁剪 · 下", "crop", "裁剪下边界（归一化）", ai=False, obs=False)
_real("CropRight", 0.0, 1.0, 4, "裁剪 · 右", "crop", "裁剪右边界（归一化）", ai=False, obs=False)
_real("CropAngle", -45.0, 45.0, 4, "裁剪 · 角度", "crop", "拉直角度（度）", ai=False, obs=False)
# 下面两个是**用户真实样本里出现过的未登记字段**（训练日志里报“含 2 个未登记的
# crs 字段”就是它们），属于裁剪工具的约束开关：
#   CropConstrainToUnitSquare —— 裁剪时强制 1:1（正方形）约束；
#   CropConstrainToWarp       —— 把裁剪边界约束在几何校正（Upright/变形）之后。
# 都是布尔标志，且**不是调色参数**：AI 不得改动，也不会被写进提示词。
# 登记它们的意义：不再在日志里报“未登记字段”（那是真信号，不能被噪音淹没），
# 且读-改-写时依旧原样保留。
_bool("CropConstrainToUnitSquare", "裁剪 · 约束", "crop",
      "裁剪时是否强制 1:1 正方形约束", ai=False, obs=False)
_bool("CropConstrainToWarp", "裁剪 · 约束", "crop",
      "裁剪边界是否约束在几何校正之后", ai=False, obs=False)

# ============================================================================
# 机器/版本相关字段：只保留、不生成（硬约束 c 的"AI 不得产出"清单）
# ============================================================================
# 这些字段若让 AI 乱写，最严重的后果是整份设置被 ACR 判为不支持而失效。
# 实测样本中 ProcessVersion 同时存在 15.4（PV6）与 11.0（PV5），
# 跨代混写会让同一批次的渲染不一致。
_enum("ProcessVersion", ("11.0", "15.4"), "（无面板，处理版本）", "machine",
      "ACR 处理版本；PV5=11.0，PV6=15.4。绝不修改，否则同批渲染不一致",
      ai=False)
_enum("CameraProfile",
      ("Camera Standard", "Camera Portrait", "Camera Landscape", "Camera Neutral",
       "Camera Faithful", "Camera Monochrome", "Adobe Standard", "Adobe Color",
       "Adobe Landscape", "Adobe Portrait", "Adobe Neutral", "Embedded"),
      "基本 · 配置文件", "machine",
      "相机配置文件；依赖本机已安装的相机配置文件，换机器可能失效，故不修改",
      ai=False)
_enum("LensProfileSetup", ("LensDefaults", "Custom"), "镜头校正 · 配置文件", "machine",
      "LensDefaults=跟随机内默认，Custom=用户显式选择；是训练模式的默认值判据之一",
      ai=False)
_bool("LensProfileIsEmbedded", "镜头校正 · 配置文件", "machine",
      "配置文件是否来自机内嵌入；与 LensProfileName=Camera Settings 共同构成默认判据",
      ai=False)
_bool("ConvertToGrayscale", "基本 · 转为灰度", "machine",
      "是否转为黑白；属输出决定而非调色参数，禁止 AI 改动以免误把照片变黑白", ai=False)
_bool("OverrideLookVignette", "（相机配置文件 · Look）", "machine", "Look 的晕影覆盖开关",
      ai=False)
_bool("HasSettings", "（无面板）", "machine", "是否含 ACR 设置", ai=False)
_bool("HasCrop", "（无面板）", "machine", "是否含裁剪", ai=False)
_bool("AlreadyApplied", "（无面板）", "machine", "设置是否已被应用", ai=False)


# ============================================================================
# 效果面板 · Glow（ACR 16.3+ 新增）
# ============================================================================
# 这是"漏字段门禁"抓到的真实案例，值得留个记录：
#   在 test_data 的样本上首次运行 tools/dump_crs_fields.py 时，Glow 被报告为
#   "未登记字段"。它是 ACR 16.3 / Lightroom Classic 13.3 起在「效果」面板
#   新增的「光晕」滑块（与清晰度相反，向暗部渗出柔光）。
#   如果不登记，AI 永远不会提出调整它，用户也会困惑"为什么这个参数没反应"。
#   这正是门禁存在的意义：ACR 版本升级引入新字段时，我们能在自己的测试样本上
#   立刻发现，而不是等用户投诉"某个滑块怎么调都没用"。
_int("Glow", -100, 100, "效果 · 光晕", "effects",
     "光晕，正值向暗部渗出柔光产生朦胧感，负值增加通透度；"
     "与清晰度方向相反，是 ACR 16.3+ 新增的效果滑块")

# ---------------------------------------------------------------------------
# 【2026-09-23 用户实测值域】用户在 ACR 里把这几个滑块拉到了两端，三份样本给出：
#     GlowRange   0 / 50 / 100        → 0..100
#     GlowSpread  0 / 50 / 100        → 0..100
#     GlowWarmth -100 / 0 / +100      → -100..100（确实是带号的）
#     GlowStyle   0 / 1 / 2           → 0..2
# 前三个是幅度滑块，值域已实测 → **开放给 AI**（不再是我猜的范围）。
# GlowStyle 是"样式选择"，0/1/2 各自的含义还没有文档，无法告诉模型该选哪个，
# 所以继续只保留不生成；用户哪天在 ACR 里看清三个样式的名字，再开放。
_int("GlowRange", 0, 100, "效果 · 光晕 · 范围", "effects",
     "光晕作用范围（用户样本实测值域 0..100）")
_int("GlowSpread", 0, 100, "效果 · 光晕 · 扩散", "effects",
     "光晕扩散程度（用户样本实测值域 0..100）")
_int("GlowWarmth", -100, 100, "效果 · 光晕 · 暖色", "effects",
     "光晕的暖/冷倾向，正值偏暖（用户样本实测值域 -100..100）")
# GlowStyle 的三种取值（用户 2026-09-23 在 ACR 里看名字告诉我）：
#   0 = 漫射（Diffuse）、1 = 光华（Glow）、2 = 光晕（Halo）
# 含义已知 → 登记为枚举（写入的值就是 "0"/"1"/"2"）并开放给 AI。
_enum("GlowStyle", ("0", "1", "2"), "效果 · 光晕 · 样式", "effects",
      "光晕样式：0=漫射（柔而不聚焦）、1=光华（中心发光感）、2=光晕（明显光晕圈）。"
      "用户训练样本里三种都用过，取值就是这三个数字字符串")
_int("CurveRefineSaturation", 0, 100, "色调曲线 · 精简饱和度", "curve",
     "点曲线的饱和度精简（样本实测 0..100；与局部版 LocalCurveRefineSaturation 不是一回事）",
     ai=False)
_int("VignetteMidpoint", 0, 100, "镜头校正 · 晕影 · 中点", "lens",
     "镜头晕影的影响范围中点（样本实测 0..100）；晕影整体由程序按「总预算」控制，不交 AI",
     ai=False)
_str("PointColors", "（ACR 内部数据）", "machine",
     "一组 19 个浮点的序列（元素型），值形如 -1.000000；只保留不生成", ai=False)
_str("ColorVariance", "（ACR 内部数据）", "machine",
     "一组浮点的序列（元素型），只保留不生成", ai=False)


# ============================================================================
# Upright 透视自动校正的内部状态（实测由 PerspectiveUpright 触发后写入）
# ============================================================================
# 用户开启 Upright 自动校正后，ACR 会把求出的变换矩阵与预览参数写进 XMP。
# 这些值互相之间必须自洽（例如 UprightTransformCount 必须等于实际 _N 的个数，
# UprightDependentDigest 必须与矩阵内容匹配），否则 ACR 会判为不一致而重新计算。
# 因此它们一律只保留、不生成。
_int("UprightVersion", 0, 2147483647, "变换 · Upright · 版本", "machine",
     "Upright 算法的内部版本号", ai=False)
_int("UprightCenterMode", 0, 2, "变换 · Upright · 中心模式", "machine",
     "Upright 投影中心模式", ai=False)
_real("UprightCenterNormX", 0.0, 1.0, 6, "变换 · Upright · 中心 X", "machine",
      "Upright 投影中心归一化 X", ai=False)
_real("UprightCenterNormY", 0.0, 1.0, 6, "变换 · Upright · 中心 Y", "machine",
      "Upright 投影中心归一化 Y", ai=False)
_real("UprightFocalLength35mm", 0.0, 2000.0, 4, "变换 · Upright · 等效焦距", "machine",
      "Upright 计算使用的 35mm 等效焦距", ai=False)
_int("UprightFocalMode", 0, 1, "变换 · Upright · 焦距模式", "machine",
     "是否使用自定义焦距", ai=False)
_bool("UprightPreview", "变换 · Upright · 预览", "machine",
      "是否为 Upright 预览状态", ai=False)
_int("UprightTransformCount", 0, 10, "变换 · Upright · 变换数量", "machine",
     "UprightTransform_N 的个数；与实际个数不匹配会导致 ACR 判为不一致", ai=False)
_str("UprightDependentDigest", "变换 · Upright · 摘要", "machine",
     "Upright 变换矩阵的内容摘要；写错会让 ACR 认为矩阵与摘要不匹配而重算", ai=False)

# 说明：UprightTransform_0 / _1 / _2 … 的**个数是变量**，因此不逐个登记，
# 而是通过下面的 DYNAMIC_FIELD_PREFIXES 机制按前缀识别（见 get_field 的实现）。
# 它们的值是 9 个浮点数组成的 3×3 变换矩阵，形如：
#     "1.001822480,-0.000359055,-0.001204664,-0.003045442,0.997074053,..."


# ============================================================================
# 局部调整（蒙版）体系 —— 整块登记
# ============================================================================
# 这些字段全部位于 <crs:MaskGroupBasedCorrections> **内部**（嵌套元素里的属性），
# 而"漏字段门禁"只扫顶层 rdf:Description 的属性，所以它们从来不会出现在门禁报警里。
# 仍然必须登记，有三个理由：
#   1) 写蒙版时要校验范围（唯一范围来源就是这张表，见 acb/xmp/masks.py 的 _LOCAL_RANGES）；
#   2) 以后要把"蒙版意图"开放给 AI 时，需要一张现成的白名单；
#   3) 撞名风险要在这里一次性看清（例如径向渐变的 Top/Left/Bottom/Right 与
#      变换面板的字段名很像）。
#
# 【现阶段一律 ai=False】AI 不许直接返回蒙版几何与局部参数：
# 局部调整由程序按"风格 + 这张图的场景"生成（见 acb/xmp/masks.py），
# 让模型直接编 41 个 Local* 数值正是我们这一轮要治的病（照抄统计中位数）。
#
# 【单位：对外一律用 ACR 面板值】普通滑块 -100..100、局部曝光 -4..4（EV）、
# 颜色分级色相 0..360。写入 XMP 时再换算成存储值（÷100 / ÷4 / 原样），
# 依据是 2026-09-23 用户在 ACR 里的实测（写 -0.4 → 面板显示 -40；写 1 → 面板 +4.00）。
def _local(
    name: str,
    lo: float,
    hi: float,
    panel: str,
    desc: str,
    *,
    kind: str = KIND_INT,
) -> None:
    """登记一个局部（蒙版）参数：面板值域，只保留不生成。"""
    _SPECS.append(
        FieldSpec(
            name=name,
            kind=kind,
            panel=panel,
            group="mask",
            minimum=lo,
            maximum=hi,
            decimals=1 if kind == KIND_REAL else 0,
            ai_writable=False,
            observed_in_samples=True,
            description=desc,
        )
    )


# 基础 27 项：ACR 在"有调整的局部"上总是全部写出（缺省 0，仅曲线饱和度写 100）。
_local("LocalExposure", -4.0, 4.0, "局部 · 曝光（旧版）", "旧版局部曝光，单位 EV",
       kind=KIND_REAL)
_local("LocalHue", -100, 100, "局部 · 色相", "旧版局部色相偏移")
_local("LocalSaturation", -100, 100, "局部 · 饱和度", "旧版局部饱和度")
_local("LocalContrast", -100, 100, "局部 · 对比度", "旧版局部对比度")
_local("LocalClarity", -100, 100, "局部 · 清晰度", "旧版局部清晰度")
_local("LocalSharpness", -100, 100, "局部 · 锐化程度", "旧版局部锐化")
_local("LocalBrightness", -150, 150, "局部 · 亮度（旧版）", "旧版局部亮度（面板 ±150）")
_local("LocalToningHue", -100, 100, "局部 · 分离色调 · 色相", "旧版局部分离色调色相")
_local("LocalToningSaturation", -100, 100, "局部 · 分离色调 · 饱和度", "旧版局部分离色调饱和度")
_local("LocalExposure2012", -4.0, 4.0, "局部 · 曝光", "局部曝光，单位 EV（面板 ±4EV）",
       kind=KIND_REAL)
_local("LocalContrast2012", -100, 100, "局部 · 对比度", "局部对比度")
_local("LocalHighlights2012", -100, 100, "局部 · 高光", "局部高光")
_local("LocalShadows2012", -100, 100, "局部 · 阴影", "局部阴影")
_local("LocalWhites2012", -100, 100, "局部 · 白色", "局部白场")
_local("LocalBlacks2012", -100, 100, "局部 · 黑色", "局部黑场")
_local("LocalClarity2012", -100, 100, "局部 · 清晰度", "局部清晰度")
_local("LocalDehaze", -100, 100, "局部 · 去薄雾", "局部去薄雾")
_local("LocalLuminanceNoise", -100, 100, "局部 · 明亮度降噪", "局部明亮度降噪")
_local("LocalMoire", -100, 100, "局部 · 摩尔纹", "局部摩尔纹消除")
_local("LocalDefringe", -100, 100, "局部 · 去边", "局部去色差")
_local("LocalTemperature", -100, 100, "局部 · 色温", "局部色温（相对值，非开尔文）")
_local("LocalTint", -100, 100, "局部 · 色调", "局部色调（绿—洋红轴）")
_local("LocalTexture", -100, 100, "局部 · 纹理", "局部纹理")
_local("LocalGrain", -100, 100, "局部 · 颗粒", "局部颗粒")
_local("LocalCorrectedDepth", -100, 100, "局部 · 校正后景深", "局部景深相关（少用）")
_local("LocalGlow", -100, 100, "局部 · 光晕", "局部光晕")
_local("LocalCurveRefineSaturation", 0, 100, "局部 · 曲线精简饱和度",
       "局部曲线的饱和度精简；缺省值就是 100（与全局 CurveRefineSaturation 不同）")

# 颜色分级 14 项：ACR 只在用户真的动过它们时才写（样本第 1 组有、第 2~5 组没有）。
_local("LocalColorGradeShadowHue", 0, 360, "局部 · 颜色分级 · 阴影色相", "阴影区色相")
_local("LocalColorGradeShadowSat", -100, 100, "局部 · 颜色分级 · 阴影饱和度", "阴影区饱和度")
_local("LocalColorGradeHighlightHue", 0, 360, "局部 · 颜色分级 · 高光色相", "高光区色相")
_local("LocalColorGradeHighlightSat", -100, 100, "局部 · 颜色分级 · 高光饱和度", "高光区饱和度")
_local("LocalColorGradeBalance", -100, 100, "局部 · 颜色分级 · 平衡", "阴影/高光权重平衡")
_local("LocalColorGradeMidtoneHue", 0, 360, "局部 · 颜色分级 · 中间调色相", "中间调色相")
_local("LocalColorGradeMidtoneSat", -100, 100, "局部 · 颜色分级 · 中间调饱和度", "中间调饱和度")
_local("LocalColorGradeShadowLum", -100, 100, "局部 · 颜色分级 · 阴影亮度", "阴影区亮度")
_local("LocalColorGradeMidtoneLum", -100, 100, "局部 · 颜色分级 · 中间调亮度", "中间调亮度")
_local("LocalColorGradeHighlightLum", -100, 100, "局部 · 颜色分级 · 高光亮度", "高光区亮度")
_local("LocalColorGradeBlending", 0, 100, "局部 · 颜色分级 · 混合", "颜色分级混合程度")
_local("LocalColorGradeGlobalHue", 0, 360, "局部 · 颜色分级 · 全局色相", "全局色相")
_local("LocalColorGradeGlobalSat", -100, 100, "局部 · 颜色分级 · 全局饱和度", "全局饱和度")
_local("LocalColorGradeGlobalLum", -100, 100, "局部 · 颜色分级 · 全局亮度", "全局亮度")

# --- 结构性字段（元素名与"这个元素是什么"的标记）----------------------------
_int("CorrectionAmount", 0, 1, "局部 · 强度", "mask", "整组局部调整的强度（样本恒为 1）", ai=False)
_bool("CorrectionActive", "局部 · 启用", "mask", "该组是否启用", ai=False)
_str("CorrectionName", "局部 · 名称", "mask", "该组在蒙版面板里的名字（如「蒙版 1」）")
_str("CorrectionSyncID", "局部 · 同步 ID", "mask",
     "32 位十六进制随机 ID，用于跨图同步；撞 ID 会让 Lightroom 同步出错", ai=False)
_bool("MaskActive", "局部 · 蒙版启用", "mask", "该蒙版是否启用", ai=False)
_str("MaskName", "局部 · 蒙版名称", "mask", "蒙版名字（如「画笔 1」「径向渐变 2」）")
_int("MaskBlendMode", 0, 10, "局部 · 蒙版混合模式", "mask",
     "蒙版混合模式（样本为 0；取值含义未获文档）", ai=False)
_bool("MaskInverted", "局部 · 蒙版反相", "mask", "蒙版是否反相", ai=False)
_str("MaskSyncID", "局部 · 蒙版同步 ID", "mask", "蒙版的同步 ID（与 CorrectionSyncID 同理）", ai=False)
_real("MaskValue", 0.0, 1.0, 6, "局部 · 蒙版值", "mask",
      "蒙版本身的取值（几何蒙版为 1；画笔为覆盖度，样本里是 0.859813）", ai=False)
_str("What", "（蒙版类型标记）", "mask",
     "元素类型标记：Correction / Mask/Gradient / Mask/CircularGradient / "
     "Mask/Aggregate / Mask/Paint / Mask/RangeMask / Mask/Image", ai=False)

# --- 几何属性（线性渐变与径向渐变）----------------------------------------
_real("ZeroX", 0.0, 1.0, 6, "局部 · 线性渐变 · 起点 X", "mask",
      "线性渐变 0% 端的归一化 X（**存储帧**坐标，见 acb/raw/orientation.py）", ai=False)
_real("ZeroY", 0.0, 1.0, 6, "局部 · 线性渐变 · 起点 Y", "mask", "线性渐变 0% 端的归一化 Y", ai=False)
_real("FullX", 0.0, 1.0, 6, "局部 · 线性渐变 · 终点 X", "mask", "线性渐变 100% 端的归一化 X", ai=False)
_real("FullY", 0.0, 1.0, 6, "局部 · 线性渐变 · 终点 Y", "mask", "线性渐变 100% 端的归一化 Y", ai=False)
_real("Top", 0.0, 1.0, 6, "局部 · 径向渐变 · 上", "mask", "径向渐变外接框上边界（归一化）", ai=False)
_real("Left", 0.0, 1.0, 6, "局部 · 径向渐变 · 左", "mask", "径向渐变外接框左边界（归一化）", ai=False)
_real("Bottom", 0.0, 1.0, 6, "局部 · 径向渐变 · 下", "mask", "径向渐变外接框下边界（归一化）", ai=False)
_real("Right", 0.0, 1.0, 6, "局部 · 径向渐变 · 右", "mask", "径向渐变外接框右边界（归一化）", ai=False)
_real("Angle", -180.0, 180.0, 6, "局部 · 径向渐变 · 角度", "mask",
      "径向渐变旋转角（样本为 0；值域未获文档）", ai=False)
_int("Midpoint", 0, 100, "局部 · 径向渐变 · 中点", "mask", "椭圆羽化中点（样本 50）", ai=False)
_int("Roundness", -100, 100, "局部 · 径向渐变 · 圆度", "mask", "椭圆圆度（样本 0）", ai=False)
_int("Feather", 0, 100, "局部 · 径向渐变 · 羽化", "mask", "羽化强度（样本 88 = 很柔）", ai=False)
_bool("Flipped", "局部 · 径向渐变 · 翻转", "mask", "椭圆的翻转标记（样本 true）", ai=False)

# --- 画笔属性与落点 ---------------------------------------------------------
_real("Radius", 0.0, 1.0, 6, "局部 · 画笔 · 半径", "mask",
      "画笔半径（归一化到画面长边；样本 0.128475）", ai=False)
_real("Flow", 0.0, 1.0, 6, "局部 · 画笔 · 流量", "mask", "画笔流量（样本 0.648411）", ai=False)
_real("CenterWeight", -1.0, 1.0, 6, "局部 · 画笔 · 中心权重", "mask",
      "画笔中心权重（样本 0）", ai=False)

# --- 元素容器 ---------------------------------------------------------------
# 注意：这些是**元素名**而不是属性名，登记它们是为了让"已知字段"这套判据
# 也能覆盖元素层（未来若有扫描元素的检查，不会把它们当未知字段刷屏）。
_str("MaskGroupBasedCorrections", "（蒙版容器）", "mask",
     "所有局部调整（蒙版）的容器元素，挂在主 rdf:Description 下", ai=False)
_str("CorrectionMasks", "（蒙版容器）", "mask", "某一组局部调整内部的蒙版列表", ai=False)
_str("Masks", "（蒙版容器）", "mask", "画笔蒙版内部的子蒙版列表（Mask/Aggregate 才有）", ai=False)
_str("Dabs", "（画笔落点）", "mask",
     "画笔落点序列：r 半径 / f 流量 / d x y 三种前缀，逐点成对", ai=False)
_str("MainCurve", "局部 · 曲线", "mask", "某一组局部调整自己的点曲线（样本第 1 组有）", ai=False)
_str("CorrectionRangeMask", "局部 · 范围蒙版", "mask",
     "范围蒙版（颜色/明亮度）的内部参数；程序**不生成**，只保留", ai=False)
_str("RangeMaskMapInfo", "局部 · 范围蒙版映射", "mask",
     "范围蒙版的映射表（RGBMin/Max、LabMin/Max、LumEq），依赖 ACR 用图像算出的内容", ai=False)
_str("LumEq", "局部 · 亮度映射", "mask", "范围蒙版的 33 点亮度映射表", ai=False)
_str("PointModels", "（ACR 内部数据）", "mask",
     "内部模型标记（出现在样本的点曲线区域），只保留", ai=False)
_int("Type", 1, 2, "局部 · 范围蒙版类型", "mask",
     "范围蒙版类型：1=颜色范围，2=明亮度范围", ai=False)


# ============================================================================
# 其他机器/内部状态字段（实测出现在样本中）
# ============================================================================
_str("Version", "（无面板）", "machine",
     "ACR 设置结构版本，实测样本中从 15.0 到 18.6 都有；与 ProcessVersion 是两回事",
     ai=False)
_int("CompatibleVersion", 0, 2147483647, "（无面板）", "machine",
     "设置兼容性版本号（实测 251658240 = 0x0F000000，代表 ACR 15.0 能读）", ai=False)
_str("CameraProfileDigest", "基本 · 配置文件", "machine",
     "相机配置文件的内容摘要；用于判断配置文件是否与本机已安装的一致", ai=False)
_str("LensProfileDigest", "镜头校正 · 配置文件", "machine",
     "镜头配置文件（.lcp）的摘要，作用同上", ai=False)
_str("LensProfileFilename", "镜头校正 · 配置文件", "machine",
     "镜头配置文件文件名，例如 'Canon EOS 7D (Canon EF-S 18-135mm f3.5-5.6 IS USM) - RAW.lcp'",
     ai=False)
_str("LensProfileName", "镜头校正 · 配置文件", "machine",
     "'Camera Settings' 表示跟随机内默认；'Adobe (...)' 形式表示用户显式选择，"
     "是训练模式区分镜头校正取舍的关键判据", ai=False)
_int("AllowFilters", 0, 1, "（无面板）", "machine",
     "是否允许智能滤镜（PS 往返相关）", ai=False)
_int("HDREditMode", 0, 2, "（无面板）", "machine",
     "HDR 编辑模式标记（ACR 的 HDR 显示模式）", ai=False)
_int("GrainSeed", 0, 2147483647, "效果 · 颗粒 · 随机种子", "machine",
     "颗粒的随机种子；改动它会改变颗粒图案的分布", ai=False)
_int("ReshapeAmount", -100, 100, "（几何 · 面部塑形）", "machine",
     "面部塑形强度（ACR 的人像几何调整，属机器相关，需依赖人脸检测上下文）", ai=False)
_int("PostCropVignetteStyle", 1, 3, "效果 · 裁剪后晕影 · 样式", "effects",
     "晕影样式：1=高光优先，2=颜色优先，3=绘画叠加", ai=False)


# ============================================================================
# 汇总表
# ============================================================================
FIELDS: dict[str, FieldSpec] = {spec.name: spec for spec in _SPECS}

# 只写不生成的"机器相关"字段名集合（与 constants.AI_FORBIDDEN_FIELDS 语义一致，
# 但这里额外包含只有本表登记的字段，例如 LensProfileDistortionScale）。
PRESERVE_ONLY_FIELDS: frozenset[str] = frozenset(
    name for name, spec in FIELDS.items() if not spec.ai_writable
)

# AI 可写字段（即允许出现在 JSON Schema 与提示词中的字段）
AI_WRITABLE_FIELDS: dict[str, FieldSpec] = {
    name: spec for name, spec in FIELDS.items() if spec.ai_writable
}

# 曲线字段（元素型，单独处理）
CURVE_FIELDS: tuple[str, ...] = (
    "ToneCurvePV2012",
    "ToneCurvePV2012Red",
    "ToneCurvePV2012Green",
    "ToneCurvePV2012Blue",
)

# 曲线名称字段 → 曲线点字段的对应关系。
# 写入曲线点时，也必须同步把名称置为 Custom，否则 ACR 会继续按预设名渲染而忽略点。
CURVE_NAME_FOR: dict[str, str] = {
    "ToneCurvePV2012": "ToneCurveName2012",
    "ToneCurvePV2012Red": "ToneCurveName2012",
    "ToneCurvePV2012Green": "ToneCurveName2012",
    "ToneCurvePV2012Blue": "ToneCurveName2012",
}

# 字段分组（用于提示词分区呈现与训练统计分区）。顺序即提示词中的呈现顺序。
FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    "wb": ("WhiteBalance", "Temperature", "Tint"),
    "basic": (
        "Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012",
        "Whites2012", "Blacks2012", "Vibrance", "Saturation",
    ),
    "parametric": (
        "ParametricShadows", "ParametricDarks", "ParametricLights", "ParametricHighlights",
        "ParametricShadowSplit", "ParametricMidtoneSplit", "ParametricHighlightSplit",
    ),
    "effects": (
        "Texture", "Clarity2012", "Dehaze", "Glow", "GlowRange", "GlowSpread", "GlowWarmth",
        "GlowStyle", "PostCropVignetteAmount", "PostCropVignetteStyle",
        "GrainAmount", "GrainSize", "GrainFrequency",
    ),
    "curve": ("ToneCurveName2012",),
    "hsl": tuple(
        f"{kind}Adjustment{color}"
        for kind in ("Hue", "Saturation", "Luminance")
        for color in ("Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta")
    ),
    "split": (
        "SplitToningShadowHue", "SplitToningShadowSaturation",
        "SplitToningHighlightHue", "SplitToningHighlightSaturation", "SplitToningBalance",
    ),
    "colorgrade": (
        "ColorGradeShadowHue", "ColorGradeShadowSat", "ColorGradeShadowLum",
        "ColorGradeMidtoneHue", "ColorGradeMidtoneSat", "ColorGradeMidtoneLum",
        "ColorGradeHighlightHue", "ColorGradeHighlightSat", "ColorGradeHighlightLum",
        "ColorGradeGlobalHue", "ColorGradeGlobalSat", "ColorGradeGlobalLum",
        "ColorGradeBlending",
    ),
    "detail": (
        "Sharpness", "SharpenRadius", "SharpenDetail", "SharpenEdgeMasking",
        "LuminanceSmoothing", "ColorNoiseReduction",
        "ColorNoiseReductionDetail", "ColorNoiseReductionSmoothness",
    ),
    "lens": (
        "LensProfileEnable", "LensManualDistortionAmount", "AutoLateralCA", "VignetteAmount",
        "DefringePurpleAmount", "DefringeGreenAmount",
    ),
    "perspective": (
        "PerspectiveUpright", "PerspectiveVertical", "PerspectiveHorizontal",
        "PerspectiveRotate", "PerspectiveAspect", "PerspectiveScale",
    ),
    "calibration": (
        "ShadowTint",
        "RedHue", "RedSaturation", "GreenHue", "GreenSaturation", "BlueHue", "BlueSaturation",
    ),
}

GROUP_LABELS: dict[str, str] = {
    "wb": "白平衡",
    "basic": "基本影调与偏好",
    "parametric": "参数曲线",
    "effects": "效果（纹理/清晰度/去薄雾/光晕/晕影/颗粒）",
    "curve": "点曲线名称",
    "hsl": "HSL 颜色混合（色相/饱和度/明亮度三组×8色）",
    "split": "分离色调",
    "colorgrade": "颜色分级（阴影/中间调/高光/全局）",
    "detail": "细节（锐化与降噪）",
    "lens": "镜头校正（配置文件/畸变/色差/晕影/去边）",
    "perspective": "变换（透视校正）",
    "calibration": "相机校准（阴影色调与三原色）",
    # mask 分组不会出现在给 AI 的提示词里（现阶段全部 ai_writable=False）：
    # 局部调整由程序按"风格 + 场景"生成，不让模型直接编 41 个 Local* 数值。
    "mask": "局部调整（蒙版）——程序生成，不交 AI",
    # "machine" 分组不会出现在给 AI 的提示词里（下面 render_field_reference 会跳过），
    # 这里登记标签只是为了日志与调试输出可读。
    "machine": "机器/版本/内部状态（只保留，不生成）",
}


def get_field(name: str) -> FieldSpec | None:
    """按字段名取定义；未登记返回 None（调用方据此拒收）。

    除了 FIELDS 里逐个登记的字段，还支持**按前缀识别**的动态字段。
    为什么需要动态字段：
        dump_crs_fields.py 在真实样本里抓到两类"名字数量不固定"的字段：
          Table_A8320194E3EFB1B3FD3D5ACEC3C27E51
              —— ACR 用它承载一段不透明/混淆编码的数据块
                 （内容形如 "n5000pY/cqgVcVfV.v5/PunbM=..." 的乱码字符串）。
                 哈希后缀取决于内容，因此名字无法预先枚举。
          UprightTransform_0 .. _N
              —— Upright 透视校正求出的 3×3 变换矩阵，
                 个数由 UprightTransformCount 决定（实测为 6 个）。
        如果把它们当作"未知字段"，日志会被刷屏、门禁会永远报警，
        真正的异常反而被淹没。因此按前缀识别并统一标记为
        "机器内部数据、只保留不生成"。
    """
    spec = FIELDS.get(name)
    if spec is not None:
        return spec
    return _dynamic_spec(name)


# 动态字段前缀表：(前缀, 说明)。
DYNAMIC_FIELD_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Table_", "ACR 内部的混淆/不透明数据块（内容不可解析，必须原样保留）"),
    ("UprightTransform_", "Upright 变换矩阵：9 个浮点数组成的 3×3 矩阵"),
)

# 动态字段的 spec 缓存：避免每次访问都新建对象（字段查找在批量循环里很频繁）。
_dynamic_spec_cache: dict[str, FieldSpec] = {}


def _dynamic_spec(name: str) -> FieldSpec | None:
    """按前缀合成动态字段定义。"""
    for prefix, description in DYNAMIC_FIELD_PREFIXES:
        if name.startswith(prefix):
            cached = _dynamic_spec_cache.get(name)
            if cached is not None:
                return cached
            spec = FieldSpec(
                name=name,
                kind=KIND_STR,
                panel="（ACR 内部数据）",
                group="machine",
                ai_writable=False,
                observed_in_samples=True,
                description=description,
            )
            _dynamic_spec_cache[name] = spec
            return spec
    return None


def is_known(name: str) -> bool:
    """字段是否已登记（含动态前缀字段）。"""
    return get_field(name) is not None


# ============================================================================
# 取值格式化：把 Python 值转成 ACR 期望的字符串形式
# ============================================================================

def format_value(spec: FieldSpec, value: Any) -> str:
    """把 Python 值格式化为 ACR 期望的属性字符串。

    细节依据（全部来自真实样本的书写风格）：
      - 整数正数带显式 + 号：样本里是 Tint="+16"、Vibrance="+28"、SaturationAdjustmentRed="+4"，
        而 0 与负数不带号：Saturation="-1"、VignetteAmount="0"。
      - 实数按字段固定小数位：Exposure2012="0.00"（2 位）、SharpenRadius="+1.0"（1 位）。
        固定小数位很重要——若写成 "1" 或 "1.0000"，虽然多数情况下 ACR 能容错，
        但会让我们的产物与 ACR 自己写的文件产生无意义的 diff，干扰用户的版本比对。
      - 布尔必须是大写 "True"/"False"：样本中 ConvertToGrayscale="False"、
        HasSettings="True"。写成小写 "true" 有被当作非法值忽略的风险。
    """
    if spec.kind == KIND_BOOL:
        return "True" if bool(value) else "False"

    if spec.kind == KIND_ENUM:
        return str(value)

    if spec.kind == KIND_INT:
        ivalue = int(round(float(value)))
        return f"+{ivalue}" if ivalue > 0 else str(ivalue)

    if spec.kind == KIND_REAL:
        fvalue = float(value)
        # 先按字段小数位格式化，再决定是否加 + 号（负号自带）。
        text = f"{fvalue:.{spec.decimals}f}"
        if fvalue > 0:
            return f"+{text}"
        return text

    if spec.kind == KIND_STR:
        # 不透明字符串原样写出。这类字段 ai_writable=False，
        # 因此本分支实际只在"用户手工传入、或将来放开某字段"时被走到。
        return str(value)

    raise ValueError(f"字段 {spec.name} 的类型 {spec.kind} 不支持属性格式化")


def parse_value(spec: FieldSpec, text: str) -> Any:
    """把 XMP 里的属性字符串解析为 Python 值。

    容错说明：Python 的 float() 原生接受前导 "+"（float("+16") == 16.0），
    因此不需要预先剥掉加号——这也是我们选择"带 + 号书写"却仍能简单解析的原因。
    """
    raw = (text or "").strip()

    if spec.kind == KIND_BOOL:
        # ACR 写的是 "True"/"False"；同时也接受小写与 1/0，以兼容其他工具。
        return raw.lower() in ("true", "1", "yes")

    if spec.kind in (KIND_ENUM, KIND_STR):
        return raw

    if spec.kind == KIND_INT:
        try:
            return int(round(float(raw)))
        except ValueError:
            return 0

    if spec.kind == KIND_REAL:
        try:
            return float(raw)
        except ValueError:
            return 0.0

    raise ValueError(f"字段 {spec.name} 的类型 {spec.kind} 不支持属性解析")


def clamp(spec: FieldSpec, value: Any) -> Any:
    """把越界值夹到合法区间。

    为什么夹取而不是报错：模型的数值输出偶尔会轻微越界（例如把曝光写成 5.02），
    这种误差不值得整张重试；但**越界幅度过大**（例如 500）说明模型理解错了，
    那应该由 validator 判为非法并触发重试。
    因此本函数只做"小幅修正"，判断门槛由 validator 控制。
    """
    if spec.kind == KIND_INT:
        ivalue = int(round(float(value)))
        lo = int(spec.minimum) if spec.minimum is not None else ivalue
        hi = int(spec.maximum) if spec.maximum is not None else ivalue
        return max(lo, min(hi, ivalue))
    if spec.kind == KIND_REAL:
        fvalue = round(float(value), spec.decimals)
        lo = float(spec.minimum) if spec.minimum is not None else fvalue
        hi = float(spec.maximum) if spec.maximum is not None else fvalue
        return max(lo, min(hi, fvalue))
    return value
