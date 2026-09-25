# -*- coding: utf-8 -*-
"""JSON → XMP 写出（读-改-写）。

用户裁决 #2：**读-改-写**，只覆盖 AI 返回的全局 crs 字段，
完整保留蒙版 / 曲线 / 局部调整 / 非 crs 元数据。

为什么必须读-改-写（而不是从模板生成一份新 XMP）
------------------------------------------------
实测样本 XMP 的最大特征是"极重"：
    IMG_7648.xmp 4152+ 行、11 个蒙版、含污点修复区域；
    IMG_4820.xmp 2073+ 行、含 AI 对象选择蒙版。
这些内容承载了用户数小时的手工修图成果。若用最小模板覆盖写出，
用户的蒙版、画笔、污点修复、裁剪、历史记录会全部消失——
而用户点的是"批量调色"，绝不会预期自己的修图被清空。
因此本模块的铁律是：**只动 crs 命名空间下、且在 fields.py 登记、且 AI 被允许写的字段**，
其余一个字节都不碰。

三个必须强制联动的行为（漏掉任一个都会导致"参数写了但不生效"）
----------------------------------------------------------------
1. **白平衡联动**：ACR 在 WhiteBalance="As Shot" 时**忽略** Temperature/Tint。
   实测 6/8 样本处于该状态，因此写入 Temperature/Tint 时必须同时把
   WhiteBalance 置为 "Custom"。这是本模块最重要的副作用。
2. **曲线联动**：写入了曲线控制点后，必须把 ToneCurveName2012 置为 "Custom"，
   否则 ACR 会继续按预设名（如 Linear）渲染而完全忽略我们写入的点。
3. **曲线坐标域**：写回时必须从 AI 用的 0..1 浮点域映射回 ACR 的 0..255 整数域。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..constants import is_dng
from ..logging_setup import get_logger
from ..paths import data_root
from . import fields as F
from . import namespaces as NS
from .reader import XmpDocument

log = get_logger("xmp.writer")

# 曲线上传给 AI 时用的坐标域是 0..1（便于模型理解），
# ACR 存储用的是 0..255 整数（8 位通道域）。这两个常量把换算关系钉死在一处。
CURVE_DOMAIN_AI = 1.0
CURVE_DOMAIN_ACR = 255

# 新建空白侧车文件时的最小骨架模板。
#
# 【2026-09-24 修一个真缺陷：不写 ProcessVersion 会被 ACR 当成「1 版（2003）」】
# 旧版本刻意**不写** crs:ProcessVersion，理由是"留空则 ACR 用自己的当前默认处理版本"。
# 实测这个假设不成立：用户把自己处理的侧车/DNG 拖进 ACR，校准面板的「处理版本」显示「1 版」
# —— 也就是 Version 1 (2003)。而 2003 引擎不认 2012 那套滑块：高光/阴影/白色/黑色/纹理/
# 去薄雾/颜色分级写进去也**不被渲染**，蒙版（MaskGroupBasedCorrections 需 PV4+）更不会生效。
# 后果是成片与写进去的参数对不上（他描述为"像 HDR 一样的高饱和、和训练集一点也不符"）。
#
# 现在的规矩：
#   · 原文件（侧车/DNG 嵌入包）已有 ProcessVersion → **原样保留**（用户 ACR 的版本是事实）；
#   · 没有 → 写入 DEFAULT_PROCESS_VERSION（= 15.4，即 ACR 界面上的「6 版」）。
#
# crs:Version 写 15.0 与实测样本一致（代表"设置结构版本"而非处理版本）。
# 若你的 ACR 早于 15.4（不认版本 6），把 DEFAULT_PROCESS_VERSION 改成 "11.0"（5 版）即可。
#
# `{profile_line}` 是 crs:CameraProfile 的位置（可为空）——
# 【用户 2026-09 的真实缺陷】新建侧车里没有相机配置，而侧车一旦存在、
# ACR 就不再回落到"相机默认配置"→ 用户看到的色彩配置变成了 Adobe 标准。
# 取值由 `acb.raw.camera_profile` 从 RAW 的机内设置推导（不猜，见那个模块）。
DEFAULT_PROCESS_VERSION = "15.4"

_SKELETON_TEMPLATE = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Auto Color Diffusion">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about=""\n'
    f'    xmlns:crs="{NS.CRS_NS}"\n'
    '   crs:Version="15.0"\n'
    '   crs:ProcessVersion="{process_version}"\n'
    '{profile_line}'
    '   crs:HasSettings="True"\n'
    '   crs:AlreadyApplied="False"/>\n'
    ' </rdf:RDF>\n'
    '</x:xmpmeta>\n'
)


def _skeleton_xml(camera_profile: str | None = None) -> str:
    """生成新建侧车的骨架文本；camera_profile 非空时写入 crs:CameraProfile。"""
    if camera_profile:
        # 值来自我们自己的映射表（没有用户输入），但仍过一道转义，避免以后被改成外部来源时出事。
        escaped = (
            camera_profile.replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;")
        )
        line = f'   crs:CameraProfile="{escaped}"\n'
    else:
        line = ""
    return _SKELETON_TEMPLATE.format(profile_line=line, process_version=DEFAULT_PROCESS_VERSION)


def ensure_process_version(doc: XmpDocument) -> str | None:
    """给“没有处理版本”的设置包补上 ProcessVersion；返回补进去的值（未补时 None）。

    为什么连**已有**文件也要看一眼：DNG 的嵌入包里通常只有相机厂商写的一小段
    （实测 DJI 只写 crs:Version="7.0" 与几个基础字段，**没有** ProcessVersion），
    我们读-改-写之后仍然没有它 —— ACR 于是按「1 版（2003）」解释整包设置，
    我们写的高光/阴影/纹理/蒙版全部白写。
    与 CameraProfile 的规矩一致：**已有值绝不覆盖**（那是用户的 ACR 版本事实）。
    """
    owner = _primary_description(doc)
    if owner.get(NS.qname("crs", "ProcessVersion")):
        return None
    owner.set(NS.qname("crs", "ProcessVersion"), DEFAULT_PROCESS_VERSION)
    return DEFAULT_PROCESS_VERSION


# 我们在新建骨架里写下的工具指纹（`x:xmptk`）。
# 用途：判断一个已有侧车是不是**本程序建的** ——
# 旧版本建的侧车里没有 crs:CameraProfile（用户 2026-09 的真实事故），
# 那些文件需要被补上；而 ACR 自己写的侧车缺该字段是正常的
# （它靠 crd: 里的相机默认基线），不该去动。
TOOLKIT_TAG = "Auto Color Diffusion"


@dataclass
class XmpWriteResult:
    """一次 XMP 写出的结果。"""

    mode: str                     # "sidecar" 或 "embedded"
    target: Path                  # 侧车文件路径，或 DNG 本身
    warnings: list[str] = field(default_factory=list)
    applied_fields: list[str] = field(default_factory=list)
    backup: Path | None = None
    skipped_fields: list[str] = field(default_factory=list)
    # 新建文件时实际写进去的相机配置（None = 没能确定）。
    # 做成结构化字段而不是只写日志：批次结束后要**汇总一句**提醒，
    # 逐文件各刷一条会把日志淹没（用户看日志就是为了找这类总账）。
    camera_profile: str | None = None
    camera_profile_note: str = ""
    # 本次是"新建文件"还是"在已有文件上改"。
    # 调用方靠它汇总提示：只有新建的那些文件才可能出现"相机配置回落成 Adobe 标准"。
    created_new: bool = False


class XmpWriteRefusedError(Exception):
    """写出被拒绝（例如 DNG 在 exiftool 不可用时无法安全改写）。"""


# ============================================================================
# 路径规则（硬约束 #1）
# ============================================================================

def sidecar_path_for(raw_path: Path) -> Path:
    """非 DNG 的侧车 XMP 路径。

    规则：XMP 文件名必须与 RAW 主文件名完全一致（含空格、中文、大小写），
    仅扩展名改为 .xmp，且与 RAW 同目录。
        例："海边 01.cr3" → "海边 01.xmp"
    Path.with_suffix 恰好满足这个语义（只换最后一段后缀，不动其余字符）。
    """
    return raw_path.with_suffix(".xmp")


def embedded_sidecar_path_for(raw_path: Path) -> Path:
    """DNG 的"强制侧车"路径（仅当用户坚持要旁侧文件时使用）。

    形如 foo.dng.xmp —— 用追加而不是替换扩展名，
    因为 foo.xmp 会与"foo.dng 的存在"在语义上产生歧义，
    而 foo.dng.xmp 能明确表达"DNG 的旁侧文件"。
    """
    return raw_path.with_name(raw_path.name + ".xmp")


def is_embedded_mode(raw_path: Path) -> bool:
    """该文件是否必须走"写回文件内部"模式（硬约束 #1 的 DNG 例外）。"""
    return is_dng(raw_path.name)


# ============================================================================
# 变换：应用参数到 XMP 文档
# ============================================================================

def _primary_description(doc: XmpDocument) -> ET.Element:
    """取得承载 crs 属性的主 Description 元素。

    约定用第一个 Description：实测 ACR 15.x 只写一段，
    若存在多段（第三方工具产物），第一段是主描述块。
    若文档里一段都没有（空 XMP），就地创建一个。
    """
    if doc.descriptions:
        return doc.descriptions[0]

    assert doc.root is not None
    if doc.root.tag == NS.RDF_TAG:
        rdf = doc.root
    else:
        rdf = doc.root.find(NS.RDF_TAG)
        if rdf is None:
            rdf = ET.SubElement(doc.root, NS.RDF_TAG)

    desc = ET.SubElement(rdf, NS.DESCRIPTION_TAG)
    # 用户裁决 #1：rdf:about 写空字符串（与真实 ACR 行为一致，8/8 样本实测）。
    desc.set(NS.ABOUT_ATTR, "")
    doc.descriptions = [desc]
    log.debug("原 XMP 无 rdf:Description，已就地创建。")
    return desc


def _find_attr_owner(doc: XmpDocument, qkey: str) -> ET.Element:
    """找到已承载该属性的 Description 元素。

    为什么要找而不是直接往第一段写：若文件里 crs:Exposure2012 恰好写在第二段，
    而我们在第一段新增同名属性，就会产生**同一个字段的两处定义**。
    XMP 规范允许，但 ACR 的取值优先级未经验证，属于自找风险。
    """
    for desc in doc.descriptions:
        if desc.get(qkey) is not None:
            return desc
    return _primary_description(doc)


def _insert_curve_element(parent: ET.Element, element: ET.Element) -> None:
    """把曲线元素插到"曲线元素区"末尾。

    实测样本里曲线元素位于 properties 的最前面（紧接属性表之后、
    蒙版元素之前）。虽然 XML 中同级元素顺序对 XMP 语义无影响，
    但保持与 ACR 相同的顺序能让用户的文本比对工具少报无意义的 diff。
    因此插入位置 = 最后一个已有曲线元素之后；若没有曲线元素，
    则插到 0 号位置（即所有非曲线子元素之前）。
    """
    curve_tags = {NS.qname("crs", name) for name in F.CURVE_FIELDS}
    insert_at = 0
    for index, child in enumerate(list(parent)):
        if child.tag in curve_tags:
            insert_at = index + 1
        else:
            break
    parent.insert(insert_at, element)


def _write_curve_points(doc: XmpDocument, curve_name: str, points: list[tuple[int, int]]) -> None:
    """写入一条曲线的控制点（替换已有内容）。"""
    qkey = NS.qname("crs", curve_name)

    owner: ET.Element | None = None
    for desc in doc.descriptions:
        found = desc.find(qkey)
        if found is not None:
            owner = found
            break

    if owner is None:
        owner = ET.Element(qkey)
        _insert_curve_element(_primary_description(doc), owner)

    # 清空旧的 rdf:Seq（可能有多个，全部移除），再重建一个。
    for child in list(owner):
        owner.remove(child)

    seq = ET.SubElement(owner, NS.SEQ_TAG)
    for x, y in points:
        li = ET.SubElement(seq, NS.LI_TAG)
        # ACR 的书写风格是 "x, y"（逗号后带一个空格），照抄以保持一致。
        li.text = f"{int(x)}, {int(y)}"


def normalize_curve_points(raw_points: list) -> list[tuple[int, int]]:
    """把 AI 返回的曲线点归一化为 ACR 的 0..255 整数域。

    接受的输入形态：
        [[0.0, 0.0], [0.35, 0.42], [1.0, 1.0]]   ← 0..1 浮点（提示词里约定的形态）
        [[0, 0], [89, 107], [255, 255]]          ← 已是 0..255 整数
        ["0, 0", "89, 107"]                      ← 字符串形态（宽松容错）

    判定规则：若所有坐标值都 <= 1.0，则判定为 0..1 域并按 255 放大；
    否则按 0..255 域直接取整。
    这个启发式在极端情况下会误判（例如曲线只有 (0,0) 和 (1,1) 两个点，
    在两种解释下都是同一条线，因此实际无害）。
    """
    parsed: list[tuple[float, float]] = []
    for item in raw_points:
        if isinstance(item, str):
            parts = [p.strip() for p in item.split(",")]
            if len(parts) != 2:
                continue
            try:
                parsed.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                parsed.append((float(item[0]), float(item[1])))
            except (TypeError, ValueError):
                continue

    if not parsed:
        return []

    looks_normalized = all(0.0 <= x <= CURVE_DOMAIN_AI and 0.0 <= y <= CURVE_DOMAIN_AI
                           for x, y in parsed)

    points: list[tuple[int, int]] = []
    for x, y in parsed:
        if looks_normalized:
            xi = int(round(x * CURVE_DOMAIN_ACR))
            yi = int(round(y * CURVE_DOMAIN_ACR))
        else:
            xi = int(round(x))
            yi = int(round(y))
        # 夹到合法域：AI 偶尔会给出 1.02 这类轻微越界值。
        points.append((max(0, min(CURVE_DOMAIN_ACR, xi)), max(0, min(CURVE_DOMAIN_ACR, yi))))

    # 按 x 升序（ACR 要求控制点按输入值单调递增），并去掉 x 重复的点
    # （重复 x 会让曲线出现垂直段，ACR 会拒绝该曲线）。
    points.sort(key=lambda p: p[0])
    deduped: list[tuple[int, int]] = []
    for point in points:
        if deduped and point[0] == deduped[-1][0]:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped


def apply_params(
    doc: XmpDocument,
    params: dict,
    curves: dict[str, list] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """把参数字典应用到 XMP 文档（就地修改）。

    返回 (已应用字段, 已跳过字段, 警告列表)。

    跳过规则（防御性，绝不静默）：
        - 未在 fields.py 登记的字段 → 跳过（ACR 会静默忽略，属于最危险的失败模式）
        - AI 被禁止写入的字段（机器相关）→ 跳过
        - 非 crs 命名空间的字段 → 跳过
    """
    applied: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []

    # --- 1. 属性型字段 ------------------------------------------------------
    touched_wb_numbers = False
    for name, value in (params or {}).items():
        if name in F.CURVE_FIELDS:
            # 曲线走下面的专门通道，这里跳过。
            continue

        spec = F.get_field(name)
        if spec is None:
            skipped.append(name)
            warnings.append(f"字段 {name} 未在 crs 字段白名单中登记，已跳过（ACR 会静默忽略）")
            continue
        if not spec.ai_writable:
            skipped.append(name)
            warnings.append(f"字段 {name} 属于机器相关字段，本工具只保留不修改，已跳过")
            continue
        if spec.kind == F.KIND_CURVE:
            skipped.append(name)
            continue

        if value is None or value == "":
            continue

        try:
            text = F.format_value(spec, value)
        except (TypeError, ValueError) as exc:
            skipped.append(name)
            warnings.append(f"字段 {name} 的值 {value!r} 无法格式化（{exc}），已跳过")
            continue

        owner = _find_attr_owner(doc, NS.qname("crs", name))
        owner.set(NS.qname("crs", name), text)
        applied.append(name)

        if name in ("Temperature", "Tint"):
            touched_wb_numbers = True

    # --- 2. 白平衡联动（最重要的副作用） -----------------------------------
    # ACR 在 WhiteBalance="As Shot" 时忽略 Temperature/Tint（实测 6/8 样本处于该状态）。
    # 因此只要动了这两个数值，就必须把模式切到 Custom，否则参数写了等于没写。
    if touched_wb_numbers:
        wb_owner = _find_attr_owner(doc, NS.qname("crs", "WhiteBalance"))
        current = wb_owner.get(NS.qname("crs", "WhiteBalance"))
        if current != "Custom":
            wb_owner.set(NS.qname("crs", "WhiteBalance"), "Custom")
            applied.append("WhiteBalance")
            log.debug("已将 WhiteBalance 由 %r 置为 Custom，使 Temperature/Tint 生效。", current)
        touched_wb_numbers = False  # 复用变量，避免下面的分支重复处理

    # --- 3. 曲线（元素型） --------------------------------------------------
    curve_written = False
    for curve_name, raw_points in (curves or {}).items():
        if curve_name not in F.CURVE_FIELDS:
            skipped.append(curve_name)
            warnings.append(f"未知曲线字段 {curve_name}，已跳过")
            continue
        if not raw_points:
            continue
        points = normalize_curve_points(list(raw_points))
        if len(points) < 2:
            skipped.append(curve_name)
            warnings.append(f"曲线 {curve_name} 的有效控制点少于 2 个，已跳过（ACR 无法渲染）")
            continue
        _write_curve_points(doc, curve_name, points)
        applied.append(curve_name)
        curve_written = True

    # --- 4. 曲线名称联动 ----------------------------------------------------
    # 写了点曲线却把名称留成 "Linear"，ACR 会完全忽略这些点。
    # 因此只要写了任一曲线点，就强制把 ToneCurveName2012 置为 Custom。
    if curve_written:
        explicit_name = (params or {}).get("ToneCurveName2012")
        if explicit_name in (None, "", "Linear", "Medium Contrast", "Strong Contrast"):
            name_owner = _find_attr_owner(doc, NS.qname("crs", "ToneCurveName2012"))
            if name_owner.get(NS.qname("crs", "ToneCurveName2012")) != "Custom":
                name_owner.set(NS.qname("crs", "ToneCurveName2012"), "Custom")
                applied.append("ToneCurveName2012")
                log.debug("已写入曲线控制点，同时把 ToneCurveName2012 置为 Custom。")

    # --- 5. 确保 HasSettings 为 True ---------------------------------------
    # 若为 False，ACR 会认为该文件"没有 ACR 设置"从而跳过全部参数。
    has_settings = _find_attr_owner(doc, NS.qname("crs", "HasSettings"))
    if has_settings.get(NS.qname("crs", "HasSettings")) != "True":
        has_settings.set(NS.qname("crs", "HasSettings"), "True")
        log.debug("已把 HasSettings 置为 True。")

    # --- 去重（保序） -------------------------------------------------------
    # 同一个字段可能被记录两次，最典型的是 ToneCurveName2012：
    # 一次来自 AI 明确给出的值，一次来自"写了曲线点就强制置 Custom"的联动修正。
    # 去重只影响日志与统计的可读性，不影响实际写入结果。
    seen: set[str] = set()
    applied_unique: list[str] = []
    for name in applied:
        if name not in seen:
            seen.add(name)
            applied_unique.append(name)

    return applied_unique, skipped, warnings


# ============================================================================
# 序列化
# ============================================================================

def serialize(doc: XmpDocument) -> bytes:
    """把文档序列化为 XMP 字节。

    细节说明：
      - 用 ET.indent 做 3 空格缩进，与样本的书写风格一致（样本用 3 空格）。
        缩进只影响可读性，不影响 XMP 语义。
      - 原样拼回 prefix/suffix（xpacket 处理指令与 BOM），
        保证字节序标记与工具指纹不丢失。
      - 不添加 XML 声明（<?xml ...?>）：真实 ACR 侧车文件没有声明，
        加上去虽然合法但会让文件与 ACR 产物不一致。
      - 不更新 xmp:MetadataDate：本工具的目标是"只改调色参数"，
        额外写入时间戳会让文件的非 crs 属性发生变化，
        给用户的版本比对与我们的回归测试都带来噪声。
    """
    if doc.root is None:
        raise XmpWriteRefusedError("XMP 文档没有根元素，无法序列化。")

    ET.indent(doc.root, space="   ")
    body = ET.tostring(doc.root, encoding="unicode")
    return (doc.prefix_text + body + doc.suffix_text).encode("utf-8")


def _backup_before_overwrite(target: Path, original: bytes) -> Path | None:
    """改已有侧车之前，把原文件备份到 <数据目录>/backups/xmp/。

    【为什么必须有这一步 —— 用户 2026-09 的反馈】
    用户原话：「已经在上一轮处理中被你覆盖掉再也回不来了」。
    侧车是**原地覆盖、无备份**的（DNG 还有 exiftool 的 *_original，侧车什么都没有）。
    我们自己的“读-改-写”会完整保留原有内容，但一旦推演错了（或者你事后怀疑
    程序弄丢了什么），没有备份就真的没得查、没得回。

    规矩：
      · 备份只写**一次**（同名已存在就不动）—— 否则第二次会拿“已经被改过的”
        版本覆盖掉真正的原件，那就失去了备份的意义；
      · 备份目录在数据目录下（**不会**往你的照片目录里堆垃圾），退出清理也不会删它；
      · 备份失败不影响主流程（日志一句，不抛异常）。
    """
    try:
        folder = data_root() / "backups" / "xmp"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = folder / f"{target.stem}.{stamp}.xmp"
        if backup.exists():
            return backup
        backup.write_bytes(original)
        return backup
    except Exception as exc:  # noqa: BLE001 —— 备份是保障措施，不能弄死主流程
        log.warning("备份原有 XMP 失败（继续写）：%s", exc)
        return None


def build_new_document(camera_profile: str | None = None) -> XmpDocument:
    """创建空白侧车 XMP 骨架（可带上按机内设置推导出的相机配置）。"""
    return XmpDocument(_skeleton_xml(camera_profile).encode("utf-8"), source="<new-skeleton>")


def _apply_program_fields(
    doc: XmpDocument,
    fields: dict | None,
) -> tuple[list[str], list[str], list[str]]:
    """写入"由程序决定"的字段（镜头开关这类），返回 (已写, 跳过, 警告)。

    为什么需要一条**单独**的通道：`apply_params` 会拒收所有 `ai_writable=False`
    的字段 —— 那是防止 AI 乱写机器字段的护栏，不能拆。但镜头开关这一类是
    **程序按每张照片自己的基线补缺**的（见 acb/raw/lens.py），必须能写。

    通道刻意很窄，三个约束：
      · 只接受 `LENS_SWITCH_FIELDS` 里的字段名（不是万能后门）；
      · 只补目标文件里**没有**的字段 —— 已有值一律不动（用户的取舍优先）；
      · 写不进去就写进警告，不静默跳过。
    """
    from ..raw.lens import LENS_SWITCH_FIELDS

    applied: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []
    if not fields:
        return applied, skipped, warnings

    owner = _primary_description(doc)
    known = {key.split("}")[-1] for key in owner.attrib if key.startswith("{")}
    for name, value in fields.items():
        if name not in LENS_SWITCH_FIELDS:
            skipped.append(name)
            warnings.append(f"程序字段 {name} 不在镜头开关白名单里，已跳过")
            continue
        if name in known:
            skipped.append(name)
            log.debug("%s 已有值（%s），程序字段不覆盖", name, owner.get(NS.qname("crs", name)))
            continue
        spec = F.get_field(name)
        if spec is None:
            skipped.append(name)
            warnings.append(f"程序字段 {name} 未登记，已跳过")
            continue
        try:
            text = F.format_value(spec, value)
        except (TypeError, ValueError) as exc:
            skipped.append(name)
            warnings.append(f"程序字段 {name} 的值 {value!r} 无法格式化（{exc}），已跳过")
            continue
        owner.set(NS.qname("crs", name), text)
        applied.append(name)
    return applied, skipped, warnings


def render_xmp(
    params: dict,
    curves: dict[str, list] | None = None,
    base: XmpDocument | None = None,
    camera_profile: str | None = None,
    masks: list[dict] | None = None,
    clear_masks: bool = False,
    orientation: str | None = None,
    masks_frame: str = "display",
    program_fields: dict | None = None,
) -> tuple[bytes, list[str], list[str], list[str]]:
    """生成 XMP 字节。

    base 为 None 时从空白骨架开始（此时才写 camera_profile）；否则在 base 上做读-改-写。
    masks 非空时追加几何蒙版（见 xmp/masks.py；已有蒙版的文件不会被覆盖，除非 clear_masks=True）。
    clear_masks=True 会先删掉文件里所有蒙版组 —— 只在明确的"清空重写"场景用。

    `masks_frame` 说明（**横竖方向的红线**）：
      · "display"（默认）：`masks` 里的坐标是**人看到的显示帧**（"天空在上方"），
        必须同时给 `orientation`（每张照片各读一个，批量里各不相同），
        由我们换算成 ACR 要的存储帧；
      · "stored"：`masks` 里已经是存储帧坐标（例如从旧侧车原样搬运），不换算。
    """
    if base is not None:
        doc = base
    else:
        doc = build_new_document(camera_profile)
    applied, skipped, warnings = apply_params(doc, params, curves)
    # 处理版本：没有就补上（否则 ACR 按 2003 引擎渲染，2012 滑块与蒙版全部失效）。
    # 已有值不动 —— 那是用户 ACR 的事实。详见 DEFAULT_PROCESS_VERSION 旁的说明。
    # ⚠ 这一步必须在 apply_params **之后**：apply_params 会重新绑定 warnings（返回值），
    #   放在前面写的警告会被那个绑定丢掉（实测踩到：日志里看不到“已补写处理版本”）。
    added_pv = ensure_process_version(doc)
    if added_pv:
        warnings.append(
            f"原设置包里没有处理版本（ACR 会按「1 版（2003）」处理，"
            f"高光/阴影/蒙版等都不会生效），已写入 {added_pv}（6 版）"
        )
        log.info("已补写 crs:ProcessVersion=%s（原文件没有该字段）", added_pv)
    if program_fields:
        prog_applied, prog_skipped, prog_warnings = _apply_program_fields(doc, program_fields)
        applied.extend(prog_applied)
        skipped.extend(prog_skipped)
        warnings.extend(prog_warnings)
    if masks or clear_masks:
        from . import masks as masks_mod

        if clear_masks:
            removed = masks_mod.clear_masks(doc)
            if removed:
                applied.append(f"清除原有蒙版组×{removed}")
        if masks:
            if masks_frame == "display":
                if not orientation:
                    raise ValueError(
                        "写蒙版必须提供照片方向（orientation）：ACR 的坐标是存储帧，"
                        "批量里每张照片的横竖方向可能不同，猜一个方向会把蒙版画到错的位置。"
                        "（若坐标本来就是存储帧，请显式传 masks_frame='stored'）"
                    )
                specs = [masks_mod.display_to_stored_spec(spec, orientation) for spec in masks]
            elif masks_frame == "stored":
                specs = masks
            else:
                raise ValueError(f"masks_frame 只能是 'display' 或 'stored'，收到 {masks_frame!r}")
            written, mask_warnings = masks_mod.apply_masks(doc, specs)
            warnings.extend(mask_warnings)
            applied.extend(f"蒙版:{name}" for name in written)
    return serialize(doc), applied, skipped, warnings


# ============================================================================
# 落盘
# ============================================================================

def write_sidecar(
    raw_path: Path,
    params: dict,
    curves: dict[str, list] | None = None,
    *,
    camera_profile: str | None = None,
    profile_note: str = "",
    masks: list[dict] | None = None,
    clear_masks: bool = False,
    orientation: str | None = None,
    masks_frame: str = "display",
    program_fields: dict | None = None,
) -> XmpWriteResult:
    """写出/更新非 DNG 的旁侧 XMP 文件。

    读-改-写的具体行为：
        - 若同名 .xmp 已存在 → 读入作为 base，只覆盖目标字段，
          用户的蒙版、曲线、裁剪、历史记录**以及他原本的相机配置**全部保留；
        - 若不存在 → 用最小骨架新建，并把 `camera_profile`
          （由 `acb.raw.camera_profile` 从 RAW 机内设置推导）写进 crs:CameraProfile；
          推导失败时只写一行警告，**不猜**一个值进去。
        - `masks` 非空时追加几何蒙版（线性/径向/画笔）；若该文件已有蒙版组，
          则整组保留、本次不写（见 xmp/masks.py）。
        - `clear_masks=True` 先删掉文件里所有蒙版组（只给"清空重写"场景用）。
        - `orientation` / `masks_frame`：写蒙版时的坐标帧约定，见 `render_xmp` 的说明。
          批量里每张照片的方向可能不同（实测 8 张里三种取值），所以方向必须逐张传入。
    """
    target = sidecar_path_for(raw_path)
    base: XmpDocument | None = None
    warnings: list[str] = []
    # 是否在**已有**侧车里补写了相机配置（新建走的另一条分支）。
    fill_profile = False
    # 被覆盖前的原始字节（用于备份；见 _backup_before_overwrite）。
    original_bytes: bytes | None = None

    if target.is_file():
        try:
            raw_bytes = target.read_bytes()
            original_bytes = raw_bytes
            base = XmpDocument(raw_bytes, source=str(target))
            # 已有侧车里的相机配置：
            #   1) 已有值 → **原样保留**，绝不用推导值覆盖（用户可能在 ACR 里选过别的）；
            #   2) 没有该字段 + 侧车是本程序建的 → 补上（修旧版本留下的文件）；
            #   3) 没有该字段 + 侧车是别的工具（如 ACR）写的 → 不动。
            existing = None
            for desc in base.descriptions:
                existing = desc.get(NS.qname("crs", "CameraProfile"))
                if existing:
                    break
            if existing:
                fill_profile = False
                log.debug("%s 已有侧车且已有相机配置，保持原值（%s）", target.name, existing)
            elif camera_profile and TOOLKIT_TAG.encode("utf-8") in raw_bytes:
                fill_profile = True
                _primary_description(base).set(
                    NS.qname("crs", "CameraProfile"), camera_profile
                )
                log.info(
                    "%s：已有侧车（本程序建的）缺相机配置，已补上「%s」（%s）",
                    target.name,
                    camera_profile,
                    profile_note,
                )
            else:
                fill_profile = False
                log.debug(
                    "%s：已有侧车没相机配置，但不满足补写条件（已有值=%s 能确定=%s 本程序建的=%s）",
                    target.name,
                    existing,
                    bool(camera_profile),
                    TOOLKIT_TAG.encode("utf-8") in raw_bytes,
                )
        except Exception as exc:
            # 已存在的 XMP 无法解析时的策略：
            # 不覆盖它（会毁掉未知内容），而是在旁边写 .new 文件并告警。
            # 这比"猜一个接着改"安全得多。
            fallback = target.with_name(target.name + ".new")
            data, applied, skipped, extra = render_xmp(
                params, curves, None, masks=masks, clear_masks=clear_masks,
                orientation=orientation, masks_frame=masks_frame,
                program_fields=program_fields,
            )
            fallback.write_bytes(data)
            warnings.append(
                f"原有 XMP 无法解析（{exc}），为避免破坏其中内容，"
                f"已把新设置写到 {fallback.name}，请人工确认后再替换。"
            )
            log.error("%s 解析失败，已改写为 %s", target, fallback)
            return XmpWriteResult(
                mode="sidecar",
                target=fallback,
                warnings=warnings + extra,
                applied_fields=applied,
                skipped_fields=skipped,
                camera_profile_note="原有 XMP 无法解析，未参与相机配置判断",
                created_new=True,
            )
    elif camera_profile:
        log.info("%s：新建侧车，按机内设置写入相机配置「%s」（%s）", target.name, camera_profile, profile_note)
    else:
        # 这里是用户真实反馈的缺陷现场：不写相机配置，ACR 就会回落到 Adobe 标准。
        # 逐文件只记 debug，由调用方在批次结束后汇总一句提醒（见 output_mode）。
        log.debug("%s：新建侧车，但无法确定相机配置：%s", target.name, profile_note or "未提供")

    data, applied, skipped, extra = render_xmp(
        params, curves, base, camera_profile, masks=masks, clear_masks=clear_masks,
        orientation=orientation, masks_frame=masks_frame, program_fields=program_fields,
    )
    warnings.extend(extra)

    # 原子写：先写临时文件再 replace，避免被强杀时留下半截 XMP。
    tmp = target.with_name(target.name + ".acbtmp")
    tmp.write_bytes(data)
    # 覆盖已有文件之前先留一份原件（只留一次）。
    if original_bytes is not None:
        backup = _backup_before_overwrite(target, original_bytes)
        if backup is not None:
            warnings.append(
                f"覆盖前已备份原有 XMP → {backup}"
            )
            log.info("已备份原有 XMP：%s", backup)
    tmp.replace(target)

    log.info(
        "已写出侧车 XMP：%s（应用 %d 个字段，跳过 %d 个）",
        target,
        len(applied),
        len(skipped),
    )
    created_new = base is None
    # 已有侧车时：只有"本程序建的、缺该字段、且能确定值"那一种情况才算写了。
    wrote_profile = created_new or bool(base is not None and fill_profile)
    return XmpWriteResult(
        mode="sidecar",
        target=target,
        warnings=warnings,
        applied_fields=applied,
        skipped_fields=skipped,
        camera_profile=camera_profile if wrote_profile else None,
        camera_profile_note=(
            profile_note if wrote_profile else "已有侧车，相机配置保持原值"
        ),
        created_new=created_new,
    )


def validate_dng_policy(raw_path: Path, force_sidecar_for_dng: bool) -> list[str]:
    """生成 DNG 策略相关的告警文本（硬约束 #1 要求"须在界面弹出警告并记录 warn"）。"""
    warnings: list[str] = []
    if not is_embedded_mode(raw_path):
        return warnings

    if force_sidecar_for_dng:
        warnings.append(
            f"{raw_path.name}：DNG 的 XMP 已嵌入文件内部，ACR 优先读取嵌入元数据，"
            f"旁侧 .xmp 可能被忽略或产生冲突。本次仍按你的选择生成旁侧文件 "
            f"{embedded_sidecar_path_for(raw_path).name}，但该文件可能不生效。"
        )
    else:
        warnings.append(
            f"{raw_path.name}：DNG 将采用「读出嵌入 XMP → 修改 → 写回文件内部」策略，"
            "不会生成旁侧 .xmp。注意：该操作会直接改写你的原片，"
            "exiftool 会自动生成一份 *_original 备份，请在确认效果后自行清理备份文件。"
        )
    return warnings
