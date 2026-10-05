"""从日志里统计 token 用量：每轮 + 按模型汇总（每张照片的输入/输出 token）。

【为什么要做成工具（用户 2026-09-24）】
他问「帮我从日志中统计一下平均每张照片的输入和输出 token」——
这说明「花了多少钱」这件事他要能自己随时查，而不是每次让助手现写一段脚本。

数据来源与口径（都是日志里已有的行，不联网、不读密钥）：
  · `Token 用量：输入 N token，输出 M token` —— 每轮结束由 output_mode 写入
    （来自客户端记账，即服务端返回的 usage.prompt_tokens / completion_tokens，**含重试**）；
  · `完成 X 张，失败 Y 张，跳过 Z 张（共 T 张）` —— 本轮结果；
  · `写完失败`（`写 XMP 失败（累计`）与 `分析失败（累计`）—— 用来把分母算对：
    写回失败的图**已经拿到 AI 结果、token 已经花了**，必须算进分母；
    分析失败的（429/超时）基本没产生 token，不能算进分母，否则把平均值拉低。
  · 训练轮：日志里的样本数（`训练样本 N 个，满足`）当分母。

用法：
  python tools/token_stats.py                 # 读 <数据目录>/logs 下全部日志
  python tools/token_stats.py --log-dir <目录>
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acb.paths import data_root  # noqa: E402

TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
# 输出轮的用量行是 `Token 用量：输入 …`，训练轮是 `训练用量：输入 …`
# （train_mode 把同一份 usage_summary 挂在自己的前缀后面）—— 两种都得认，
# 否则“训练那一发”在统计里永远是空的（而它正是单次最贵的一发）。
USAGE_RE = re.compile(r"(?:Token 用量|训练用量)：输入 (\d+) token，输出 (\d+) token")
# 真实日志是 `模型能力：deepseek-flash [deepseek-flash] @ https://…；视觉=支持`，
# 即 `display = f"{label} [{model}]"`。要的是**方括号里的模型 id**：
# 服务商备注名可能带空格（如“通义千问 (Qwen)”），用 \S+ 抓会整条匹配不上。
MODEL_RE = re.compile(r"模型能力：.*?\[([^\]]+)\]")
START_RE = re.compile(r"开始(输出|训练)模式")
SUMMARY_RE = re.compile(r"完成 (\d+) 张，失败 (\d+) 张，跳过 (\d+) 张（共 (\d+) 张）")
REQUEST_RE = re.compile(r"acb\.ai\.client: 请求 http")
# 只写成 `训练样本 N 个，满足建议下限（10）` 会漏掉**样本不足**那一轮
# （那轮打的是另一句话），而样本不足的训练同样在花 token。
SAMPLES_RE = re.compile(r"(?:训练样本 (\d+) 个|当前 (\d+) 个样本)")
STYLE_RE = re.compile(r"风格=(\S+)")
# `{文件名} 写 XMP 失败（累计 N 次）：…` / `{文件名} 分析失败（累计 N 次）：…`
# 抓文件名用于去重（同一次失败会同时走 log.error 与 callbacks.error）。
WRITE_FAIL_RE = re.compile(r"(\S+?) 写 XMP 失败（累计")
ANALYSIS_FAIL_RE = re.compile(r"(\S+?) 分析失败（累计")


def parse_logs(log_dir: Path) -> list[dict]:
    """把日志切成「一轮」并抽出用量/规模/风格。

    只统计**在单个文件内闭合**的轮次：日志按大小轮转时，一轮的起始行可能落在
    `app-<日期>.1.log` 而用量行落在 `app-<日期>.log`，这种跨文件的轮次直接不计
    （宁可少算一轮，也不要把两轮的数字拼到一行上）。
    """
    runs: list[dict] = []
    cur: dict | None = None
    for logfile in sorted(log_dir.glob("app-*.log")):
        cur = None
        stamp = ""
        for line in logfile.read_text(encoding="utf-8", errors="replace").splitlines():
            if (hit := TS_RE.match(line)):
                stamp = hit.group(1)
            if START_RE.search(line) and line.startswith(stamp):
                cur = {
                    "date": stamp[:10], "time": stamp[11:16],
                    "mode": "训练" if "训练模式" in line else "输出",
                    "model": "?", "style": "?", "usage": None, "requests": 0,
                    "done": 0, "failed": 0, "skipped": 0,
                    "write_fail": 0, "analysis_fail": 0, "samples": 0,
                    "write_fail_files": set(), "analysis_fail_files": set(),
                }
                runs.append(cur)
                continue
            if cur is None:
                continue
            if (m := MODEL_RE.search(line)):
                cur["model"] = m.group(1)
            if (m := USAGE_RE.search(line)):
                cur["usage"] = (int(m.group(1)), int(m.group(2)))
            if REQUEST_RE.search(line):
                cur["requests"] += 1
            if (m := SUMMARY_RE.search(line)):
                cur["done"], cur["failed"], cur["skipped"] = (int(m.group(k)) for k in (1, 2, 3))
            if (m := SAMPLES_RE.search(line)):
                cur["samples"] = int(m.group(1) or m.group(2))
            # 这两条失败行走的是不同的 logger：
            #   写 XMP 失败 = acb.output_mode 的 log.error + callbacks.error（→ acb.ui.worker）
            #   分析失败   = callbacks.error → acb.ui.worker
            # 所以**不能**给它们统一加模块名前缀条件（加了之后"分析败"永远是 0）；
            # 又因为同一次失败会被写两行（app 侧故意的双写），
            # 分母必须**按文件名去重**，否则会把平均值算小。
            if (m := WRITE_FAIL_RE.search(line)):
                cur["write_fail_files"].add(m.group(1))
            if (m := ANALYSIS_FAIL_RE.search(line)):
                cur["analysis_fail_files"].add(m.group(1))
            if (m := STYLE_RE.search(line)) and cur["style"] == "?":
                cur["style"] = m.group(1)
    return runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从日志统计每张照片的输入/输出 token")
    parser.add_argument("--log-dir", default=None, help="日志目录（默认 <数据目录>/logs）")
    args = parser.parse_args(argv)

    log_dir = Path(args.log_dir) if args.log_dir else (data_root() / "logs")
    if not log_dir.is_dir():
        print(f"没有找到日志目录：{log_dir}")
        return 1

    runs = [r for r in parse_logs(log_dir) if r["usage"]]
    if not runs:
        # 区分两种情况：真的没用量行（老版本），还是有用量行但一轮都没切出来
        # （日志格式变了 / 正则失效）。后者必须报错退出，
        # 否则“工具坏了”会静默表现成“没花钱”，那比报错危险得多。
        usage_lines = sum(
            len(re.findall(r"(?:Token 用量|训练用量)：输入",
                           p.read_text(encoding="utf-8", errors="replace")))
            for p in log_dir.glob("app-*.log")
        )
        if usage_lines:
            print(
                f"统计失败：日志里有 {usage_lines} 条用量记录，但一轮也没能切出来 —— "
                "日志格式可能变了（检查 tools/token_stats.py 里的正则）。"
            )
            return 1
        print(f"{log_dir} 下的日志里没有可统计的用量记录（旧版本不记用量）。")
        return 0

    print(f"日志目录：{log_dir}")
    print(f"\n{'日期 时刻':<17}{'模式':<5}{'模型':<18}{'风格':<20}"
          f"{'请求':>5}{'计入':>5}{'写回败':>6}{'分析败':>6}{'输入':>9}{'输出':>8}{'每张输入':>9}{'每张输出':>9}")
    totals: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0, 0, 0])
    for r in runs:
        in_tok, out_tok = r["usage"]
        write_fail = len(r["write_fail_files"])
        analysis_fail = len(r["analysis_fail_files"])
        if r["mode"] == "训练":
            counted = r["samples"]
            tail = "   ← 训练（分母=样本数）"
        else:
            # 分母 = 完成 + 写回失败（写回失败的图已经拿到结果、token 已经花了）
            counted = r["done"] + write_fail
            tail = ""
        per_in = f"{in_tok / counted:>9.0f}" if counted else f"{'—':>9}"
        per_out = f"{out_tok / counted:>8.0f}" if counted else f"{'—':>8}"
        print(f"{r['date'] + ' ' + r['time']:<17}{r['mode']:<5}{r['model']:<18}{r['style'][:18]:<20}"
              f"{r['requests']:>5}{counted:>5}{write_fail:>6}{analysis_fail:>6}"
              f"{in_tok:>9}{out_tok:>8}{per_in:>9}{per_out:>8}{tail}")
        if counted:
            key = r["model"] + ("（训练）" if r["mode"] == "训练" else "")
            acc = totals[key]
            acc[0] += in_tok
            acc[1] += out_tok
            acc[2] += counted
            acc[3] += r["requests"]

    print("\n=== 按模型汇总 ===")
    for model, (in_tok, out_tok, counted, requests) in sorted(
        totals.items(), key=lambda kv: -kv[1][2]
    ):
        print(f"  {model:<22} {counted:>3} {'个样本' if '训练' in model else '张'} / {requests:>3} 次请求 | "
              f"每{'样本' if '训练' in model else '张'}平均：输入 {in_tok / counted:>6.0f} token、"
              f"输出 {out_tok / counted:>5.0f} token | 合计 输入 {in_tok}、输出 {out_tok}")
    print("\n口径提示：用量行来自服务端返回的 usage，**包含重试与本轮全部请求**；"
          "分母只算真正拿到 AI 结果的图（429/超时那类不算，它们基本不产生 token）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
