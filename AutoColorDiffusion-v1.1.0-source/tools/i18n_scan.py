# -*- coding: utf-8 -*-
"""界面文案门禁：英文表是否完整、有没有"该翻却没翻"的中文。

三件事：
  1. **缺失条目**：代码里 `T("…")` / `TF("…", …)` 用到、但 `acb/i18n_catalog.py`
     里没有的键 —— 英文模式下那句会原样显示中文（不会崩，但用户看到半中半英）。
  2. **占位符不一致**：中文键里有 `{0}` / `{name}`，英文值里的占位符集合必须一致，
     否则 `TF(...)` 在英文模式下会抛 `KeyError` / `IndexError`（中文模式看不见！）。
  3. **未包装的文本出口**：处在"给人看的出口位置"（日志/控件/弹窗/print/异常）
     却仍然是中文裸字面量的地方 —— 这类是**漏翻**，不是"故意不翻"。
     故意不翻的（AI 提示词、风格规则、正则片段、字段名）不在出口位置，因此不会被误报。

用法：
    python tools/i18n_scan.py                 # 门禁（有问题时退出码 1）
    python tools/i18n_scan.py --keys          # 打印全部需要的键（写英文表用）
    python tools/i18n_scan.py --missing       # 只打印缺失的键
    python tools/i18n_scan.py --unwrapped     # 只打印未包装的出口文本
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 这些文件整体不参与翻译（理由见 acb/i18n.py 的模块说明）。
SKIP_FILES = {
    "acb/ai/prompts.py",       # AI 提示词：发给模型，翻译会改变行为
    "acb/i18n.py",             # 本机制自身
    "acb/i18n_catalog.py",     # 英文文案表（内容就是英文）
    "tools/i18n_scan.py",
}

LOG_LEVELS = {"debug", "info", "warning", "warn", "error", "critical", "exception"}
QT_TEXT_METHODS = {
    "setText", "setWindowTitle", "setToolTip", "setStatusTip", "setWhatsThis",
    "setPlaceholderText", "addItem", "insertItem", "addAction", "insertAction",
    "addMenu", "addButton", "showMessage", "setDetailedText", "setInformativeText",
    "addTab", "insertTab", "setTabText", "setLabelText", "setTitle", "setNote",
    "setSubTitle", "setCaption", "addItems",
}
CTOR_TEXT = {
    "QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox", "QAction",
    "QTableWidgetItem", "QListWidgetItem", "QToolButton", "QMenu", "QWidget",
}
MSGBOX_STATICS = {"information", "warning", "critical", "question", "about"}
INPUT_DIALOG = {"getText", "getItem", "getMultiLineText", "getDouble", "getInt"}
CALLBACK_SINKS = {
    "info", "warn", "warning", "error", "item_status", "note", "stage", "progress",
    "message", "detail", "log", "emit_log",
}
TEXT_KEYWORD_ARGS = {
    "text", "label", "title", "tooltip", "help", "description", "placeholder",
    "status_tip", "informative_text", "detailed_text", "caption", "subtitle", "msg",
}

PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")

# 翻译入口 → "模板参数"在参数列表里的下标。
# T_in()/TF_in() 的第一个参数是**目标语言**，模板在第二位 —— 它们只用于
# "重启后生效"那个对话框（必须用目标语言显示，而那一刻进程的语言还是旧的）。
WRAPPERS: dict[str, int] = {"T": 0, "TF": 0, "T_in": 1, "TF_in": 1}


def has_cjk(text: object) -> bool:
    return isinstance(text, str) and any("\u4e00" <= ch <= "\u9fff" for ch in text)


def source_files() -> list[Path]:
    files = sorted((ROOT / "acb").rglob("*.py")) + [ROOT / "app.py"]
    out = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if rel in SKIP_FILES:
            continue
        out.append(path)
    return out


def parents_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    out: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def call_name(call: ast.Call) -> tuple[str | None, str | None]:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id, None
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            return func.attr, base.id
        if isinstance(base, ast.Attribute):
            return func.attr, base.attr
        return func.attr, None
    return None, None


def is_sink(node: ast.AST, parents: dict[ast.AST, ast.AST], rel: str) -> bool:
    """这个字符串节点是不是"给人看的文本出口"？"""
    parent = parents.get(node)
    if parent is None:
        return False
    if isinstance(parent, ast.List):
        grand = parents.get(parent)
        if isinstance(grand, ast.Call):
            name, _base = call_name(grand)
            return name in QT_TEXT_METHODS and parent in grand.args
        return False
    if not isinstance(parent, ast.Call):
        return False
    name, _base = call_name(parent)
    if name is None:
        return False
    position = parent.args.index(node) if node in parent.args else None
    if name in LOG_LEVELS and position == 0:
        return True
    if name in QT_TEXT_METHODS and position == 0:
        return True
    if name in CTOR_TEXT and position == 0:
        return True
    if name in MSGBOX_STATICS and position is not None and position >= 1:
        return True
    if name in INPUT_DIALOG and position in (2, 3):
        return True
    if name in CALLBACK_SINKS and position is not None:
        return True
    if name == "print" and position is not None:
        return True
    if (name.endswith("Error") or name.endswith("Exception")) and position == 0:
        return True
    if rel.startswith(("acb/ui/", "app.py")):
        for kw in parent.keywords:
            if kw.value is node and kw.arg in TEXT_KEYWORD_ARGS:
                return True
    return False


def docstring_nodes(tree: ast.AST) -> set[ast.AST]:
    found: set[ast.AST] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(body[0].value)
    return found


def required_keys() -> dict[str, list[str]]:
    """代码里 T()/TF()/T_in()/TF_in() 用到的键 → 出现位置（file:line）。"""
    keys: dict[str, list[str]] = {}
    for path in source_files():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name, _base = call_name(node)
            if name not in WRAPPERS:
                continue
            index = WRAPPERS[name]
            if len(node.args) <= index:
                continue
            first = node.args[index]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.setdefault(first.value, []).append(f"{rel}:{node.lineno}")
    return keys


def unwrapped_sink_texts() -> list[str]:
    """处在文本出口却仍是中文裸字面量的位置（漏翻）。"""
    found: list[str] = []
    for path in source_files():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = parents_map(tree)
        docstrings = docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and has_cjk(node.value) and node not in docstrings:
                if isinstance(parents.get(node), ast.JoinedStr):
                    continue
                if is_sink(node, parents, rel):
                    found.append(f"{rel}:{node.lineno}: {node.value[:48]!r}")
            elif isinstance(node, ast.JoinedStr) and is_sink(node, parents, rel):
                joined = "".join(
                    v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
                if has_cjk(joined):
                    found.append(f"{rel}:{node.lineno}: f\"{joined[:48]}\"")
    return found


def placeholders(text: str) -> set[str]:
    return {match.group(1) for match in PLACEHOLDER_RE.finditer(text)}


def check_catalog() -> tuple[list[str], list[str], list[str]]:
    """返回 (缺失键, 占位符不一致, 未包装出口文本)。"""
    from acb.i18n_catalog import EN

    required = required_keys()
    # 只要求"含中文的键"有英文条目：像 `"{0} @ {1}"`、`"color: {0};"` 这种
    # 本来就没有中文的模板，翻译后与原文一样（T() 查不到就原样返回），
    # 强制给它们写一条同值条目只是噪声。
    missing = sorted(key for key in required if key not in EN and has_cjk(key))
    mismatch: list[str] = []
    for key, value in EN.items():
        if not placeholders(key):
            continue
        if placeholders(key) != placeholders(value):
            mismatch.append(
                f"{key!r} → {value!r}"
                f"（zh={sorted(placeholders(key))} en={sorted(placeholders(value))}）"
            )
    return missing, sorted(mismatch), unwrapped_sink_texts()


def main() -> int:
    parser = argparse.ArgumentParser(description="界面文案门禁（英文表完整性 / 漏翻检查）")
    parser.add_argument("--keys", action="store_true", help="打印全部需要的键")
    parser.add_argument("--missing", action="store_true", help="只打印缺失的键")
    parser.add_argument("--unwrapped", action="store_true", help="只打印未包装的出口文本")
    parser.add_argument("--dump-json", type=Path, default=None,
                        help="把「需要的键 / 未包装 / 占位符不一致」写成 JSON（供工具链读取）")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    if args.dump_json is not None:
        import json

        missing_keys, mismatch_keys, unwrapped_items = check_catalog()
        # 按文件分组（按行号排序）：写英文译文时"看到上下文"比看字母序准得多。
        by_file: dict[str, list[str]] = {}
        for key, locations in required_keys().items():
            for location in locations:
                rel = location.rsplit(":", 1)[0]
                by_file.setdefault(rel, []).append(f"{location.rsplit(':', 1)[1]}\t{key}")
        args.dump_json.write_text(
            json.dumps(
                {
                    "required": sorted(required_keys()),
                    "required_by_file": by_file,
                    "missing": missing_keys,
                    "placeholder_mismatch": mismatch_keys,
                    "unwrapped": unwrapped_items,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
            newline="",
        )
        print(f"已写出 {args.dump_json}")
        return 0

    if args.keys:
        for key in sorted(required_keys()):
            print(key)
        return 0

    missing, mismatch, unwrapped = check_catalog()
    if args.missing:
        print("\n".join(missing))
        return 0
    if args.unwrapped:
        print("\n".join(unwrapped))
        return 0

    print(f"需要的键 {len(required_keys())} 个；缺失 {len(missing)} 个；"
          f"占位符不一致 {len(mismatch)} 个；未包装的出口文本 {len(unwrapped)} 处")
    for label, items in (("缺失条目", missing), ("占位符不一致", mismatch),
                         ("未包装（漏翻）", unwrapped)):
        if items:
            print(f"\n--- {label} ---")
            for item in items[:200]:
                print(item)
            if len(items) > 200:
                print(f"...（共 {len(items)} 条，只列前 200）")
    ok = not missing and not mismatch and not unwrapped
    print("\n门禁结果：" + ("通过" if ok else "未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
