# -*- coding: utf-8 -*-
"""XMP 命名空间表与 xpacket 包装处理。

为什么必须集中管理命名空间：
    ElementTree 序列化时用的前缀来自全局的 register_namespace 表。
    如果某个前缀没有注册，ET 会写成 ns0:、ns1: 这样的自动前缀，
    虽然 XML 语义等价，但 ACR 之外的工具（exiftool、桥接程序、
    用户的文本比对脚本）会看到完全不同的文件，且 ACR 对
    "非标准前缀"的容错未经验证。因此宁可多注册几个，
    也不要让任何前缀落到自动生成路径上。

本表的前 13 项在真实样本 test_data/*.xmp 中逐条出现过，是实测依据；
后几项为常见扩展（stRef/Mask 等），用于兼容其他工具写出的文件。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

# --- 命名空间 URI -----------------------------------------------------------

X_NS = "adobe:ns:meta/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMP_NS = "http://ns.adobe.com/xap/1.0/"
TIFF_NS = "http://ns.adobe.com/tiff/1.0/"
EXIF_NS = "http://ns.adobe.com/exif/1.0/"
EXIFEX_NS = "http://cipa.jp/exif/1.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"
AUX_NS = "http://ns.adobe.com/exif/1.0/aux/"
PHOTOSHOP_NS = "http://ns.adobe.com/photoshop/1.0/"
XMPMM_NS = "http://ns.adobe.com/xap/1.0/mm/"
STEVT_NS = "http://ns.adobe.com/xap/1.0/sType/ResourceEvent#"
STREF_NS = "http://ns.adobe.com/xap/1.0/sType/ResourceRef#"
XML_NS = "http://www.w3.org/XML/1998/namespace"

# --- 本项目最核心的两个命名空间 ---------------------------------------------
# crs = Camera Raw Settings：用户的实际调整参数。
#   这是 AI 产出参数唯一允许写入的命名空间（硬约束 #5：字段名须为 crs: 前缀字段名）。
CRS_NS = "http://ns.adobe.com/camera-raw-settings/1.0/"

# crd = Camera Raw Defaults：相机出厂默认基线。
#   实测样本中 crd:CameraProfile / crd:LookName / crd:LensProfileEnable 与
#   crs: 同名同义字段并存。它是硬约束 #15"区分用户实际调整与相机默认"
#   最可靠的判据来源：crs:X == crd:X 即表示用户没有改动 X。
#   本工具**只读** crd，绝不写入。
CRD_NS = "http://ns.adobe.com/camera-raw-defaults/1.0/"

# 前缀 → URI 映射。键必须与真实样本中出现的前缀一致。
PREFIX_TO_URI: dict[str, str] = {
    "x": X_NS,
    "rdf": RDF_NS,
    "xmp": XMP_NS,
    "tiff": TIFF_NS,
    "exif": EXIF_NS,
    "exifEX": EXIFEX_NS,
    "dc": DC_NS,
    "aux": AUX_NS,
    "photoshop": PHOTOSHOP_NS,
    "xmpMM": XMPMM_NS,
    "stEvt": STEVT_NS,
    "stRef": STREF_NS,
    "crs": CRS_NS,
    "crd": CRD_NS,
}

URI_TO_PREFIX: dict[str, str] = {uri: prefix for prefix, uri in PREFIX_TO_URI.items()}

# 供训练模式排除的命名空间前缀（相机/工具元数据，与审美无关）
NON_STYLE_PREFIXES: tuple[str, ...] = (
    "xmp",
    "tiff",
    "exif",
    "exifEX",
    "dc",
    "aux",
    "photoshop",
    "xmpMM",
    "stEvt",
    "stRef",
    "crd",
)

# --- xpacket 包装 -----------------------------------------------------------
# 真实 ACR 产物长这样：
#   <x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0-...">
#     <rdf:RDF ...> ... </rdf:RDF>
#   </x:xmpmeta>
# 外层还可能有 <?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?> ... <?xpacket end="w"?>
#
# 处理策略：把包装与会话头（xpacket 处理指令）当作"不透明字符串"原样保留，
# 只解析并修改中间的 x:xmpmeta 子树。这样能保证：
#   1. 字节序标记（BOM，即 begin 属性里的 \ufeff）不被破坏；
#   2. x:xmptk 这类工具指纹被保留（对排查"是哪版 ACR 写的"很有用）；
#   3. 不会因为 ET 不认处理指令而丢内容。
XPACKET_BEGIN_PREFIX = "<?xpacket"
XPACKET_END_SUFFIX = "?>"

# x:xmpmeta 元素的限定名
XMPMETA_TAG = f"{{{X_NS}}}xmpmeta"
RDF_TAG = f"{{{RDF_NS}}}RDF"
DESCRIPTION_TAG = f"{{{RDF_NS}}}Description"
SEQ_TAG = f"{{{RDF_NS}}}Seq"
ALT_TAG = f"{{{RDF_NS}}}Alt"
BAG_TAG = f"{{{RDF_NS}}}Bag"
LI_TAG = f"{{{RDF_NS}}}li"
ABOUT_ATTR = f"{{{RDF_NS}}}about"


def qname(prefix: str, local: str) -> str:
    """构造 ElementTree 的限定名，例如 qname("crs", "Exposure2012")。

    ElementTree 用的是 Clark 记法 {uri}local，而不是 crs:local。
    """
    uri = PREFIX_TO_URI.get(prefix)
    if uri is None:
        raise KeyError(f"未注册的命名空间前缀：{prefix}")
    return f"{{{uri}}}{local}"


def split_qname(tag: str) -> tuple[str, str]:
    """把 Clark 记法拆回 (前缀, 本地名)。未注册的 URI 返回 ("?", 本地名)。"""
    if not tag.startswith("{"):
        return ("", tag)
    uri, _, local = tag[1:].partition("}")
    return (URI_TO_PREFIX.get(uri, "?"), local)


def register_all() -> None:
    """把所有前缀注册到 ElementTree，避免序列化时出现 ns0: 之类的自动前缀。

    必须在**解析之前**调用：register_namespace 影响的是后续
    ET.tostring/write 的输出，而解析时需要前缀表已就绪才能正确
    映射回我们期望的前缀（ET 内部按 URI 存储，注册表决定写出的前缀）。

    踩坑记录（这是本项目最隐蔽的一个 bug）：
        最初 register_all() 只在需要时手动调用，结果序列化时 ET 为
        crs / x / photoshop 等命名空间生成了 ns0、ns2、ns11 这样的自动前缀，
        而 rdf 却仍是 rdf（因为 ET 的 _namespace_map 里**预置**了 rdf、dc、
        xhtml、xsi 等几个常见前缀）。于是输出里出现"部分前缀正常、
        部分前缀是 nsN"的混合形态。
        更糟的是下游影响：读取时我们用 text.find("<x:xmpmeta") 定位根元素，
        自动前缀让它变成 <ns0:xmpmeta>，查找失败后退化为只取 <rdf:RDF> 片段，
        而该片段依赖父元素上的 ns0 声明 —— 于是 ET.fromstring 抛
        "unbound prefix"，表现为"自己写出的文件自己读不回来"。

    修复方式：改为**模块导入时自动注册**。register_namespace 是全局状态，
    对所有 xmp 操作都需要，因此在 namespaces.py 被导入时执行是最省心且不会遗漏的做法。
    """
    for prefix, uri in PREFIX_TO_URI.items():
        if prefix == "xml":
            # xml 前缀由 XML 规范保留，ET 不允许注册，跳过。
            continue
        ET.register_namespace(prefix, uri)


# 模块导入即注册。理由见 register_all 的注释：这是本项目的必做前置步骤，
# 且无法用"在某个函数里调用一次"来可靠覆盖（解析与序列化都可能先发生）。
register_all()
