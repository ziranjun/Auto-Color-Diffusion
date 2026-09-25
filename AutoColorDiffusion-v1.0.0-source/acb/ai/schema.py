# -*- coding: utf-8 -*-
"""JSON Schema 构造（硬约束 #5）。

要求原文：AI 输出必须用 JSON Schema 约束，字段名须为 ACR 的 crs: 前缀字段名，
并给出取值范围与类型。

设计取舍（strict 模式）
----------------------
OpenAI 的 strict json_schema 要求 schema 里每个 properties 的键都出现在
required 中。本项目有约 90 个可写字段，若开 strict：
    - 模型每张图都必须把 90 个字段完整吐一遍（未调整的填 null），
    - 单图输出约 1000+ token，100 张的批次就是十万级 token。
成本与延迟都不可接受，且大量 null 反而增加模型"手滑填错"的机会。

因此默认 strict=False：
    - schema 仍然随请求下发，用于**引导格式**与**暴露取值范围/面板语义**；
    - 真正的把关者是本地 pydantic 强校验（validator.py）——
      未登记字段、越界值、类型错误都会被拦住并触发重试。
用户可在 models.yaml 里把 json_schema_strict 打开以换取确定性（代价是 token）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..xmp import fields as F
from ..xmp.masks import AI_LOCAL_RANGES, MASK_KINDS, PROGRAM_MAX_MASKS

# 曲线坐标在提示词/响应里统一用 0..1 浮点域（便于模型理解），
# 写回时由 writer.normalize_curve_points 映射回 ACR 的 0..255 整数域。
CURVE_MIN = 0.0
CURVE_MAX = 1.0


# 严格模式（structured outputs）**不支持**的 schema 关键字。
#
# 依据（2026-09 核对官方文档）：
#   · OpenAI / Azure OpenAI《structured outputs》：不支持
#     - 字符串：minLength / maxLength / pattern / format
#     - 数字：minimum / maximum / multipleOf
#     - 对象：patternProperties / unevaluatedProperties / propertyNames /
#             minProperties / maxProperties
#     - 数组：unevaluatedItems / contains / minContains / maxContains /
#             minItems / maxItems / uniqueItems
#   · Kimi 的 Structured Output 要求 schema 符合 MFJS 规范（不符合会报错或 warning）。
#
# 本项目的字段带 minimum/maximum（取值范围），曲线带 minItems/maxItems ——
# 因此**下发给服务端的 schema 必须先剥掉这些关键字**，否则 strict 模式会被拒。
# 剥掉不会丢信息：这些范围本来就已经写在系统提示词的字段参考表里
# （render_field_reference 带 range_text），而且真正的把关者是本地强校验。
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "minLength", "maxLength", "pattern", "format",
        "minimum", "maximum", "multipleOf",
        "patternProperties", "unevaluatedProperties", "propertyNames",
        "minProperties", "maxProperties",
        "unevaluatedItems", "contains", "minContains", "maxContains",
        "minItems", "maxItems", "uniqueItems",
    }
)


@dataclass(frozen=True)
class SchemaDialect:
    """某个端点能收哪些 schema 关键字 —— "方言"。

    【为什么要做成每家一份，而不是全局一套】
    全局一套的意思是"这家的限制会连带削弱另一家"：
      · OpenAI 的 structured outputs 不认 minimum/maxItems（全局，见上面的集合）；
      · 但 Kimi 的 MFJS 规范连 `anyOf` 都不支持；
      · 以后接第五家还会有别的差异（pattern / format / $ref …）。
    写成每家一份，接新家时只改配置（`forbidden_schema_keywords` /
    `allow_anyof_in_schema`），不必动这个文件。

    `allow_anyof=False` 的处理方式很关键：我们**不在事后删 anyOf**，而是在
    生成 schema 时就压根不产生它。理由 —— 事后删会留下一个
    `{"description": …}` 这样没有任何类型约束的属性，模型可以往里塞任意东西；
    而"不产生 anyOf"的等价物是明确的：字段从 required 里去掉，
    用「可以省略」代替「可以写 null」。前者比后者危险得多，所以选后者。

    连带后果（必须说清，不能装作没有）：strict 模式要求所有属性都出现在
    required 里，而"可以省略"与它直接冲突 → 遇到不支持 anyOf 的家时，
    strict 会**退化**为非严格（见 resolve_strict），字段结构改由本地强校验兜住。
    """

    # 在这家端点**额外**禁用的关键字（与全局集合取并集）。
    forbid: frozenset[str] = frozenset()
    # 是否接受 anyOf。False 表示这家不支持 union 关键字。
    allow_anyof: bool = True


DEFAULT_SCHEMA_DIALECT = SchemaDialect()


def dialect_for_spec(spec: Any) -> SchemaDialect:
    """从模型/连接条目上读出它的方言。

    用 getattr 加默认值：离线替身、旧配置（没有这两个字段）、
    以及测试里的最小 stub 都必须能直接过 —— 这里不是校验点。
    """
    extra = getattr(spec, "forbidden_schema_keywords", None) or ()
    return SchemaDialect(
        forbid=frozenset(str(k) for k in extra),
        allow_anyof=bool(getattr(spec, "allow_anyof_in_schema", True)),
    )


def resolve_strict(strict: bool, dialect: SchemaDialect) -> tuple[bool, str]:
    """这个方言下 strict 是否可用；不可用时返回退化后的值与**原因**。

    调用方负责把原因写进日志（不是静默降级：用户开了 strict 却发现没生效，
    必须能找到原因，否则会以为配置项坏了）。
    """
    if strict and not dialect.allow_anyof:
        return False, (
            "该端点不支持 anyOf：严格模式要求 schema 里每个属性都出现在 required 中，"
            "而本项目约有 90 个可写字段，那样会逼模型每张图都吐一遍 null。"
            "因此本次退化为非严格模式（字段结构与取值仍由本地强校验把关）。"
        )
    return strict, ""


def strict_keyword_subset(node: Any, dialect: SchemaDialect = DEFAULT_SCHEMA_DIALECT) -> Any:
    """递归剥掉该方言不支持的关键字（不修改入参，返回新对象）。

    只处理 dict / list；其余值原样返回。
    注意 `required` / `properties` / `additionalProperties` / `enum` / `anyOf`
    都必须保留 —— 它们正是严格模式生效的依据。

    ⚠ **剥掉取值范围之后，"不出错值"这件事就只剩本地校验一道防线了**
      （见 xmp/validator.py 的 pydantic 边界 + clamp，以及 smoke_test 里
      那条"越界值必须被拒/夹取"的断言）。改这里之前先确认那边还守着。
    """
    if isinstance(node, dict):
        return {
            key: strict_keyword_subset(value, dialect)
            for key, value in node.items()
            if key not in _UNSUPPORTED_SCHEMA_KEYWORDS and key not in dialect.forbid
        }
    if isinstance(node, list):
        return [strict_keyword_subset(item, dialect) for item in node]
    return node


def build_params_properties(strict: bool, allow_null_union: bool = True) -> dict[str, dict[str, Any]]:
    """构造 params 对象的属性表。

    键名带 crs: 前缀（硬约束 #5 要求"字段名须为 ACR 的 crs: 前缀字段名"）。
    strict 模式下每个属性都加上 null 分支，因为 strict 要求全部字段出现在
    required 中，而"本图不需要调整"必须能表达为 null。

    ⚠ "可空"有两种写法：这里是 `"type": [T, "null"]`，曲线那边是
      `{"anyOf": [T, {"type": "null"}]}`。**两种都属于联合类型**，
      不支持联合的端点（Kimi 的 MFJS）两种都不认，所以要一起被
      allow_null_union 管住 —— 只堵 anyOf 会在 params 上漏过去。
    """
    properties: dict[str, dict[str, Any]] = {}
    for name, spec in F.AI_WRITABLE_FIELDS.items():
        fragment = spec.json_schema_fragment()
        key = f"crs:{name}"
        if strict and allow_null_union:
            # 把类型扩展为"原类型 或 null"。
            original_type = fragment.get("type")
            if original_type is not None:
                fragment["type"] = [original_type, "null"]
            elif "enum" in fragment:
                fragment["enum"] = list(fragment["enum"]) + [None]
        properties[key] = fragment
    return properties


def build_curves_schema(strict: bool, allow_null_union: bool = True) -> dict[str, Any]:
    """构造 curves 对象（四条曲线的控制点）。

    allow_null_union=False 时**不生成 anyOf**（不支持联合的端点），
    而不是生成之后再删 —— 事后删会留下一个没有类型约束的属性（见 SchemaDialect）。
    """
    point_schema: dict[str, Any] = {
        "type": "array",
        "items": {
            "type": "array",
            "items": {"type": "number", "minimum": CURVE_MIN, "maximum": CURVE_MAX},
            "minItems": 2,
            "maxItems": 2,
        },
        "minItems": 2,
        "maxItems": 16,
    }
    properties: dict[str, Any] = {
        f"crs:{name}": dict(
            point_schema,
            description=(
                "点曲线控制点，按 x 递增；(x, y) 均为 0..1 归一化坐标。"
                "x 是输入亮度，y 是输出亮度。至少两个点，最多 16 个点。"
                "ToneCurvePV2012 是 RGB 合成曲线，带 Red/Green/Blue 后缀的为单通道曲线。"
            ),
        )
        for name in F.CURVE_FIELDS
    }
    if strict and allow_null_union:
        for key in list(properties):
            properties[key] = {"anyOf": [properties[key], {"type": "null"}]}
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        "required": sorted(properties) if strict else [],
    }


def build_masks_schema() -> dict[str, Any]:
    """逐图的**局部调整意图**（masks）。

    设计原则（用户 2026-09-23 的裁决）：
      · 风格只决定方向与力度，所以这里**不给**具体数值目标，只允许模型提"意图 + 位置"；
      · 几何一律用**显示帧**归一化坐标（人看到的"天空在上方"），
        写入前由程序按每张照片自己的 EXIF 方向换算成 ACR 的存储帧；
      · 局部参数只开一小扇门（明暗/质感/相对色温色调），不允许局部降噪、去边、颜色分级、
        颗粒与摩尔纹 —— 降噪是技术校正（取决于 ISO/机身/曝光时长），颜色分级则是审美方向；
      · 最多 3 个：数量一多就变成凑数，效果反而变差。
    """
    local_props = {
        name: {
            "type": "number",
            "minimum": limits[0],
            "maximum": limits[1],
            "description": (
                f"局部参数（面板值）。允许范围 {limits[0]:g}..{limits[1]:g}，由程序复核与夹紧"
                + ("；这是**相对偏移**，不是开尔文" if "Temperature" in name or "Tint" in name else "")
                + ("；负值即磨皮/柔化" if name in ("LocalTexture", "LocalClarity2012") else "")
            ),
        }
        for name, limits in AI_LOCAL_RANGES.items()
    }
    return {
        "type": "array",
        "maxItems": PROGRAM_MAX_MASKS,
        "description": (
            "本图需要的局部调整（最多 3 个）。**只有在你确实需要在画面某处单独处理时才用**；"
            "风格档案里「局部调整习惯」显示这位用户本来就不用蒙版时，请返回空数组 []。"
            "每项都必须给出 kind（linear/radial/brush）、target（对哪里，如「天空」）、"
            "intent（干什么，如「压暗并保持透亮」）、显示帧坐标、以及至少一个 local 参数。"
            "坐标写法（务必按 kind 对应的键名写，不要混用）：\n"
            "  · linear → zero/full（各 [x, y]，0..1，zero 是效果 0% 端，"
            "full 是 100% 端）；例："
            "{\"kind\":\"linear\",\"target\":\"天空\",\"intent\":\"压暗\","
            "\"zero\":[0.5,0.75],\"full\":[0.5,0.05],\"local\":{\"LocalHighlights2012\":-30}}\n"
            "  · radial → rect（[上, 左, 下, 右]）+ 可选 feather/angle/midpoint/roundness；例："
            "{\"kind\":\"radial\",\"target\":\"人物面部\",\"intent\":\"提亮\","
            "\"rect\":[0.35,0.40,0.60,0.65],\"feather\":60,"
            "\"local\":{\"LocalExposure2012\":0.35}}\n"
            "  · brush → dabs（[[x, y], …]，最多 60 个点）+ 可选 radius（0.001..0.5）/flow；例："
            "{\"kind\":\"brush\",\"target\":\"人物面部\",\"intent\":\"磨皮\","
            "\"dabs\":[[0.50,0.50],[0.55,0.52],[0.45,0.53]],\"radius\":0.08,\"flow\":0.6,"
            "\"local\":{\"LocalTexture\":-15}}"
        ),
        "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(MASK_KINDS)},
                "target": {"type": "string", "description": "局部作用于哪里（中文短词）"},
                "intent": {"type": "string", "description": "局部要达成什么（中文一句话）"},
                "zero": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                         "minItems": 2, "maxItems": 2},
                "full": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                         "minItems": 2, "maxItems": 2},
                "rect": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                         "minItems": 4, "maxItems": 4,
                         "description": "径向渐变外接框 [上, 左, 下, 右]（显示帧）"},
                "angle": {"type": "number"},
                "midpoint": {"type": "number", "minimum": 0, "maximum": 100},
                "roundness": {"type": "number", "minimum": -100, "maximum": 100},
                "feather": {"type": "number", "minimum": 0, "maximum": 100},
                "dabs": {
                    "type": "array",
                    "maxItems": 60,
                    "items": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                              "minItems": 2, "maxItems": 2},
                },
                "radius": {"type": "number", "minimum": 0.001, "maximum": 0.5},
                "flow": {"type": "number", "minimum": 0.05, "maximum": 1},
                "local": {
                    "type": "object",
                    "properties": local_props,
                    "additionalProperties": False,
                    "description": "局部参数（面板值），至少给一个",
                },
            },
            "required": ["kind", "target", "local"],
            "additionalProperties": False,
        },
    }


def build_item_schema(
    file_ids: list[str] | None, strict: bool, allow_null_union: bool = True
) -> dict[str, Any]:
    """构造单张图的响应结构。"""
    file_id_schema: dict[str, Any] = {"type": "string"}
    if file_ids:
        # 用 enum 把 file_id 限死在本次请求的图片集合内，
        # 避免模型编造不存在的文件名（那是很常见的一类失败）。
        file_id_schema["enum"] = list(file_ids)
    file_id_schema["description"] = "必须原样回填请求中给出的 file_id，用于把参数对应回具体文件"

    properties: dict[str, Any] = {
        "file_id": file_id_schema,
        "params": {
            "type": "object",
            "properties": build_params_properties(strict, allow_null_union),
            "additionalProperties": False,
            "required": sorted(build_params_properties(strict, allow_null_union)) if strict else [],
            "description": (
                "本图需要写入 ACR 的参数。键必须使用 crs: 前缀的 ACR 字段名。"
                "**只返回需要改动的字段**，不要返回保持默认的字段。"
                "若某张图不需要任何调整，params 返回空对象 {}。"
            ),
        },
        "curves": build_curves_schema(strict, allow_null_union),
        "masks": build_masks_schema(),
        "notes": {
            "type": "string",
            "description": "一句话说明你为什么这样调（中文，便于用户复核）",
        },
    }

    required = ["file_id", "params", "notes"] + (["curves", "masks"] if strict else [])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def build_response_schema(
    file_ids: list[str] | None = None,
    strict: bool = False,
    dialect: SchemaDialect = DEFAULT_SCHEMA_DIALECT,
) -> dict[str, Any]:
    """构造完整的响应 JSON Schema。

    顶层结构：
        {"items": [ {file_id, params, curves, notes}, ... ]}
    用 items 数组包一层而不是直接返回对象，是为了兼容"一次请求多张图"
    （config 的 images_per_request），也便于模型在批内做一致性比较。

    strict 会按 dialect 再解析一次（不支持联合类型的端点会退化为非严格，
    原因见 resolve_strict；调用方负责把原因写进日志）。
    """
    strict, _degraded = resolve_strict(strict, dialect)
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": build_item_schema(file_ids, strict, dialect.allow_anyof),
                "minItems": 1,
                "description": "每张图一项，请为请求中列出的每个 file_id 各返回一项",
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def build_training_schema(strict: bool = False) -> dict[str, Any]:
    """训练模式的响应结构。

    训练模式不产出参数，而是产出对用户偏好的归纳。
    param_ranges（均值/中位数/范围）由本地 numpy 计算，不交给模型
    ——统计量必须可复现、可审计，不能依赖模型心算。
    """
    text_rule = {
        "type": "string",
        "description": "一条自然语言描述的风格倾向或禁区（中文），例如「人像偏好暖调，肤色向橙红偏移」",
    }
    return {
        "type": "object",
        "properties": {
            "style_summary": {
                "type": "string",
                "description": "对该用户调色风格的整体概括，2–4 句中文",
            },
            "text_rules": {
                "type": "array",
                "items": text_rule,
                "minItems": 1,
                "maxItems": 20,
                "description": "可执行的风格规则列表，用于后续输出模式的提示词",
            },
            "saturation_tendency": {
                "type": "string",
                "enum": ["偏保守", "中性", "偏激进"],
                "description": "饱和度倾向",
            },
            "contrast_tendency": {
                "type": "string",
                "enum": ["低对比/柔和", "中性", "高对比/硬朗"],
                "description": "对比度倾向",
            },
            "shadow_color_cast": {
                "type": "string",
                "enum": ["偏冷（蓝青）", "中性", "偏暖（黄橙）", "偏绿", "偏洋红"],
                "description": "暗部色偏倾向",
            },
            "hsl_habits": {
                "type": "string",
                "description": "HSL 分区调整习惯，例如「倾向压绿、提橙红亮度以改善肤色」",
            },
            "lens_correction_preference": {
                "type": "string",
                "description": "镜头校正取舍，例如「通常启用配置文件校正并保留轻微暗角」",
            },
            "excluded_reasoning": {
                "type": "string",
                "description": "你判断哪些字段属于相机/镜头默认值而排除在偏好统计之外的简要理由",
            },
            "scene_labels": {
                "type": "object",
                "description": (
                    "给每一张训练样本打一个**场景标签**（键是 file_id，值是 2–8 字的短标签）。"
                    "这一栏是程序统计「场景 ↔ 参数」关系的唯一依据：标签词要能在同类照片上复用。"
                    "优先用这些候选：人像（正面/半身）、人像（环境）、风景（日出日落）、"
                    "风景（山野）、风景（水景/海景）、城市建筑、夜景弱光、街拍人文、"
                    "花卉微距、静物、动物、运动、室内、逆光剪影、雪景、雾天/阴天、航拍/俯拍；"
                    "没有合适的就自拟一个短名，但同一批里的同类照片必须用同一个词。"
                ),
                "additionalProperties": {"type": "string"},
            },
        },
        "required": [
            "style_summary",
            "text_rules",
            "saturation_tendency",
            "contrast_tendency",
            "shadow_color_cast",
            "hsl_habits",
            "lens_correction_preference",
            "excluded_reasoning",
        ],
        "additionalProperties": False,
    }
