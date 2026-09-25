# -*- coding: utf-8 -*-
"""QThread 工作器（硬约束 #9 的落地实现）。

线程纪律
--------
    严禁在子线程操作任何 UI 控件；严禁在主线程发起网络请求。

本模块的做法：
    1. 所有批量任务（预览提取、压缩、API 请求、XMP 写出、脚本生成）
       都在 QThread 的 run() 里执行；
    2. 与 UI 的**唯一**通道是 pyqtSignal；
    3. 连 logging 都通过信号转发（UiLogSignalHandler 的 callback 就是
       sig_log.emit），从而避免任何 handler 直接触碰控件。

跨线程信号是安全的
------------------
PyQt 会自动为跨线程的 signal/slot 选择 QueuedConnection，
即信号先进入接收者线程的事件队列，由事件循环在接收者线程中执行槽函数。
因此即使 sig_log 是从 ThreadPoolExecutor 的普通 worker 线程里 emit 的
（而不是 QThread 自己的线程），Qt 也会把它排队到主线程执行 —— 这正是我们要的。
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable

from PyQt6.QtCore import QThread, pyqtSignal

from ..logging_setup import get_logger
from ..pipeline.output_mode import JobCallbacks

log = get_logger("ui.worker")


class BaseWorker(QThread):
    """带标准化信号集的基类工作器。"""

    # 日志（消息, logging 级别）—— **保留供外部兼容**，但界面不再单独接它：
    # 管线消息统一走 logging（见 _emit_log），文件与界面两处同时可见。
    sig_log = pyqtSignal(str, int)
    # 进度（已完成步数, 总步数）
    sig_progress = pyqtSignal(int, int)
    # 当前阶段名
    sig_stage = pyqtSignal(str)
    # 单个文件的状态（文件名, 状态文本）
    sig_item = pyqtSignal(str, str)
    # 正常结束（结果对象）
    sig_finished = pyqtSignal(object)
    # 异常结束（错误文本）
    sig_error = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 停止旗标。用 threading.Event 而不是 volatile bool，
        # 因为它能被多个线程安全地读写（停止信号会从 UI 线程设置，
        # 由 worker 线程与内部线程池的多个线程读取）。
        self._cancel = threading.Event()

    # --- 对外接口 -----------------------------------------------------------

    def stop(self) -> None:
        """请求停止：跳过剩余请求，已完成的保留（硬约束 #8）。

        【为什么文案要说清“包括重试”】用户真实反馈：他点了暂停，
        但处理失败的那几张**还在继续重试/继续发请求**（他用 GLM 时发现的）。
        根因是停止旗标以前只被提交循环看，已经发出去的那一批会把自己的重试跑完。
        现在重试路径也会看这个旗标（见 ai/client.py 与 ai/adapter.py），
        所以界面把行为说死：不再发新请求，在飞的那几条会收尾。
        """
        self._cancel.set()
        self._emit_log(
            "已收到停止请求：**不会再发送任何新请求**（包括重试与拆单重发）；"
            "已在飞的那几条会等它返回或超时，已完成的处理结果会保留。",
            logging.WARNING,
        )

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    @property
    def is_stopping(self) -> bool:
        return self._cancel.is_set()

    # --- 回调桥接 -----------------------------------------------------------

    def _emit_log(self, message: str, level: int) -> None:
        """管线日志 → 日志系统（文件日志 + 界面日志面板都看得到）。

        【为什么不能只 emit(sig_log) —— 真实事故】
        以前管线消息只走 sig_log → 界面日志面板，**文件日志里一个字都没有**。
        后果：一批 21 张里 3 个 DNG 因为"此前失败已达上限"被永久跳过，
        文件日志里连“跳过”都没有（只有一句“共 18 项”的汇总），
        用户只能说“DNG 依然没有被处理”，而我连线索都拿不到。

        【现在的做法】统一交给 logging：
          · 文件 handler（DATED 轮转）→ 落盘，事后可查；
          · UiLogSignalHandler → 界面日志面板与状态栏（它本来就只收 INFO 以上）。
        所以两处永远不会脱节，也不会重复。
        """
        log.log(int(level), "%s", message)

    def _make_callbacks(self) -> JobCallbacks:
        """构造 pipeline 层的回调集合。

        这里只做“把普通函数调用转成信号/日志”，绝不直接操作控件。
        """
        return JobCallbacks(
            log=self._emit_log,
            progress=lambda done, total: self.sig_progress.emit(int(done), int(total)),
            stage=lambda text: self.sig_stage.emit(str(text)),
            item_status=lambda name, status: self.sig_item.emit(str(name), str(status)),
            should_stop=lambda: self._cancel.is_set(),
        )

    # --- 子类实现 -----------------------------------------------------------

    def execute(self, callbacks: JobCallbacks) -> Any:
        """子类在这里执行真正的任务。"""
        raise NotImplementedError

    # --- 线程体 -------------------------------------------------------------

    def run(self) -> None:  # noqa: D102  （QThread 接口）
        try:
            result = self.execute(self._make_callbacks())
        except Exception as exc:
            # 捕获全部异常：QThread 里未捕获的异常会导致进程直接崩溃（PyQt6 行为），
            # 而用户看到的就是"程序莫名消失了"，最难排查。
            detail = traceback.format_exc()
            log.error("工作线程异常：%s\n%s", exc, detail)
            self.sig_error.emit(f"{type(exc).__name__}: {exc}\n\n{detail[-1500:]}")
        else:
            self.sig_finished.emit(result)


class OutputWorker(BaseWorker):
    """输出模式工作器（第一节的 1–6 步）。"""

    def __init__(
        self,
        job_callable: Callable[[JobCallbacks, threading.Event], Any],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._job = job_callable

    def execute(self, callbacks: JobCallbacks) -> Any:
        return self._job(callbacks, self._cancel)


class TrainWorker(BaseWorker):
    """训练模式工作器（第二节【训练模式】）。"""

    def __init__(
        self,
        job_callable: Callable[[JobCallbacks, threading.Event], Any],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._job = job_callable

    def execute(self, callbacks: JobCallbacks) -> Any:
        return self._job(callbacks, self._cancel)


class ModelsFetchWorker(BaseWorker):
    """后台拉取"服务商真实可调用的模型列表"（保存密钥后用）。

    不继承 execute() 的 JobCallbacks 语义 —— 它只是轻量 HTTP GET，
    用 BaseWorker 的 sig_error / sig_finished 把结果带回主线程即可。
    """

    sig_models = pyqtSignal(str, list)  # (provider_key, [model_id, ...])

    def __init__(self, provider_key: str, provider, api_key: str, parent=None) -> None:
        super().__init__(parent)
        self._provider_key = provider_key
        self._provider = provider
        self._api_key = api_key

    def execute(self, callbacks: JobCallbacks) -> Any:
        from ..ai.discovery import discover_models

        return discover_models(self._provider, self._api_key)

    def run(self) -> None:  # noqa: D102
        try:
            models = self.execute(self._make_callbacks())
        except Exception as exc:
            detail = traceback.format_exc()
            log.error("拉取模型列表失败：%s\n%s", exc, detail)
            self.sig_error.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.sig_models.emit(self._provider_key, models)

