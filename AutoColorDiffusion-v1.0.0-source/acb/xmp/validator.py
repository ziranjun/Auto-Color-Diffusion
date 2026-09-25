# -*- coding: utf-8 -*-
"""AI 返回值的强校验（硬约束 #5 + 技术栈要求 pydantic）。

两级校验设计
------------
为什么不是简单地用 pydantic 的 ge/le 卡死范围：
    模型的数值输出**偶尔会轻微越界**（把曝光写成 5.02、把对比度写成 101）。
    这类误差属于浮点/舍入性质，重试一次大概率还是同样结果，白白烧钱。
    但**越界幅度过大**（曝光 500、对比度 -900）说明模型理解错了字段，
    必须判为非法并触发重试（硬约束 #5：解析失败自动重试 1 次）。

因此：
    第 1 级（pydantic 模型）：用**放宽后的边界**（确切范围 ± 容差）校验。
        超出放宽边界 → 判为非法，交给上层重试。
    第 2 级（clamp）：把落在容差内的轻微越界值夹到确切范围。
    这样既不会为琐碎误差重试，也不会让严重错误悄悄通过。

字段名兼容
----------
硬约束 #5 要求"字段名须为 ACR 的 crs: 前缀字段名"，因此提示词与 JSON Schema
里用的键是 `crs:Exposure2012`。但 `crs:Exposure2012` 不是合法的 Python 标识符，
无法直接做 pydantic 字段名。这里的处理是：
    入口处剥掉 "crs:" 前缀并归一化，内部一律用裸字段名（Exposure2012）。
    同时接受裸字段名输入（宽容容错，避免模型只写半个前缀时整批失败）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from ..logging_setup import get_logger
from . import fields as F

log = get_logger("xmp.validator")

# crs 前缀（AI 返回的键使用这个前缀，硬约束 #5）
CRS_PREFIX = "crs:"

# 容差定义
#   整数型：允许越界 2 个单位。
#       依据：ACR 面板滑块通常一步 1–5，越界 2 以内肉眼无法分辨，
#       属于模型四舍五入的合理误差。
#   浮点型：允许越界 max(0.05, 跨度的 2%)。
#       依据：曝光跨度 10 EV，2% = 0.2 EV，这已是可感知的幅度；
#       再大就该重试而不是夹取。下限 0.05 用于覆盖跨度极小的字段
#       （如 SharpenRadius 的跨度仅 2.5，2% = 0.05）。
INT_TOLERANCE = 2
REAL_TOLERANCE_RATIO = 0.02
REAL_TOLERANCE_MIN = 0.05


def _int_tolerance(spec: F.FieldSpec) -> int:
    return INT_TOLERANCE


def _real_tolerance(spec: F.FieldSpec) -> float:
    if spec.minimum is None or spec.maximum is None:
        return REAL_TOLERANCE_MIN
    span = float(spec.maximum) - float(spec.minimum)
    return max(REAL_TOLERANCE_MIN, span * REAL_TOLERANCE_RATIO)


def build_params_model(ai_only: bool = True) -> type[BaseModel]:
    """按字段表动态构造 pydantic 模型。

    使用 extra="forbid"：未登记的字段直接报错，
    这是"绝不发明字段名"的强制手段（ACR 对未知字段是静默忽略的，
    若放任通过，用户会面对"任务成功但参数没生效"这种最难排查的问题）。
    """
    source = F.AI_WRITABLE_FIELDS if ai_only else F.FIELDS
    definitions: dict[str, Any] = {}

    for name, spec in source.items():
        if spec.kind == F.KIND_ENUM:
            annotation = Optional[Literal[spec.enum_values]]  # type: ignore[valid-type]
        elif spec.kind == F.KIND_BOOL:
            annotation = Optional[bool]
        elif spec.kind == F.KIND_INT:
            assert spec.minimum is not None and spec.maximum is not None
            tol = _int_tolerance(spec)
            annotation = Optional[
                Annotated[int, Field(ge=int(spec.minimum) - tol, le=int(spec.maximum) + tol)]
            ]
        elif spec.kind == F.KIND_REAL:
            assert spec.minimum is not None and spec.maximum is not None
            tol = _real_tolerance(spec)
            annotation = Optional[
                Annotated[float, Field(ge=float(spec.minimum) - tol, le=float(spec.maximum) + tol)]
            ]
        else:
            # 曲线型字段不走属性通道（见 validate_curves）。
            continue
        definitions[name] = (annotation, None)

    return create_model(  # type: ignore[call-overload]
        "AcrParams",
        __config__=ConfigDict(extra="forbid"),
        **definitions,
    )


# 模型构造有一定开销（约 100 个字段），用模块级缓存避免每个请求重建。
_PARAMS_MODEL: type[BaseModel] | None = None


def params_model() -> type[BaseModel]:
    global _PARAMS_MODEL
    if _PARAMS_MODEL is None:
        _PARAMS_MODEL = build_params_model(ai_only=True)
    return _PARAMS_MODEL


def normalize_field_name(name: str) -> str:
    """把 `crs:Exposure2012` 归一化为 `Exposure2012`。

    同时接受裸名与带前缀名，避免模型只写半个前缀时整批失败。
    """
    text = (name or "").strip()
    if text.startswith(CRS_PREFIX):
        return text[len(CRS_PREFIX):]
    if text.startswith("{"):
        # 兼容极端情况：模型把 Clark 记法照抄回来了。
        _, _, local = text[1:].partition("}")
        return local
    return text


@dataclass
class ValidationResult:
    """一次校验的结果。"""

    params: dict[str, Any] = field(default_factory=dict)
    curves: dict[str, list] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否可以继续（有 errors 则应触发重试）。"""
        return not self.errors

    def summary(self) -> str:
        if self.errors:
            return "校验失败：" + "；".join(self.errors[:5])
        if self.warnings:
            return "校验通过（含警告）：" + "；".join(self.warnings[:5])
        return f"校验通过（{len(self.params)} 个参数）"


def validate_params(raw: dict | None) -> ValidationResult:
    """校验 AI 返回的单张图参数字典。

    输入形如：
        {"crs:Exposure2012": 0.35, "crs:Saturation": -5, "crs:WhiteBalance": "Custom"}
    输出为归一化 + 夹取后的裸字段名参数字典。
    """
    result = ValidationResult()
    # ⚠ 这里必须把三种情况分清楚（把"可选"和"没给"混在一起是真实缺陷）：
    #   1. 键缺失 / null —— 模型没回答，是**错误**（响应被截断、模型跑偏都长这样）；
    #   2. 空对象 {} —— 提示词与 JSON Schema 都明确写了
    #      「若某张图不需要任何调整，params 返回空对象 {}」→ **合法**，
    #      意思是"这张不用调"，不是"没回答"。
    #   3. 其它类型 —— 错误。
    # 曾经的写法是 `if not raw:` 一锅端，于是"本来就调得很好的图"会被判失败、
    # 触发重试，甚至整批失败 —— 而它其实是模型的正确答案。
    if raw is None:
        result.errors.append("响应里没有 params（键缺失或为 null）")
        return result

    if not isinstance(raw, dict):
        result.errors.append(f"参数不是对象（实际类型 {type(raw).__name__}）")
        return result

    if not raw:
        result.warnings.append(
            "模型未提出任何调整（params 为空对象），按「无需调整」处理"
        )
        return result

    # --- 1. 归一化字段名，分离出曲线字段与非 crs 字段 ------------------------
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        bare = normalize_field_name(str(key))

        if bare in F.CURVE_FIELDS:
            result.curves[bare] = value
            continue

        # 用 get_field 而不是直接查 FIELDS：它会按前缀识别动态机器字段
        # （Table_xxx、UprightTransform_N），从而给出一条"这是机器字段"的
        # 准确报错，而不是笼统地说"未登记"。
        spec = F.get_field(bare)
        if spec is None:
            # 未登记字段：明确报错而不是静默丢弃。
            result.errors.append(
                f"字段 {key!r} 未在 crs 字段白名单中登记（ACR 会静默忽略该字段，故拒收）"
            )
            continue

        if not spec.ai_writable:
            result.errors.append(
                f"字段 {key!r} 属于机器/版本相关字段，AI 不允许产出（会影响跨机器一致性）"
            )
            continue

        normalized[bare] = value

    # --- 2. pydantic 强校验（放宽边界） -------------------------------------
    model = params_model()
    try:
        validated = model.model_validate(normalized)
    except ValidationError as exc:
        for err in exc.errors():
            location = ".".join(str(p) for p in err.get("loc", ()))
            message = err.get("msg", "")
            spec = F.get_field(location)
            if spec is not None:
                result.errors.append(
                    f"字段 {location} 取值 {err.get('input')!r} 越界（{message}；"
                    f"合法范围 {spec.range_text()}）"
                )
            else:
                result.errors.append(f"字段 {location} 校验失败：{message}")
        return result

    dumped = validated.model_dump(exclude_none=True)

    # --- 3. 夹取轻微越界值 --------------------------------------------------
    for name, value in dumped.items():
        spec = F.get_field(name)
        if spec is None:
            continue
        clamped = F.clamp(spec, value)
        if clamped != value:
            result.warnings.append(
                f"字段 {name} 的值 {value!r} 轻微越界，已夹到 {clamped!r}（范围 {spec.range_text()}）"
            )
        result.params[name] = clamped

    # --- 4. 曲线点校验 ------------------------------------------------------
    if result.curves:
        from .writer import normalize_curve_points

        cleaned_curves: dict[str, list] = {}
        for name, raw_points in result.curves.items():
            if not isinstance(raw_points, (list, tuple)):
                result.warnings.append(f"曲线 {name} 不是数组，已忽略")
                continue
            points = normalize_curve_points(list(raw_points))
            if len(points) < 2:
                result.warnings.append(f"曲线 {name} 的有效控制点少于 2 个，已忽略")
                continue
            if len(points) > 16:
                # ACR 的点曲线上限是 16 个控制点；超出的按等距抽样保留 16 个。
                step = len(points) / 16.0
                points = [points[int(i * step)] for i in range(16)]
                result.warnings.append(f"曲线 {name} 控制点超过 16 个，已等距抽样到 16 个")
            cleaned_curves[name] = points
        result.curves = cleaned_curves

    # --- 5. 语义级检查（不阻断，只告警） -----------------------------------
    # 白平衡联动：模型可能只给了 Temperature 而没给 WhiteBalance。
    # writer 会自动补 Custom，这里只提示一下让日志可读。
    if ("Temperature" in result.params or "Tint" in result.params) and \
            "WhiteBalance" not in result.params:
        result.warnings.append(
            "给出了 Temperature/Tint，将自动把 WhiteBalance 置为 Custom 使其生效"
        )

    if not result.params and not result.curves:
        result.errors.append("参数字典中没有任何可用字段")

    return result
