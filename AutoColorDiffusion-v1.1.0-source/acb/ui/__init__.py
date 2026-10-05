# -*- coding: utf-8 -*-
"""界面层（PyQt6）。

线程纪律（硬约束 #9）：
    所有批量任务都在 QThread 中执行，通过 pyqtSignal 回传进度与日志；
    严禁在子线程操作任何 UI 控件；严禁在主线程发起网络请求。
    本包通过 workers.BaseWorker 把这套纪律固化下来。
"""
