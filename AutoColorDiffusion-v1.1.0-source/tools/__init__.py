# -*- coding: utf-8 -*-
"""离线工具集。

这些脚本不参与程序运行，只用于开发期与用户自查：
    dump_crs_fields.py   扫描真实 XMP，反推 crs: 字段清单（"漏字段门禁"）
    roundtrip_test.py    读-改-写-再读的往返回归测试（保护用户的蒙版数据）
    verify_xmp.py        用 exiftool 校验写出的 XMP 是否合法
"""
