# -*- coding: utf-8 -*-
"""XMP → JSON 解析（问题 d 的实现）。

解析策略（逐条对应问题 d 的要求）
--------------------------------
1. **命名空间**：用 xml.etree + Clark 记法（{uri}local），前缀表集中在
   xmp/namespaces.py，并在解析前 register_namespace，保证写回时前缀不变。

2. **rdf:Description 多段合并**：真实 ACR 15.x 只写一段 Description，
   但 Lightroom 与部分第三方工具会把属性拆到多段。
   合并规则：按文档顺序遍历，属性取**并集**；同名字段冲突时保留
   **首个非空值**，并记 DEBUG 日志。选择"首个非空"而不是"最后一个"，
   是因为文档靠前的 Description 通常是主描述块，更可能是权威值。

3. **单位与比例换算**：
   - 属性字符串带前导 "+"（"+16"、"+1.0"）→ float() 原生可解析。
   - 布尔以 "True"/"False" 字符串出现 → 内部归一化为 bool，
     写回时必须还原为大写字面量。
   - 曲线坐标是 **0..255** 整数域（不是 0..1）。
     交给 AI 前统一转 0..1 浮点并在提示词中说明，收回时反向映射。
   - 绝对量 vs 相对量分区：
       Temperature（开尔文，绝对）、SplitToning*Hue（0..360，绝对角）
       vs HueAdjustment*（-100..100，相对偏移）
     二者不可合并求均值。

4. **白平衡陷阱**：实测 6/8 样本为 WhiteBalance="As Shot"，此时
   Temperature/Tint 被 ACR 忽略（只是记录性的机内值）。
   解析时保留原值，但在 typed_params 中标记 wb_effective 供上层判断。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import AcbError
from ..logging_setup import get_logger
from . import fields as F
from . import namespaces as NS

log = get_logger("xmp.reader")


class XmpParseError(AcbError):
    """XMP 内容无法解析（不是合法 XML，或缺少 xmpmeta 根）。"""


# 蒙版/局部调整中需要统计但不解析几何的字段前缀。
# 这些是"局部"参数，与全局同名参数（如 Exposure2012）语义不同，必须分开。
LOCAL_FIELD_PREFIX = "Local"
MASK_GEOMETRY_TAGS: tuple[str, ...] = (
    "CorrectionMasks",
    "Gesture",
    "Points",
    "MaskGroupBasedCorrections",
    "RetouchAreas",
    "RetouchArea",
)


@dataclass
class MaskStats:
    """蒙版使用情况统计（不解析几何，只看使用模式）。

    用户裁决：忽略蒙版几何（坐标/digest/多边形动辄上千行且跨图不可比），
    但把蒙版使用频率与局部调整倾向写进 style_profile 的 text_rules。
    """

    total_corrections: int = 0
    total_masks: int = 0
    total_retouch_areas: int = 0
    names: list[str] = field(default_factory=list)
    manual_count: int = 0      # 手动画笔/渐变（ACR 默认名 "蒙版 N"）
    subject_count: int = 0     # AI 主体蒙版（"人物 N - 面部皮肤"等）
    object_count: int = 0      # AI 对象选择（"对象 N"）
    # 局部参数聚合：{字段名: [值, ...]}，供 text_rules 归纳使用
    local_values: dict[str, list[float]] = field(default_factory=dict)

    @property
    def used(self) -> bool:
        return self.total_corrections > 0 or self.total_retouch_areas > 0

    def classify_name(self, name: str) -> None:
        """按名称模式分类。中英文 ACR 都要匹配（用户可能用英文界面）。"""
        from ..constants import (
            MASK_NAME_MANUAL_PREFIXES,
            MASK_NAME_OBJECT_PREFIXES,
            MASK_NAME_SUBJECT_PREFIXES,
        )

        text = (name or "").strip()
        if not text:
            return
        if any(text.startswith(p) for p in MASK_NAME_SUBJECT_PREFIXES):
            self.subject_count += 1
        elif any(text.startswith(p) for p in MASK_NAME_OBJECT_PREFIXES):
            self.object_count += 1
        elif any(text.startswith(p) for p in MASK_NAME_MANUAL_PREFIXES):
            self.manual_count += 1

    def local_mean(self, field_name: str) -> float | None:
        """某局部字段的均值；无数据返回 None。"""
        values = self.local_values.get(field_name)
        if not values:
            return None
        return sum(values) / len(values)


class XmpDocument:
    """一份已解析的 XMP 文档。

    保留"包装层"（xpacket 处理指令、xmpmeta 前后的一切文本）为不透明字符串，
    只解析并暴露中间的 xmpmeta 子树，从而在写回时做到"非目标内容零改动"。
    """

    def __init__(self, raw: bytes, *, source: str = "") -> None:
        self.source = source
        self.raw = raw
        self.prefix_text = ""
        self.suffix_text = ""
        self.root: ET.Element | None = None
        self.descriptions: list[ET.Element] = []
        self._parse()

    # --- 解析 ---------------------------------------------------------------

    def _parse(self) -> None:
        text = self.raw.decode("utf-8", errors="replace")

        # 定位 x:xmpmeta 子树。用 rfind 而不是正则，是为了正确处理
        # 属性里可能出现的 ">"（正则很容易在这里出错）。
        start = text.find("<x:xmpmeta")
        if start < 0:
            # 兼容：某些工具只写 <rdf:RDF> 没有 x:xmpmeta 外层。
            start = text.find("<rdf:RDF")
            if start < 0:
                raise XmpParseError(
                    f"{self.source or 'XMP'} 中未找到 <x:xmpmeta> 或 <rdf:RDF> 根元素，"
                    "该文件可能不是 XMP 侧车文件。"
                )
            end_tag = "</rdf:RDF>"
            end = text.rfind(end_tag)
            if end < 0:
                raise XmpParseError(f"{self.source or 'XMP'} 的 <rdf:RDF> 没有闭合标签。")
            end = end + len(end_tag)

            # 关键修复：只取 <rdf:RDF> 片段时会丢掉**父元素上**的命名空间声明，
            # 于是片段里用到的前缀全部变成"未绑定"，ET.fromstring 会抛
            # "unbound prefix"。
            # 这里的做法是：在 <rdf:RDF> 的起始标签里就地补回全部已知命名空间声明。
            # 这是我们唯一能安全假设的前缀集合（PREFIX_TO_URI），
            # 因为 XMP 的命名空间是规范固定的。
            gt = text.find(">", start)
            if gt < 0:
                raise XmpParseError(f"{self.source or 'XMP'} 的 <rdf:RDF> 起始标签未闭合。")
            declarations = " ".join(
                f'xmlns:{prefix}="{uri}"'
                for prefix, uri in NS.PREFIX_TO_URI.items()
                if prefix != "xml"
            )
            body = text[start:gt] + " " + declarations + text[gt:end]
            self.prefix_text = text[:start]
            self.suffix_text = text[end:]
        else:
            end_tag = "</x:xmpmeta>"
            end = text.rfind(end_tag)
            if end < 0:
                # 自闭合形式 <x:xmpmeta ... /> 极少见但存在。
                self_close = text.find("/>", start)
                if self_close < 0:
                    raise XmpParseError(f"{self.source or 'XMP'} 的 <x:xmpmeta> 没有闭合标签。")
                end = self_close + 2
            else:
                end = end + len(end_tag)
            body = text[start:end]
            self.prefix_text = text[:start]
            self.suffix_text = text[end:]

        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise XmpParseError(f"{self.source or 'XMP'} XML 解析失败：{exc}") from exc

        # 允许 body 直接是 rdf:RDF（没有 xmpmeta 外层）
        if root.tag == NS.RDF_TAG:
            self.root = root
            self.descriptions = list(root.findall(NS.DESCRIPTION_TAG))
        else:
            self.root = root
            rdf = root.find(NS.RDF_TAG)
            if rdf is None:
                raise XmpParseError(f"{self.source or 'XMP'} 中缺少 <rdf:RDF>。")
            self.descriptions = list(rdf.findall(NS.DESCRIPTION_TAG))

        if not self.descriptions:
            log.debug("%s 中没有 rdf:Description 段（空 XMP）。", self.source or "XMP")

    # --- 属性读取 -----------------------------------------------------------

    @property
    def rdf_about(self) -> str | None:
        """rdf:about 的值。

        实测结论：真实 ACR 产物的 rdf:about 是**空字符串**（8/8 样本验证），
        不是"原始文件名"。用户已裁决按空字符串写。
        """
        if not self.descriptions:
            return None
        return self.descriptions[0].get(NS.ABOUT_ATTR)

    def _namespace_values(self, ns_uri: str) -> dict[str, str]:
        """收集某命名空间下所有属性（跨多段 Description，取并集）。"""
        result: dict[str, str] = {}
        for desc in self.descriptions:
            for key, value in desc.attrib.items():
                if not key.startswith("{"):
                    continue
                uri, _, local = key[1:].partition("}")
                if uri != ns_uri:
                    continue
                if local in result and result[local] != "":
                    # 冲突时保留首个非空值，并留痕便于排查异常文件。
                    if value and value != result[local]:
                        log.debug(
                            "%s 中字段 %s 出现重复定义（%r vs %r），保留首个非空值。",
                            self.source or "XMP",
                            local,
                            result[local],
                            value,
                        )
                else:
                    result[local] = value
        return result

    def crs_raw(self) -> dict[str, str]:
        """crs: 命名空间的全部属性（原始字符串）。"""
        return self._namespace_values(NS.CRS_NS)

    def crd_raw(self) -> dict[str, str]:
        """crd: 命名空间（camera-raw-defaults，相机出厂默认基线）。

        硬约束 #15 的判别依据：crs:X == crd:X 表示用户没有改动 X。
        """
        return self._namespace_values(NS.CRD_NS)

    def typed_params(self) -> dict[str, Any]:
        """把 crs: 属性解析为带类型的参数字典。

        只返回本表已登记的字段；未登记字段由 unknown_crs_fields() 单独报告，
        以便 tools/dump_crs_fields.py 做"漏字段门禁"。
        """
        raw = self.crs_raw()
        params: dict[str, Any] = {}
        for name, text in raw.items():
            spec = F.get_field(name)
            if spec is None or spec.kind == F.KIND_CURVE:
                continue
            params[name] = F.parse_value(spec, text)
        return params

    def unknown_crs_fields(self) -> list[str]:
        """返回出现在文件中但未在 fields.py 登记的 crs 字段名。"""
        return sorted(name for name in self.crs_raw() if not F.is_known(name))

    def wb_is_effective(self) -> bool:
        """白平衡数值（Temperature/Tint）当前是否真正生效。

        实测：6/8 样本为 WhiteBalance="As Shot"，此时 ACR 忽略这两个数值。
        训练模式据此决定是否把色温计入偏好统计（否则会严重污染统计结果）。
        """
        raw = self.crs_raw()
        return raw.get("WhiteBalance", "As Shot") == "Custom"

    # --- 曲线读取 -----------------------------------------------------------

    def curves(self) -> dict[str, list[tuple[int, int]]]:
        """读取四个曲线的控制点。

        结构（元素型，不是属性）：
            <crs:ToneCurvePV2012>
              <rdf:Seq>
                <rdf:li>0, 0</rdf:li>
                <rdf:li>255, 255</rdf:li>
              </rdf:Seq>
            </crs:ToneCurvePV2012>
        坐标域为 0..255 整数。

        空 Seq（只有端点 0,0 / 255,255）表示线性，与"未设置"等价。
        """
        result: dict[str, list[tuple[int, int]]] = {}
        for desc in self.descriptions:
            for curve_name in F.CURVE_FIELDS:
                element = desc.find(NS.qname("crs", curve_name))
                if element is None:
                    continue
                points: list[tuple[int, int]] = []
                seq = element.find(NS.SEQ_TAG)
                if seq is not None:
                    for li in seq.findall(NS.LI_TAG):
                        text = (li.text or "").strip()
                        if not text:
                            continue
                        parts = [p.strip() for p in text.split(",")]
                        if len(parts) != 2:
                            log.debug("曲线 %s 的点 %r 不是二元组，已跳过。", curve_name, text)
                            continue
                        try:
                            points.append((int(round(float(parts[0]))), int(round(float(parts[1])))))
                        except ValueError:
                            log.debug("曲线 %s 的点 %r 含非数值，已跳过。", curve_name, text)
                            continue
                if points:
                    result[curve_name] = points
        return result

    def curve_is_linear(self) -> bool:
        """是否所有曲线都是线性（无实际点曲线编辑）。

        判据：只有两个端点且端点就是 (0,0) 与 (255,255)。
        """
        curves = self.curves()
        if not curves:
            return True
        for points in curves.values():
            if len(points) > 2:
                return False
            if points != [(0, 0), (255, 255)] and points != [(0, 0), (255, 255)][: len(points)]:
                return False
        return True

    # --- 蒙版统计（不解析几何） --------------------------------------------

    def mask_stats(self) -> MaskStats:
        """统计蒙版使用情况。

        明确不做的事（用户裁决）：不解析 CorrectionMasks/Gesture/Points 中的
        多边形坐标、InputDigest、MaskDigest、WholeImageArea、ReferencePoint。
        这些动辄上千行、跨图片不可比，且对"审美偏好"没有信息量。
        明确要做的事：统计蒙版数量与名称模式，聚合局部调整参数。
        """
        stats = MaskStats()

        for desc in self.descriptions:
            # --- 蒙版组（局部调整）---
            mg = desc.find(NS.qname("crs", "MaskGroupBasedCorrections"))
            if mg is not None:
                seq = mg.find(NS.SEQ_TAG)
                if seq is not None:
                    for li in seq.findall(NS.LI_TAG):
                        for sub in li.findall(NS.DESCRIPTION_TAG):
                            what = sub.get(NS.qname("crs", "What"))
                            if what != "Correction":
                                continue
                            stats.total_corrections += 1
                            name = sub.get(NS.qname("crs", "CorrectionName")) or ""
                            if name:
                                stats.names.append(name)
                                stats.classify_name(name)
                            # 统计该蒙版下挂了几个实际蒙版（Mask/Image、Mask/Polygon 等）
                            masks = sub.find(NS.qname("crs", "CorrectionMasks"))
                            if masks is not None:
                                mask_seq = masks.find(NS.SEQ_TAG)
                                if mask_seq is not None:
                                    stats.total_masks += len(mask_seq.findall(NS.LI_TAG))
                            # 聚合局部调整参数
                            for key, value in sub.attrib.items():
                                if not key.startswith("{"):
                                    continue
                                uri, _, local = key[1:].partition("}")
                                if uri != NS.CRS_NS or not local.startswith(LOCAL_FIELD_PREFIX):
                                    continue
                                try:
                                    stats.local_values.setdefault(local, []).append(float(value))
                                except ValueError:
                                    continue

            # --- 污点修复 ---
            retouch = desc.find(NS.qname("crs", "RetouchAreas"))
            if retouch is not None:
                seq = retouch.find(NS.SEQ_TAG)
                if seq is not None:
                    stats.total_retouch_areas += len(seq.findall(NS.LI_TAG))

        return stats

    # --- 训练快照 -----------------------------------------------------------

    def training_snapshot(self) -> dict[str, Any]:
        """产出训练模式需要的完整快照（供 pipeline/style_profile.py 使用）。"""
        return {
            "crs_raw": self.crs_raw(),
            "crd_raw": self.crd_raw(),
            "params": self.typed_params(),
            "curves": self.curves(),
            "curve_linear": self.curve_is_linear(),
            "wb_effective": self.wb_is_effective(),
            "masks": self.mask_stats(),
            "unknown_fields": self.unknown_crs_fields(),
            "rdf_about": self.rdf_about,
        }


def read_xmp_bytes(raw: bytes, source: str = "") -> XmpDocument:
    """从字节解析 XMP。"""
    return XmpDocument(raw, source=source)


def read_xmp_file(path: Path) -> XmpDocument:
    """从磁盘读取并解析 XMP 文件。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise XmpParseError(f"读取 XMP 文件失败 {path}：{exc}") from exc
    return XmpDocument(raw, source=str(path))
