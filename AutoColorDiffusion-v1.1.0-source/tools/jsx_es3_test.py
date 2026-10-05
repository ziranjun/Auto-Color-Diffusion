# -*- coding: utf-8 -*-
"""在**真正的 ES3 引擎**里跑 export_batch.jsx 内嵌的 JSON 实现。

为什么需要这个工具（被一个真实事故逼出来的）
--------------------------------------------
用户在 Photoshop 里执行导出脚本，弹窗报「manifest.json 解析失败：json 未定义」。

根因：ExtendScript 只有 **ECMAScript 3**，**没有 JSON 对象** ——
`JSON.parse` 是 ES5 才有的。同一个文件里的 `JSON.stringify` 也踩了同一个坑，
而它写在 `catch (e) {}` 里被静默吞掉，所以 `ps_result.json` 从来没被写出来过，
Python 侧只能含糊地报「结果未知」，把真正的线索完全掩盖了。

为什么必须有自动化测试，而不是"读代码检查一下"
----------------------------------------------
这类缺陷的特点是：**Python 侧的全部自检都是绿的** ——
因为 Python 有 json 模块，manifest 生成得完全正确；
问题只在 Photoshop 的脚本引擎里出现，而那个引擎在本机没有可编程入口。

做法
----
把 jsx 里 `ACB_JSON_POLYFILL` 区块**原样抽出来**（不是抄一份到测试里，
抽的是同一段源码，避免测试与产物分叉），放进 Windows 自带的 JScript 5.8 ——
它和 ExtendScript 同属 ECMAScript 3，同样没有 JSON 对象（脚本会当场验证这一点）。
然后用真实的 manifest.json 与一批边界用例，与 Python 的 json 模块逐项比对。

覆盖的断言
----------
    1. 引擎确实没有 JSON，且已走自实现分支（否则测的是宿主的，等于没测）；
    2. 真实 manifest.json（中文路径、反斜杠、嵌套数组）往返一致；
    3. 所有合法用例：解析成功 + 序列化产物是**合法 JSON** + 往返一致；
    4. 转义序列、控制字符、emoji、数字形态、空容器、深层嵌套都能过；
    5. 带 UTF-8 BOM 的输入被容忍（用户用记事本另存就会带上 BOM）；
    6. 非法输入必须**抛错**，而不是静默返回 undefined 或半个对象；
    7. 静态扫描：jsx 自身不得使用 ES3 没有的语法（排除 polyfill 区块本身）；
    8. 目录创建逻辑：用打桩的 File/Folder 验证"逐级创建"与"失败退回兜底"。
       这一条同样重要：ExtendScript 的 Folder.create() 不创建中间层级，
       写错的表现是**静默降级**（日志落到兜底位置，用户看不到任何提示）。

用法
----
    python tools/jsx_es3_test.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 控制台/管道的代码页可能是 GBK（Windows 默认 936），直接 print "✓/✗" 会抛
# UnicodeEncodeError —— 那会把"未通过清单"变成一段 traceback，等于把最关键的信息
# 藏起来（本文件真的踩到过这个坑）。统一把标准输出切到 UTF-8 并替换掉不能编码的字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
JSX_PATH = ROOT / "assets" / "jsx" / "export_batch.jsx"

BEGIN_MARK = "// ==== ACB_JSON_POLYFILL_BEGIN ===="
END_MARK = "// ==== ACB_JSON_POLYFILL_END ===="
FS_BEGIN_MARK = "// ==== ACB_FS_HELPERS_BEGIN ===="
FS_END_MARK = "// ==== ACB_FS_HELPERS_END ===="

PASS = "[通过]"
FAIL = "[失败]"

_results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"{PASS if ok else FAIL} {name}")
    if detail:
        print(f"        {detail}")


# ---------------------------------------------------------------------------
# 静态扫描：jsx 里不得出现 ES3 引擎没有的东西
# ---------------------------------------------------------------------------
# 这些都是 ES5+ 才有的，在 ExtendScript / JScript 5.8 里统统不存在，
# 一旦有人在 jsx 里用了，就会重演"json 未定义"这一类事故。
ES5_PATTERNS: list[tuple[str, str]] = [
    (r"\bJSON\s*\.", "JSON 对象（本次事故的根因）"),
    (r"\.map\s*\(", "Array.prototype.map"),
    (r"\.forEach\s*\(", "Array.prototype.forEach"),
    (r"\.filter\s*\(", "Array.prototype.filter"),
    (r"\.reduce\s*\(", "Array.prototype.reduce"),
    (r"\.some\s*\(", "Array.prototype.some"),
    (r"\.every\s*\(", "Array.prototype.every"),
    (r"\.trim\s*\(", "String.prototype.trim"),
    (r"\bObject\.keys\b", "Object.keys"),
    (r"\bObject\.create\b", "Object.create"),
    (r"\bObject\.defineProperty\b", "Object.defineProperty"),
    (r"\bArray\.isArray\b", "Array.isArray"),
    (r"\.bind\s*\(", "Function.prototype.bind"),
    (r"\bDate\.now\b", "Date.now"),
    # `indexOf/lastIndexOf` 是 ES5 的**数组**方法，而 `String.prototype.indexOf`
    # 在 ES3 里就有 —— 两者写法一样，扫描器分不清接收者是数组还是字符串。
    # 所以只拦"接收者明显不是字符串"的写法（数字/数组字面量/identifer 后紧跟
    # `.indexOf(`，而字符串字面量 `"x".indexOf(` 与常见字符串变量名除外），
    # 宁可漏报也不要把合法的字符串查找错报成 ES5 用法。
    (r"(?<![\"'])\b[a-zA-Z_$][\w$]*\.(?:indexOf|lastIndexOf)\s*\(",
     "Array.prototype.indexOf（若接收者确实是字符串，这是 ES3 合法的，可在这一行加注释说明）"),
    (r"\bconst\s+\w", "const 声明"),
    (r"\blet\s+\w", "let 声明"),
]


def extract_block(text: str, begin: str, end: str, label: str) -> str:
    """按标记区块抽取源码。抽不到直接抛错——静默抽到空字符串会让测试假装通过。"""
    start = text.find(begin)
    stop = text.find(end)
    if start == -1 or stop == -1 or stop <= start:
        raise RuntimeError(f"{JSX_PATH.name} 里找不到 {label} 标记区块（标记被改动过？）")
    return text[start:stop + len(end)]


def extract_fs_helpers(text: str) -> str:
    """抽出"目录创建"辅助函数，供打桩测试单独执行。"""
    return extract_block(text, FS_BEGIN_MARK, FS_END_MARK, "ACB_FS_HELPERS")


def extract_polyfill(text: str) -> str:
    """从**实际发布**的 jsx 里抽出 JSON 兼容层源码。

    刻意不"抄一份"到这里：抄一份就会出现测试与产物分叉——
    改了 jsx 忘了改测试，测试照样全绿而 Photoshop 里依然报错。
    """
    start = text.find(BEGIN_MARK)
    end = text.find(END_MARK)
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(
            f"{JSX_PATH.name} 里找不到 ACB_JSON_POLYFILL 标记区块（标记被改动过？）"
        )
    return text[start:end + len(END_MARK)]


def scan_es3_safety(text: str) -> list[str]:
    """扫描 polyfill 之外的部分有没有用 ES3 不支持的特性。"""
    blanked = text.replace(extract_polyfill(text), "")
    problems: list[str] = []
    for line_no, line in enumerate(blanked.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue  # 注释里出现这些词是正常的（本项目注释大量提及它们）
        for pattern, label in ES5_PATTERNS:
            if re.search(pattern, line):
                problems.append(f"第 {line_no} 行用了 {label}：{stripped[:70]}")

    # ES3 不允许对象/数组字面量的尾随逗号（ES5 才允许）。
    for line_no, line in enumerate(blanked.splitlines(), start=1):
        if re.search(r",\s*$", line) and not line.strip().startswith("//"):
            following = blanked.splitlines()
            if line_no < len(following) and following[line_no].strip()[:1] in ("}", "]"):
                problems.append(f"第 {line_no} 行有尾随逗号（ES3 不允许）")
    return problems


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------
VALID_CASES: dict[str, object] = {
    "basic": {
        "version": 1,
        "colorSpace": "Adobe RGB(1998)",
        "psJpegQuality": 12,
        "items": [
            {"raw": "D:\\a\\x.CR3", "out": "D:\\a\\_export\\x_E.jpg", "xmp": "D:\\a\\x.xmp"},
        ],
    },
    # 中文 / emoji / 全角空格 —— 真实图片路径里一定会有中文
    "unicode": {
        "items": [{"raw": "D:\\照片\\海边 01.CR3", "out": "D:\\照片\\_export\\海边 01_E.jpg"}],
        "备注": "全角空格\u3000与 emoji 🎞️ 也要能过",
    },
    # 所有 JSON 转义序列
    "escapes": {
        "quote": 'he said "hi"',
        "backslash": "C:\\Users\\x",
        "slash": "a/b",
        "newline": "line1\nline2",
        "tab": "a\tb",
        "cr": "a\rb",
        "backspace": "a\bb",
        "formfeed": "a\fb",
        "unicode_escape": "\u4e2d\u6587",
        "control": "\u0001\u001f",
    },
    # 数字形态
    "numbers": {
        "zero": 0,
        "neg": -1,
        "float": -0.35,
        "exp": 1.5e3,
        "neg_exp": 2e-3,
        "big": 1234567890123,
    },
    # 空容器与嵌套
    "containers": {"empty_obj": {}, "empty_arr": [], "nested": [[[]], {"a": [{"b": []}]}]},
    # 布尔与 null
    "literals": {"t": True, "f": False, "n": None, "s_true": "true"},
}

# 非法输入：必须抛错。每项是 (名称, 内容, 说明)
INVALID_CASES: list[tuple[str, str, str]] = [
    ("empty", "", "空文件"),
    ("truncated_obj", '{"a": 1', "对象没有闭合"),
    ("truncated_str", '{"a": "abc', "字符串没有结束引号"),
    ("trailing_comma", '{"a": 1,}', "ES3/JSON 都不允许尾随逗号"),
    ("single_quote", "{'a': 1}", "键必须用双引号"),
    ("unquoted_key", "{a: 1}", "键必须加引号"),
    ("bad_escape", '{"a": "\\x"}', "非法转义序列"),
    ("bad_unicode", '{"a": "\\uZZZZ"}', "非法 \\u 转义"),
    ("trailing_junk", '{"a": 1} extra', "解析完成后有多余内容"),
    ("bare_word", "undefined", "不是合法 JSON 取值"),
    ("not_closed_arr", "[1, 2", "数组没有闭合"),
    ("misplaced_comma", "[1,,2]", "连续逗号"),
]


def build_deep() -> object:
    """构造一个 30 层嵌套的对象，验证递归解析不会提前崩。"""
    node: object = {"leaf": True}
    for _ in range(30):
        node = {"child": node}
    return node


# ---------------------------------------------------------------------------
# JScript 测试驱动
# ---------------------------------------------------------------------------
HARNESS = r"""
// 在 ES3 引擎（cscript 的 JScript 5.8）里跑被抽出来的 JSON 实现。
// 注意：本文件是**测试驱动**，可以用 WSH 专有的 ActiveXObject 做文件 I/O；
// 被测的 JSON 实现本身则严格只用 ES3 语法。
(function () {
    // 关键：显式声明一个未初始化的 JSON，确保即使宿主自带 JSON
    // 也会走我们自己的实现（否则测的是宿主的，等于什么都没测）。
    var JSON;

__POLYFILL__

    var report = [];
    report.push("ENGINE_JSON=" + (typeof JSON !== "undefined" ? "present" : "absent"));
    report.push("CODEC_IS_POLYFILL=" + (JsonCodec === AcbJSON));

    // ================= 目录创建逻辑：用打桩的 File / Folder 测 =================
    // ExtendScript 的 File/Folder 在本机没有可编程入口，所以打桩模拟其语义。
    // **关键的一条**：create() 只在父目录已存在时才成功 ——
    // 这正是"必须逐级创建"这个修复存在的原因，也是本组用例要守住的行为。
    var fsExists = {};
    var fsCreated = [];
    var fsFail = {};

    function parentOf(path) {
        var idx = path.lastIndexOf("\\");
        if (idx <= 0) { return ""; }
        return path.substring(0, idx);
    }

    function FolderStub(path) {
        this.fsName = path;
        this.exists = fsExists[path] === true;
        var up = parentOf(path);
        this.parent = up ? new FolderStub(up) : null;
    }
    FolderStub.prototype.create = function () {
        fsCreated.push(this.fsName);
        if (fsFail[this.fsName] === true) { return false; }
        var up = parentOf(this.fsName);
        if (up !== "" && fsExists[up] !== true) { return false; }
        fsExists[this.fsName] = true;
        return true;
    };

    function FileStub(path) {
        this.fsName = path;
        this.parent = new FolderStub(parentOf(path));
    }

    var File = FileStub;

__FSHELPERS__

    var fsResults = [];

    function resetFs() {
        fsExists = {};
        fsExists["C:\\stub"] = true;
        fsCreated = [];
        fsFail = {};
    }

    function recordFs(name, ok, detail) {
        fsResults.push("FSCASE " + name + " " + (ok ? "PASS" : "FAIL") + " " + detail);
    }

    // 用例 1：整条目录链都不存在 → 必须逐级创建，并返回首选路径
    resetFs();
    var fb1 = new FileStub("X:\\fb\\ps_log.txt");
    var got1 = preferTarget("C:\\stub\\logs\\photoshop\\ps_export_x.txt", fb1);
    recordFs(
        "create_full_chain",
        got1 !== fb1 && got1.fsName === "C:\\stub\\logs\\photoshop\\ps_export_x.txt"
            && fsCreated.join(";") === "C:\\stub\\logs;C:\\stub\\logs\\photoshop",
        "created=" + fsCreated.join(";")
    );

    // 用例 2：某一级创建失败 → 必须退回兜底路径（而不是返回一个写不进去的路径）
    resetFs();
    fsFail["C:\\stub\\logs"] = true;
    var fb2 = new FileStub("X:\\fb\\ps_log.txt");
    var got2 = preferTarget("C:\\stub\\logs\\photoshop\\ps_export_x.txt", fb2);
    recordFs("fallback_on_failure", got2 === fb2, "got=" + got2.fsName);

    // 用例 3：路径为空 → 直接用兜底，且不尝试创建任何目录
    resetFs();
    var fb3 = new FileStub("X:\\fb\\ps_log.txt");
    var got3 = preferTarget("", fb3);
    recordFs(
        "empty_path_uses_fallback",
        got3 === fb3 && fsCreated.length === 0,
        "created=" + fsCreated.join(";")
    );

    // 用例 4：中间层级已存在 → 只创建缺失的那一级，不重复创建
    resetFs();
    fsExists["C:\\stub\\logs"] = true;
    var fb4 = new FileStub("X:\\fb\\ps_log.txt");
    var got4 = preferTarget("C:\\stub\\logs\\photoshop\\ps_result_x.json", fb4);
    recordFs(
        "skip_existing_levels",
        got4 !== fb4 && fsCreated.join(";") === "C:\\stub\\logs\\photoshop",
        "created=" + fsCreated.join(";")
    );

    var fsOkCount = 0;
    for (var fi = 0; fi < fsResults.length; fi++) {
        if (fsResults[fi].indexOf(" PASS ") !== -1) { fsOkCount++; }
        report.push(fsResults[fi]);
    }
    report.push("FS_TOTAL=" + fsResults.length + " FS_PASS=" + fsOkCount);

    function readUtf8(path) {
        var stream = new ActiveXObject("ADODB.Stream");
        stream.Type = 2;
        stream.Charset = "utf-8";
        stream.Open();
        stream.LoadFromFile(path);
        var text = stream.ReadText();
        stream.Close();
        return text;
    }

    function writeUtf8(path, text) {
        var stream = new ActiveXObject("ADODB.Stream");
        stream.Type = 2;
        stream.Charset = "utf-8";
        stream.Open();
        stream.WriteText(text);
        stream.SaveToFile(path, 2);
        stream.Close();
    }

    function readLines(path) {
        var text = readUtf8(path);
        return text.replace(/\r/g, "").split("\n");
    }

    var workDir = __WORKDIR__;
    var validList = readLines(workDir + __VALIDLIST__);
    var invalidList = readLines(workDir + __INVALIDLIST__);

    for (var i = 0; i < validList.length; i++) {
        var name = validList[i];
        if (name === "") { continue; }
        var source = readUtf8(workDir + "\\" + name + ".in.json");
        try {
            var parsed = JsonCodec.parse(source);
            writeUtf8(workDir + "\\" + name + ".out.json", JsonCodec.stringify(parsed));
            report.push("VALID " + name + " OK");
        } catch (e) {
            report.push("VALID " + name + " THREW " + ((e && e.message) ? e.message : String(e)));
        }
    }

    for (var j = 0; j < invalidList.length; j++) {
        var bad = invalidList[j];
        if (bad === "") { continue; }
        var badSource = readUtf8(workDir + "\\" + bad + ".in.json");
        var threw = false;
        var detail = "";
        try {
            JsonCodec.parse(badSource);
        } catch (e) {
            threw = true;
            detail = (e && e.message) ? e.message : String(e);
        }
        report.push("INVALID " + bad + (threw ? " THREW" : " NOTHREW") + " " + detail);
    }

    writeUtf8(workDir + "\\report.txt", report.join("\r\n"));
})();
"""


def run_engine(workdir: Path, polyfill: str, fs_helpers: str = "",
               valid_file: str = "valid.txt",
               invalid_file: str = "invalid.txt") -> subprocess.CompletedProcess:
    """在 cscript(JScript) 里执行测试驱动。"""
    harness = (
        HARNESS.replace("__POLYFILL__", polyfill)
        .replace("__FSHELPERS__", fs_helpers)
        .replace("__WORKDIR__", json.dumps(str(workdir)))
        .replace("__VALIDLIST__", json.dumps("\\" + valid_file))
        .replace("__INVALIDLIST__", json.dumps("\\" + invalid_file))
    )
    script = workdir / "harness.js"
    # 必须写成 **UTF-16**。WSH 对 .js 的编码判定很挑，以下两条都是实际踩出来的：
    #   - UTF-8 不带 BOM：按系统 ANSI 代码页解析，中文注释被拆坏，
    #     报 "Unterminated string constant"，且行号列号完全对不上；
    #   - UTF-8 带 BOM：WSH 不认，BOM 被当成普通字符，首行的 // 会被
    #     误判成正则起始，报 "Expected '/'"；
    #   - UTF-16（带 BOM）：WSH 原生支持，实测正常。
    script.write_text(harness, encoding="utf-16", newline="")
    return subprocess.run(
        ["cscript", "//nologo", "//E:JScript", str(script)],
        capture_output=True,
        timeout=180,
    )


def main() -> int:
    print("=" * 90)
    print("export_batch.jsx 内嵌 JSON 实现的 ES3 引擎实测")
    print("=" * 90)
    print("说明：把 jsx 里的 ACB_JSON_POLYFILL 区块原样抽出，")
    print("      放进 Windows 自带的 JScript 5.8（与 ExtendScript 同属 ES3）执行。")
    print("=" * 90 + "\n")

    cscript = shutil.which("cscript") or r"C:\Windows\System32\cscript.exe"
    if not Path(cscript).is_file():
        print("找不到 cscript.exe，本项无法运行（Windows 上应当自带）。")
        return 2

    jsx_text = JSX_PATH.read_text(encoding="utf-8")

    # --- 断言 0：jsx 自身不得使用 ES3 没有的语法 ---------------------------
    problems = scan_es3_safety(jsx_text)
    record(
        "静态扫描：jsx 未使用 ES3 不支持的特性",
        not problems,
        f"问题：{problems or '无'}",
    )

    try:
        polyfill = extract_polyfill(jsx_text)
        fs_helpers = extract_fs_helpers(jsx_text)
    except RuntimeError as exc:
        record("从 jsx 抽出代码区块", False, str(exc))
        return 1
    record("从 jsx 抽出代码区块", True,
           f"JSON 兼容层 {len(polyfill.splitlines())} 行；"
           f"目录创建辅助 {len(fs_helpers.splitlines())} 行")

    VALID_CASES["deep"] = build_deep()

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)

        # 期望值表：往返比较统一从这里取，避免每个用例各写一遍判断。
        expected: dict[str, object] = {}
        valid_names: list[str] = []

        for name, payload in VALID_CASES.items():
            (workdir / f"{name}.in.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            valid_names.append(name)
            expected[name] = payload

        # 真实 manifest.json —— 最贴近线上的一步：
        # 它由生产代码 render_export_outputs 生成，含中文路径、反斜杠与嵌套数组。
        real_note = ""
        try:
            from acb.ps.jsx_render import render_export_outputs

            plan = render_export_outputs(
                script_dir=workdir / "real",
                manifest_items=[
                    {
                        "raw": "D:\\照片库\\海边 01.CR3",
                        "out": "D:\\照片库\\_export\\海边 01_E.jpg",
                        "xmp": "D:\\照片库\\海边 01.xmp",
                    },
                    {"raw": "D:\\a\\b.CR3", "out": "D:\\a\\_export\\b_E.jpg", "xmp": ""},
                ],
                color_space="Adobe RGB(1998)",
                quality_value=11,
            )
            real_source = plan.manifest_path.read_text(encoding="utf-8")
            (workdir / "realmanifest.in.json").write_text(real_source, encoding="utf-8")
            valid_names.append("realmanifest")
            expected["realmanifest"] = json.loads(real_source)
            real_note = (
                f"质量={expected['realmanifest'].get('psJpegQuality')} "
                f"项数={len(expected['realmanifest'].get('items') or [])}"
            )
        except Exception as exc:  # noqa: BLE001 - 自检工具，报错即失败
            record("生成真实 manifest.json 用于实测", False, str(exc))
            real_note = "生成失败"
        else:
            record("生成真实 manifest.json 用于实测", True, real_note)

        # 带 UTF-8 BOM 的输入：用户用记事本另存就会出现 BOM，必须能容忍。
        (workdir / "with_bom.in.json").write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"a": 1}, ensure_ascii=False).encode("utf-8")
        )
        valid_names.append("with_bom")
        expected["with_bom"] = {"a": 1}

        invalid_names: list[str] = []
        for name, raw, _why in INVALID_CASES:
            (workdir / f"{name}.in.json").write_text(raw, encoding="utf-8")
            invalid_names.append(name)

        (workdir / "valid.txt").write_text("\r\n".join(valid_names), encoding="utf-8")
        (workdir / "invalid.txt").write_text("\r\n".join(invalid_names), encoding="utf-8")

        proc = run_engine(workdir, polyfill, fs_helpers)
        report_path = workdir / "report.txt"
        if proc.returncode != 0 or not report_path.is_file():
            err = (proc.stderr or b"").decode("mbcs", errors="replace")[:600]
            record("在 ES3 引擎里执行测试驱动", False, err or "未生成 report.txt")
            return 1
        record("在 ES3 引擎里执行测试驱动", True, f"{len(valid_names)} 个合法 + "
                                                 f"{len(invalid_names)} 个非法用例")

        lines = report_path.read_text(encoding="utf-8-sig").splitlines()

        # --- 断言 1：引擎确实没有 JSON，且走的是自实现分支 -------------------
        engine_line = next((l for l in lines if l.startswith("ENGINE_JSON=")), "")
        codec_line = next((l for l in lines if l.startswith("CODEC_IS_POLYFILL=")), "")
        record(
            "ES3 引擎无 JSON，且已强制走自实现分支",
            codec_line.endswith("true"),
            f"{engine_line}；{codec_line}",
        )

        # --- 断言 2：合法用例解析成功 + 产物是合法 JSON + 往返一致 ------------
        mismatches: list[str] = []
        for name in valid_names:
            status = next((l for l in lines if l.startswith(f"VALID {name} ")), "")
            if " OK" not in status:
                mismatches.append(f"{name}: {status or '无记录'}")
                continue
            out_file = workdir / f"{name}.out.json"
            if not out_file.is_file():
                mismatches.append(f"{name}: 未产出 out.json")
                continue
            try:
                round_tripped = json.loads(out_file.read_text(encoding="utf-8-sig"))
            except ValueError as exc:
                # 产出的不是合法 JSON —— 转义逻辑有 bug，这是最严重的一类。
                mismatches.append(f"{name}: 产出不是合法 JSON（{exc}）")
                continue
            if round_tripped != expected[name]:
                mismatches.append(f"{name}: 往返结果与原值不一致")
        record(
            "合法用例：解析成功 + 产物是合法 JSON + 往返一致",
            not mismatches,
            f"{len(valid_names)} 个用例；问题：{mismatches or '无'}",
        )

        # --- 断言 3：目录创建逻辑（打桩 File/Folder）-------------------------
        fs_lines = [l for l in lines if l.startswith("FSCASE ")]
        fs_bad = [l for l in fs_lines if " PASS " not in l]
        fs_total = next((l for l in lines if l.startswith("FS_TOTAL=")), "")
        record(
            "目录创建：逐级创建 / 失败退回兜底 / 空路径不建目录",
            bool(fs_lines) and not fs_bad,
            f"{fs_total}；未通过：{fs_bad or '无'}",
        )

        # --- 断言 4：非法输入必须抛错 ----------------------------------------
        # 注意 not not_threw：直接把列表当真值用会让"空列表=无问题"被判成失败
        # （本项目已经有两次栽在"假值"上了，这里显式转成布尔）。
        not_threw: list[str] = []
        for name, _raw, why in INVALID_CASES:
            status = next((l for l in lines if l.startswith(f"INVALID {name} ")), "")
            if " THREW" not in status:
                not_threw.append(f"{name}（{why}）")
        record(
            "非法输入必须抛错，而不是静默返回",
            not not_threw,
            f"{len(INVALID_CASES)} 个用例；未抛错：{not_threw or '无'}",
        )

    print("\n" + "=" * 90)
    failed = [name for name, ok, _ in _results if not ok]
    print(f"ES3 引擎实测完成：{len(_results) - len(failed)}/{len(_results)} 项通过")
    if failed:
        print("\n未通过：")
        for name in failed:
            print(f"  ✗ {name}")
        return 1
    print("全部通过。jsx 内嵌的 JSON 实现在 ES3 引擎里可以正常工作。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
