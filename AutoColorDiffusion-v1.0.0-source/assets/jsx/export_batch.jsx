// ============================================================================
// 【给下一个人：VS Code 里那条「应为 ";"。ts(1005)」是误报，不是缺陷】
//   本文件是 **ExtendScript**（Photoshop 的脚本引擎，ECMAScript 3），不是 React JSX。
//   VS Code 内置的 TS/JS 语言服务把 .jsx 按 JSX 解析，而下面的
//   `#target photoshop` 是 ExtendScript 的**预处理器指令**，不是 JS 语法 ——
//   于是它在那一行报「应为 ";"。ts(1005)」。
//   处理方式：**关掉编辑器的 JS 语法校验**（见仓库根目录 .vscode/settings.json
//   里的 javascript.validate.enable=false），语法高亮不受影响。
//   ⚠ 不要为了"让编辑器不报错"而删掉 `#target photoshop`：
//   它是在双击 .jsx（或用 ExtendScript Toolkit 直接运行）时，唯一告诉引擎
//   "目标是 Photoshop" 的东西。本程序自己的两种调用方式虽然用不到它，
//   但用户很可能会双击试试。
// ============================================================================
// Auto Color Diffusion —— Photoshop 批量导出脚本
// ----------------------------------------------------------------------------
// 使用方式（三种任选）：
//   1. 程序通过 win32com.client 自动调用（推荐，无需手动操作）；
//   2. 双击同目录的 run_export.bat；
//   3. Photoshop 菜单「文件 → 脚本 → 浏览」选择本文件；
//      或命令行把本文件作为 Photoshop 的启动参数：Photoshop.exe "本文件路径"
//
// ============================================================================
// 【本脚本被谁、以什么方式调用】—— 完整的 app.DoJavaScriptFile 调用链
// ============================================================================
// Python 侧（acb/ps/photoshop.py::run_export_script）做的事：
//
//     import pythoncom, win32com.client
//     pythoncom.CoInitialize()                       # ← QThread 里必须先初始化 COM，
//                                                    #   否则抛 "CoInitialize has not been called"
//     app = win32com.client.Dispatch("Photoshop.Application")
//                                                    # ← PS 未运行会自动拉起；
//                                                    #   未安装则抛 0x80040154 (Class not registered)
//     app.DisplayDialogs = 3                         # ← psDisplayNoDialogs，抑制一切模态框。
//                                                    #   不设这一句，批处理会在某张图上弹窗并永久挂起
//     app.DoJavaScriptFile(str(jsx_path))            # ← 同步执行本文件，返回脚本的 return 值
//     pythoncom.CoUninitialize()
//
// 为什么用 DoJavaScriptFile 而不是 DoJavaScript：
//   DoJavaScript(script_string) 要把整段脚本塞进 COM 字符串参数，会同时踩到
//   转义（引号/反斜杠/中文）与参数长度上限两个坑；DoJavaScriptFile 只传一个路径，
//   脚本内容由 Photoshop 自己从磁盘读，既绕开转义，也让我们能单独编辑与调试本文件。
//
// 执行完成后，Photoshop 侧写出两个文件供 Python 读取与汇总：
//     ps_result.json   结构化结果 {total, ok, failed, fatal, results[]}
//     ps_log.txt       逐项的文字日志
//   Python 读取 ps_result.json 后会打印"成功 n/N"，并用 ps_log.txt 定位失败项。
//
// 【本脚本与它的产物放在哪里】—— 都在软件的运行目录里，不进出片目录
//   本脚本、manifest.json、run_export.bat 三者，连同下面两个记账文件，
//   全部放在同一次运行的目录里：<数据目录>/runs/<运行标识>/
//       manifest.json / export_batch.jsx / run_export.bat
//       ps_log.txt        ← 执行日志（哪一项失败、为什么）
//       ps_result.json    ← 逐项结果（Python 读它，也用于手动排查）
//   原因：出片目录是用户的交付目录，只该有 JPG。这五个是驱动导出的"机器件"，
//   混在里面既碍事、又会在打包交付时被一起发出去，而且源目录会被彻底搞乱。
//   文件名不必再带运行标识——每次运行本来就是独立目录
//   （见 Python 侧的 acb/ps/jsx_render.py::script_dir_for）。
//   注意：那个目录在程序退出时会被自动清理，所以手动补跑要在关闭程序前完成。
//   【兜底】若上面的绝对路径建不出来（例如整个脚本目录被拷到另一台机器执行），
//   脚本会退回"与本文件同目录"并使用同样的文件名
//   ps_log.txt / ps_result.json —— 宁可位置不理想，也不能不留痕迹。
//
// 降级（Photoshop 未安装 / COM 被禁用 / 授权窗口挂起）：
//   Python 侧会捕获异常，并提示你改用以下任一方式，而**不会**去伪造导出结果：
//     a. 直接双击同目录的 run_export.bat（脚本会自动查找 Photoshop.exe）；
//     b. 打开 Photoshop，用「文件 → 脚本 → 浏览」选择本文件；
//     c. 在 Adobe Camera Raw / Lightroom 里打开这批 RAW，用「同步设置」批量套用
//        旁侧 XMP，再用 ACR 自己的「存储图像」导出 JPG。
//   明确不做：用 rawpy/dcraw 复刻 ACR 渲染 —— ACR 的 PV5/PV6 管线是闭源的，
//   在色调曲线、相机配置文件、镜头 LCP 校正上不同源，产出会与你在 ACR 里看到的
//   不一致，那属于误导性降级，不如明确告知"请用 Photoshop 导出"。
//
// ============================================================================
// 【manifest.json 契约】—— 本脚本与 Python 之间唯一的接口
// ============================================================================
//   {
//     "version": 1,
//     "colorSpace": "Adobe RGB(1998)",          // 界面选择，仅作记录
//     "colorSpaceProfile": "Adobe RGB (1998)",  // 传给 doc.convertProfile 的 ICC 描述名
//     "psJpegQuality": 12,                       // ← 实际使用的 Photoshop 0–12 刻度
//     "psJpegQualityHint": "12 / 12　最高质量（Photoshop 上限）",  // 同值的可读说明，仅供日志
//     "libjpegEquivalent": 97,                   // 仅供参考的 libjpeg 体感对照，不参与编码
//     "items": [ { "raw": "D:\\a\\x.CR3", "out": "D:\\a\\x_E.jpg", "xmp": "..." } ]
//   }
// 质量值只有一个权威表示：Photoshop 的 0–12（界面滑块直接就是这个刻度）。
// libjpegEquivalent 只是给熟悉 libjpeg / Lightroom 的人做体感参照的，
// **不参与任何编码决策**。
// 早期版本这里是 libjpeg 语义的档位名加一张换算表，已废弃——PS 只有 13 档，
// 四档映射进去必然出现"两档产出完全相同的文件"，而且真实 q100 在 PS 里
// 根本无法表达（12 就是上限）。刻度定义见 acb/constants.py 的 PS_JPEG_QUALITY_MIN。
//
// 为什么需要这个脚本（回答问题 a）：
//   Camera Raw 没有官方 CLI，无法在命令行里批量套用 XMP 并导出。
//   唯一可行的自动化路径是 Photoshop 的脚本引擎（ExtendScript）。
//   本脚本由 Python 生成，读取同目录的 manifest.json 获得完整任务清单，
//   因此脚本本身是通用的、可重复运行的（manifest 变了，行为就变）。
//
// 【为什么脚本里自带一份 JSON 实现】
//   ExtendScript 只有 ECMAScript 3，**没有 JSON 对象**，直接写 JSON.parse
//   会抛"json 未定义"。脚本因此内嵌了一份最小实现（见文件中的
//   ACB_JSON_POLYFILL 区块），并在宿主自带 JSON 时优先用原生实现。
//   该区块被 tools/jsx_es3_test.py 抽出、放在真正的 ES3 引擎（Windows 的
//   JScript 5.8）里跑过，不是"看起来应该能行"。
//
// 关键设计：所有"计算"都在 Python 侧完成
//   输出文件名、去重后缀（_E_2、_E_3）、导出目录——全部由 Python 预先算好
//   写进 manifest.json。ExtendScript 里只做"打开 → 转色彩空间 → 存 JPG"。
//   原因：ExtendScript 的调试能力极差（没有断点、报错信息模糊），
//         把逻辑放在可测试的 Python 侧能显著降低出错率。
//
// ACR 前提条件（README 也会强调）：
//   Camera Raw 首选项必须勾选「将图像设置存储在侧车 .xmp 文件中」，
//   否则 Photoshop 打开 RAW 时不会读取我们写出的旁侧 XMP，导出结果不会有任何调整。
// ============================================================================

#target photoshop

(function () {
    // ---- 基础环境准备 -----------------------------------------------------
    // displayDialogs = NO 会抑制所有模态对话框（包括"是否保存"、"配置文件不匹配"等）。
    // 不设它的话批处理会在某一张上弹出对话框并永久挂起，这是最常见的事故原因。
    app.displayDialogs = DialogModes.NO;

    var scriptFile = new File($.fileName);
    var baseDir = scriptFile.parent;
    var manifestFile = new File(baseDir + "/manifest.json");

    // 日志与结果的落盘位置：**先设为兜底位置**（脚本自己所在的目录），
    // 等读到 manifest 再换成 Python 指定的路径。
    // 正常情况下两者就是同一个目录（都在 <数据目录>/runs/<运行标识>/），
    // 所以这一步几乎总是"原地不动"；先设兜底是为了万一 manifest 解析失败时
    // 我们仍然能留下日志。
    var logFile = new File(baseDir + "/ps_log.txt");
    var resultFile = new File(baseDir + "/ps_result.json");

    // ---- 日志工具 ---------------------------------------------------------
    var logLines = [];
    function log(message) {
        logLines.push(String(message));
    }

    function writeLog() {
        try {
            logFile.encoding = "UTF-8";
            logFile.open("w");
            logFile.write(logLines.join("\n"));
            logFile.close();
        } catch (e) {
            // 日志写不出去也不能让脚本失败。
        }
    }

    // ==== ACB_JSON_POLYFILL_BEGIN ====
    // ========================================================================
    // JSON 兼容层 —— ExtendScript 只有 ECMAScript 3，**没有内置 JSON 对象**
    // ========================================================================
    // 真实故障记录：早期这里直接写 JSON.parse(manifestText)，
    // 在 Photoshop 里必抛 "json 未定义"，脚本在读到任务清单之前就退出，
    // 用户看到的是「manifest.json 解析失败：json 未定义」，
    // 而真正的原因（引擎里根本没有 JSON）完全看不出来。
    // Windows 自带的 JScript 5.8 同属 ES3，也一样没有 JSON —— 可用来离线复现。
    //
    // 为什么不用 eval("(" + text + ")") 这个常见替代：
    //   1. eval 抛出的是宿主引擎的 SyntaxError，位置信息对用户毫无意义；
    //      自写解析器能报出"第 N 个字符处遇到什么"，这在本项目里很重要
    //      （manifest 可能是用户手工编辑过的）。
    //   2. JSON 文本若包含 U+2028 / U+2029（某些文件系统路径可能出现），
    //      在部分引擎里会把 eval 的输入拆成两行从而报语法错；解析器不受影响。
    //   3. 本项目的 exe 已经有杀软误报问题，脚本里出现 eval 会加剧误报。
    //
    // 全部代码刻意只用 ES3 语法：不用 Array.map/forEach/indexOf、
    // 不用 String.trim、不用 Object.keys、不用 let/const、不写尾随逗号。
    var AcbJSON = (function () {
        var WHITESPACE = " \t\n\r";
        var DIGITS = "0123456789";

        function makeError(message, at) {
            return new Error(message + "（第 " + (at + 1) + " 个字符处）");
        }

        function isDigit(ch) {
            return ch !== "" && DIGITS.indexOf(ch) !== -1;
        }

        function parse(text) {
            var source = String(text === null || text === undefined ? "" : text);

            // 去掉 UTF-8 BOM。带 BOM 的文件第一个字符不是 "{"，
            // 会被误报成"内容不是对象"，而用户完全看不出哪里有问题。
            // Python 侧写出时不带 BOM，但用户手工用记事本另存就可能带上。
            if (source.length > 0 && source.charCodeAt(0) === 0xFEFF) {
                source = source.substring(1);
            }

            var at = 0;
            var len = source.length;

            function skipWhitespace() {
                while (at < len && WHITESPACE.indexOf(source.charAt(at)) !== -1) {
                    at++;
                }
            }

            function parseString() {
                at++; // 跳过起始引号
                var out = "";
                while (true) {
                    if (at >= len) {
                        throw makeError("字符串没有结束引号", at);
                    }
                    var ch = source.charAt(at);
                    if (ch === '"') {
                        at++;
                        return out;
                    }
                    if (ch !== "\\") {
                        out += ch;
                        at++;
                        continue;
                    }
                    at++; // 跳过反斜杠
                    if (at >= len) {
                        throw makeError("转义序列不完整", at);
                    }
                    var esc = source.charAt(at);
                    at++;
                    if (esc === '"') { out += '"'; }
                    else if (esc === "\\") { out += "\\"; }
                    else if (esc === "/") { out += "/"; }
                    else if (esc === "b") { out += "\b"; }
                    else if (esc === "f") { out += "\f"; }
                    else if (esc === "n") { out += "\n"; }
                    else if (esc === "r") { out += "\r"; }
                    else if (esc === "t") { out += "\t"; }
                    else if (esc === "u") {
                        var hex = source.substr(at, 4);
                        if (hex.length < 4) {
                            throw makeError("\\u 转义不完整", at);
                        }
                        var code = parseInt(hex, 16);
                        if (isNaN(code)) {
                            throw makeError("\\u 转义不是合法十六进制：" + hex, at);
                        }
                        out += String.fromCharCode(code);
                        at += 4;
                    } else {
                        throw makeError("无法识别的转义字符 \\" + esc, at);
                    }
                }
            }

            function parseNumber() {
                var start = at;
                if (source.charAt(at) === "-") {
                    at++;
                }
                while (isDigit(source.charAt(at))) {
                    at++;
                }
                if (source.charAt(at) === ".") {
                    at++;
                    while (isDigit(source.charAt(at))) {
                        at++;
                    }
                }
                var exp = source.charAt(at);
                if (exp === "e" || exp === "E") {
                    at++;
                    var sign = source.charAt(at);
                    if (sign === "+" || sign === "-") {
                        at++;
                    }
                    while (isDigit(source.charAt(at))) {
                        at++;
                    }
                }
                var raw = source.substring(start, at);
                var value = Number(raw);
                if (isNaN(value)) {
                    throw makeError("不是合法数字：" + raw, start);
                }
                return value;
            }

            function parseObject() {
                at++; // 跳过 '{'
                var obj = {};
                skipWhitespace();
                if (source.charAt(at) === "}") {
                    at++;
                    return obj;
                }
                while (true) {
                    skipWhitespace();
                    if (source.charAt(at) !== '"') {
                        throw makeError("对象的键必须是双引号字符串", at);
                    }
                    var key = parseString();
                    skipWhitespace();
                    if (source.charAt(at) !== ":") {
                        throw makeError("键 " + key + " 后面缺少冒号", at);
                    }
                    at++;
                    obj[key] = parseValue();
                    skipWhitespace();
                    var sep = source.charAt(at);
                    if (sep === ",") {
                        at++;
                        continue;
                    }
                    if (sep === "}") {
                        at++;
                        return obj;
                    }
                    throw makeError("对象里缺少逗号或右花括号", at);
                }
            }

            function parseArray() {
                at++; // 跳过 '['
                var arr = [];
                skipWhitespace();
                if (source.charAt(at) === "]") {
                    at++;
                    return arr;
                }
                while (true) {
                    arr.push(parseValue());
                    skipWhitespace();
                    var sep = source.charAt(at);
                    if (sep === ",") {
                        at++;
                        continue;
                    }
                    if (sep === "]") {
                        at++;
                        return arr;
                    }
                    throw makeError("数组里缺少逗号或右方括号", at);
                }
            }

            function parseValue() {
                skipWhitespace();
                if (at >= len) {
                    throw makeError("内容意外结束", at);
                }
                var ch = source.charAt(at);
                if (ch === "{") { return parseObject(); }
                if (ch === "[") { return parseArray(); }
                if (ch === '"') { return parseString(); }
                if (ch === "-" || isDigit(ch)) { return parseNumber(); }
                if (source.substr(at, 4) === "true") { at += 4; return true; }
                if (source.substr(at, 5) === "false") { at += 5; return false; }
                if (source.substr(at, 4) === "null") { at += 4; return null; }
                throw makeError("无法识别的取值，起始字符是 " + ch, at);
            }

            var value = parseValue();
            skipWhitespace();
            if (at < len) {
                throw makeError("解析完成后仍有多余内容", at);
            }
            return value;
        }

        function quote(text) {
            var out = '"';
            for (var i = 0; i < text.length; i++) {
                var ch = text.charAt(i);
                var code = text.charCodeAt(i);
                if (ch === '"') { out += '\\"'; }
                else if (ch === "\\") { out += "\\\\"; }
                else if (ch === "\n") { out += "\\n"; }
                else if (ch === "\r") { out += "\\r"; }
                else if (ch === "\t") { out += "\\t"; }
                else if (ch === "\b") { out += "\\b"; }
                else if (ch === "\f") { out += "\\f"; }
                else if (code < 0x20) {
                    // 其余控制字符必须转义，否则产出的不是合法 JSON。
                    var hex = code.toString(16);
                    while (hex.length < 4) {
                        hex = "0" + hex;
                    }
                    out += "\\u" + hex;
                } else {
                    out += ch;
                }
            }
            return out + '"';
        }

        // 只需要覆盖本脚本实际写出的结构：对象/数组/字符串/数字/布尔/null。
        // 不做循环引用检测——结果对象是手工构造的，不可能成环。
        function stringify(value) {
            if (value === null || value === undefined) {
                return "null";
            }
            var type = typeof value;
            if (type === "string") { return quote(value); }
            if (type === "number") { return isFinite(value) ? String(value) : "null"; }
            if (type === "boolean") { return value ? "true" : "false"; }
            if (value instanceof Array) {
                var parts = [];
                for (var i = 0; i < value.length; i++) {
                    parts.push(stringify(value[i]));
                }
                return "[" + parts.join(",") + "]";
            }
            if (type === "object") {
                var members = [];
                for (var key in value) {
                    if (!value.hasOwnProperty(key)) { continue; }
                    if (typeof value[key] === "undefined") { continue; }
                    members.push(quote(key) + ":" + stringify(value[key]));
                }
                return "{" + members.join(",") + "}";
            }
            return "null";
        }

        return { parse: parse, stringify: stringify };
    })();

    // 优先使用宿主自带的 JSON（万一将来 Photoshop 换了引擎并提供原生实现），
    // 否则回退到上面的实现。注意这里必须同时检查 parse 与 stringify：
    // 只提供其中一个的宿主会让另一半静默失效。
    var JsonCodec = (typeof JSON !== "undefined" && JSON !== null &&
                     typeof JSON.parse === "function" &&
                     typeof JSON.stringify === "function") ? JSON : AcbJSON;
    // ==== ACB_JSON_POLYFILL_END ====

    // ---- 结果写回工具 -----------------------------------------------------
    // 统一成一个函数，是为了让"致命错误提前退出"这条路径也能留下 ps_result.json。
    // 返回空串表示成功，否则返回错误信息（由调用方记入日志）。
    function writeResult(total, okCount, failCount, results, fatalError) {
        try {
            resultFile.encoding = "UTF-8";
            resultFile.open("w");
            resultFile.write(JsonCodec.stringify({
                total: total,
                ok: okCount,
                failed: failCount,
                fatal: fatalError || "",
                results: results
            }));
            resultFile.close();
            return "";
        } catch (e) {
            return (e && e.message) ? e.message : String(e);
        }
    }

    // ---- 读取 manifest ----------------------------------------------------
    if (!manifestFile.exists) {
        alert("找不到 manifest.json。\n请先用 Auto Color Diffusion 生成脚本产物，" +
              "或确认本脚本与 manifest.json 在同一目录。");
        return;
    }

    manifestFile.encoding = "UTF-8";
    // 读取也要包起来：manifest 被别的程序占用 / 没有读权限时，
    // open/read 会直接抛错 —— 那样整个 IIFE 就地终止，ps_result.json 一个字都不写，
    // Python 侧只能含糊地报"结果未知"（这正是下面解析失败那段想避开的情况）。
    var manifestText;
    try {
        manifestFile.open("r");
        manifestText = manifestFile.read();
        manifestFile.close();
    } catch (readError) {
        var readReason = (readError && readError.message) ? readError.message : String(readError);
        log("[致命] 无法读取 manifest.json：" + readReason);
        var readWriteError = writeResult(0, 0, 0, [], "无法读取 manifest.json：" + readReason);
        if (readWriteError) {
            log("[警告] 写入 ps_result.json 失败：" + readWriteError);
        }
        writeLog();
        alert("无法读取 manifest.json：\n" + readReason);
        return;
    }

    var manifest;
    try {
        manifest = JsonCodec.parse(manifestText);
    } catch (e) {
        var reason = (e && e.message) ? e.message : String(e);
        // 解析失败是致命错误：把原因**同时**落到 ps_log.txt 与 ps_result.json，
        // 因为自动化调用时 alert 可能被抑制、也可能让整批挂住，
        // 而这两个文件 Python 一定会读、用户也一定能看到。
        log("[致命] manifest.json 解析失败：" + reason);
        var fatalWriteError = writeResult(0, 0, 0, [], "manifest.json 解析失败：" + reason);
        if (fatalWriteError) {
            log("[警告] 写入 ps_result.json 失败：" + fatalWriteError);
        }
        writeLog();
        alert(
            "manifest.json 解析失败：" + reason + "\n\n" +
            "常见原因：\n" +
            "  1. manifest.json 与 export_batch.jsx 不是同一次生成的（请成对复制）；\n" +
            "  2. 用记事本等工具编辑过 manifest.json，把引号或逗号改坏了；\n" +
            "  3. 文件被另存成了 UTF-16 或带了 BOM（本脚本按 UTF-8 读取）；\n" +
            "  4. 文件被清空或只写了一半（例如磁盘满、复制中断）。\n\n" +
            "文件开头 200 个字符如下，可据此判断：\n" +
            manifestText.substring(0, 200)
        );
        return;
    }

    // ---- 切换到 manifest 指定的落盘位置 -----------------------------------
    // 日志与结果与本脚本同一个运行目录（见文件头注释），不在出片目录里。
    //
    // 但**必须容错**：manifest 被拷到另一台机器上执行时，那些绝对路径可能
    // 建不出来（用户名不同、盘符不同、无写权限）。此时保持上面的兜底位置，
    // 保证"至少还能留下日志"，而不是因为写不了日志就让整个导出失败。
    // ==== ACB_FS_HELPERS_BEGIN ====
    // 逐级向上创建缺失的目录。
    //
    // 【为什么不能只调一次 parent.create()】
    //   ExtendScript 的 Folder.create() **不会创建中间层级**。
    //   若 <数据目录>\runs 不存在（用户手工删过数据目录，或程序从未跑过导出），
    //   直接创建 <运行标识> 子目录会失败 —— 而且失败是静默的：
    //   我们只是退回兜底位置，用户什么提示都看不到。
    //   所以必须自己从上层往下逐级创建。
    //
    // 这段逻辑被 tools/jsx_es3_test.py 用打桩的 File/Folder 单独测过
    // （含"父目录不存在"与"创建失败"两条分支）。
    function ensureFolder(folder) {
        if (!folder || folder.exists) {
            return true;
        }
        var parent = null;
        try {
            parent = folder.parent;
        } catch (eParent) {
            parent = null;
        }
        // 到达根目录仍创建不了就认输；同时防止 parent 指向自身导致无限递归。
        if (!parent || parent.fsName === folder.fsName) {
            try {
                return folder.create() || folder.exists;
            } catch (eRoot) {
                return false;
            }
        }
        if (!ensureFolder(parent)) {
            return false;
        }
        try {
            return folder.create() || folder.exists;
        } catch (eCreate) {
            return false;
        }
    }

    // 优先用 manifest 指定的路径；建不出来就退回兜底位置。
    function preferTarget(preferredPath, fallbackFile) {
        if (!preferredPath) {
            return fallbackFile;
        }
        try {
            var target = new File(preferredPath);
            if (ensureFolder(target.parent)) {
                return target;
            }
        } catch (eTarget) {
            // 落到下面的兜底
        }
        return fallbackFile;
    }
    // ==== ACB_FS_HELPERS_END ====

    logFile = preferTarget(manifest.logPath, logFile);
    resultFile = preferTarget(manifest.resultPath, resultFile);
    // 把实际落盘位置写进日志：用户拿到的是 JPG，日志在哪必须自己说清楚，
    // 否则出问题时他根本找不到线索。
    log("日志文件：" + logFile.fsName);
    log("结果文件：" + resultFile.fsName);

    var items = manifest.items || [];

    // ---- 保存选项（JPG / PNG）---------------------------------------------
    // 格式由 manifest.exportFormat 决定。Python 侧已经按格式算好了每一项的
    // 输出扩展名，所以这里的 SaveOptions 类型必须与之匹配 ——
    // Photoshop 的 saveAs 是按 SaveOptions 类型决定写什么格式的，
    // 类型与扩展名不一致会写出"名字是 .png、内容其实是 JPEG"的文件。
    //
    // 注意：Photoshop 的 JPEGSaveOptions.quality 取值范围是 0–12（不是 0–100），
    // 而且 **12 就是上限** —— 真实的 libjpeg q100 在 Photoshop 里无法表达。
    // Python 侧把滑块值**原样**写进 manifest.psJpegQuality，不做任何换算，
    // 刻度定义见 acb/constants.py 的 PS_JPEG_QUALITY_MIN 注释。
    function makeJpegOptions(quality) {
        var opts = new JPEGSaveOptions();
        opts.quality = quality;
        // 嵌入色彩配置文件：这是硬约束 #2 的一部分——
        // 导出图必须带上色彩空间声明，否则用户在别的软件里看到的是错色。
        opts.embedColorProfile = true;
        // 格式选项：standard 是标准基线 JPEG，兼容性最好。
        opts.formatOptions = FormatOptions.STANDARDBASELINE;
        // 扫描线顺序：三遍扫描会让某些老软件读取异常，这里用单遍。
        opts.scans = 3;
        return opts;
    }

    // PNG：无损。compression 是 0–9 的压缩级别，**只影响文件体积、不影响画质**；
    // 6 是常见的速度/体积折中。
    function makePngOptions() {
        var opts = new PNGSaveOptions();
        try {
            opts.compression = 6;
        } catch (eComp) {
            log("[警告] 设置 PNG 压缩级别失败，将使用 Photoshop 默认值：" + eComp.message);
        }
        try {
            opts.interlaced = false;          // 隔行扫描会让部分软件读取变慢
        } catch (eInter) {
            // 同上，非关键属性
        }
        return opts;
    }

    // 按 manifest 的格式挑 SaveOptions。未知格式回退到 JPG
    // （Python 侧已经对未知格式发过警告，这里不再重复刷屏）。
    //
    // 【没有 TIFF】实测 Photoshop 2026 的脚本 DOM 里没有 TIFFSaveOptions，
    // 无法脚本化导出 TIFF（doc.saveAs 不带 options 会存成 PSD；
    // ActionManager 的 executeAction('save', As=TIFF) 会弹模态框挂死）。
    // 所以 Python 侧的格式清单里已经没有 TIFF，这里也不需要对应分支；
    // 万一 manifests 里写着 TIFF，会走下面的兜底（按 JPG 存）并由 Python 侧告警。
    function makeSaveOptions(format, quality) {
        var name = String(format || "JPG").toUpperCase();
        if (name === "PNG") {
            return makePngOptions();
        }
        return makeJpegOptions(quality);
    }

    // ---- 主循环 -----------------------------------------------------------
    var okCount = 0;
    var failCount = 0;
    var results = [];

    log("=== Auto Color Diffusion 批量导出开始 ===");
    log("清单项数：" + items.length);
    log("目标色彩空间：" + (manifest.colorSpaceProfile || "(不转换)"));
    log("导出格式：" + (manifest.exportFormat || "JPG") +
        (manifest.lossless ? "（无损，画质设置不参与）" : ""));
    if (!manifest.lossless) {
        log("JPEG 质量刻度（PS 0-12）：" + manifest.psJpegQuality);
    }

    for (var i = 0; i < items.length; i++) {
        var item = items[i];
        var rawPath = item.raw;
        var outPath = item.out;
        var doc = null;

        try {
            var rawFile = new File(rawPath);
            if (!rawFile.exists) {
                throw new Error("源文件不存在：" + rawPath);
            }

            // 输出目录必须预先存在，Photoshop 的 saveAs 不会自动创建目录。
            // 必须走 ensureFolder，不能写 outFolder.create()：
            // Folder.create() **不创建中间层级**，当出片目录是多级路径
            // （例如 <源目录>/_export/ 且 _export 还不存在）时会静默失败，
            // 随后 saveAs 抛出的错误信息完全指不到真正的原因。
            var outFile = new File(outPath);
            if (!ensureFolder(outFile.parent)) {
                throw new Error("无法创建输出目录：" + outFile.parent.fsName);
            }

            // ---- 打开 RAW ----
            // Photoshop 打开 RAW 时会自动启动 Camera Raw 并读取该文件的设置
            // （旁侧 XMP 或 DNG 内嵌 XMP）。因此这里不需要手动 apply XMP。
            // 若 ACR 首选项没有开启「将图像设置存储在侧车 .xmp 文件中」，
            // 打开的就是相机的原始默认设置，调整不会生效——README 已强调这一点。
            doc = app.open(rawFile);

            // ---- 色彩空间转换 ----
            // convertProfile(目标配置文件, 渲染意图, 黑场补偿, 使用仿色)
            //   意图用 RELATIVECOLORIMETRIC（相对色度）：这是"精确复现色彩"的
            //   标准选择，与缩略图阶段刻意使用 PERCEPTUAL 不同——
            //   缩略图是给 AI 判断观感用的，出片是交付用的，二者目标不同。
            if (manifest.colorSpaceProfile) {
                try {
                    doc.convertProfile(
                        manifest.colorSpaceProfile,
                        Intent.RELATIVECOLORIMETRIC,
                        true,
                        true
                    );
                } catch (convErr) {
                    // 目标 ICC 未安装是很常见的情况（尤其是 Display P3）。
                    // 此时**不中断**，按当前色彩空间导出，并在日志中明确记录，
                    // 让用户知道产出与预期色彩空间不符。
                    log("[警告] 第 " + (i + 1) + " 项色彩空间转换失败（目标配置文件可能未安装：" +
                        manifest.colorSpaceProfile + "）：" + convErr.message);
                }
            }

            // ---- 导出（格式由 manifest.exportFormat 决定）----
            doc.saveAs(
                outFile,
                makeSaveOptions(manifest.exportFormat, manifest.psJpegQuality),
                true
            );
            okCount++;
            results.push({ raw: rawPath, out: outPath, ok: true, error: "" });
            log("[成功] " + rawPath + "  ->  " + outPath);

        } catch (err) {
            failCount++;
            var message = (err && err.message) ? err.message : String(err);
            results.push({ raw: rawPath, out: outPath, ok: false, error: message });
            log("[失败] " + rawPath + " ：" + message);
        } finally {
            // 无论成败都要关闭文档，否则批处理很快会耗尽内存
            // （RAW 文档动辄数百 MB，开着十几个就会爆）。
            if (doc !== null) {
                try {
                    // DONOTSAVECHANGES：不保存对文档的改动，因为我们已有 JPG 产出，
                    // 也不希望覆盖用户的 RAW 或写出额外文件。
                    doc.close(SaveOptions.DONOTSAVECHANGES);
                } catch (closeErr) {
                    log("[警告] 关闭文档失败：" + closeErr.message);
                }
            }
        }
    }

    log("=== 完成：成功 " + okCount + " 项，失败 " + failCount + " 项 ===");

    // ---- 写回结构化结果，供 Python 侧汇总 ---------------------------------
    // 顺序很重要：必须先写结果、再写日志。
    // 早期版本是先 writeLog() 再写结果，而写结果又用 try/catch 静默吞异常，
    // 于是"结果文件根本没写出来"这件事在 ps_log.txt 里一个字都没有，
    // Python 侧只能含糊地报"结果未知"。
    var writeError = writeResult(items.length, okCount, failCount, results, "");
    if (writeError) {
        log("[警告] 写入 ps_result.json 失败：" + writeError);
    }

    if (failCount > 0) {
        // 用 alert 会让自动化流程挂住，因此这里只写日志不弹窗。
        // ⚠ 必须在 writeLog() **之前**：writeLog() 已经把缓冲区落盘了，
        //   写完之后再 log() 的行永远进不了 ps_log.txt（失败清单反而看不到）。
        //   批量用时把这条行放在第一行：失败清单几十条，写日志前先写它才不会被淹。
        log("存在失败项，详见 ps_log.txt；失败 " + failCount + " / " + items.length);
    }

    writeLog();
})();
