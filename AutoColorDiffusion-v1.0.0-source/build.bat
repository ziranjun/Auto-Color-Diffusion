@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem ===========================================================================
rem  Auto Color Diffusion —— Windows 打包脚本
rem ---------------------------------------------------------------------------
rem  做五件事：
rem    1. 检查 Python
rem    2. 建虚拟环境 .venv（若不存在）
rem    3. 安装/更新依赖
rem    4. 用 PyInstaller 打包为 onedir 产物（优先 onedir，见下方说明）
rem    5. 打印产物清单与后续自验步骤
rem
rem  为什么优先 --onedir 而不是 --onefile：
rem    onedir：启动快（约 1–2 秒，直接加载 _internal 下的 dll/pyd）；
rem            排障容易（缺哪个 dll 一目了然，日志里能直接看到路径）；
rem            更新方便（只替换改动文件，不必重下 200MB）。
rem    onefile：分发只有一个文件，但每次启动都要把整个包解压到
rem            %TEMP%\_MEIxxxx，首次启动可能要 10–30 秒；
rem            被杀软反复扫描解压目录，误报率更高；
rem            临时目录如果被清理策略删除，程序会直接起不来。
rem    本工具面向"一次装好长期用"的场景，因此选 onedir。
rem    若你确实需要单文件：**不能只改命令行参数** —— acb.spec 里写的是
rem    EXE(exclude_binaries=True, …) + COLLECT(…) 的 onedir 结构，改单文件需要
rem    把 EXE 改成 exclude_binaries=False 并删掉 COLLECT（或改用 PyInstaller 的
rem    --onefile 重新生成一份 spec）。改完请阅读 README 的「打包后首次运行常见问题」。
rem ===========================================================================

cd /d "%~dp0"

echo.
echo ===========================================================================
echo  Auto Color Diffusion 打包脚本
echo ===========================================================================
echo.

rem --- 1. 检查 Python ---------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python。请先安装 Python 3.10 或更高版本，
    echo        并在安装时勾选 "Add Python to PATH"。
    echo        下载：https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

for /f "delims=" %%V in ('python -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%V"
echo [1/5] 使用 Python !PYVER!

rem --- 1b. 前置检查：程序不能还在运行 -----------------------------------------
rem 为什么必须先查：正在运行的 AutoColorDiffusion.exe 会占住 dist\ 里的文件，
rem PyInstaller 清理旧产物时会报 “PermissionError: [WinError 5] 拒绝访问”，
rem 那个报错完全看不出是“程序没关”。所以这里先给一句人话。
tasklist /fi "IMAGENAME eq AutoColorDiffusion.exe" 2>nul | find /i "AutoColorDiffusion.exe" >nul
if not errorlevel 1 (
    echo [错误] AutoColorDiffusion.exe 正在运行 —— 它会占住 dist\ 目录，打包必然失败。
    echo        请先完全退出程序（包括任务栏里的窗口），再重新运行本脚本。
    pause
    exit /b 1
)

rem --- 2. 创建虚拟环境 --------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [2/5] 创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo [2/5] 虚拟环境已存在，跳过创建。
)

set "VENV_PY=.venv\Scripts\python.exe"

rem --- 3. 安装依赖 ------------------------------------------------------------
echo [3/5] 安装依赖（首次约需 3–8 分钟，视网络而定）...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败。若在国内网络，可先换镜像源再重试：
    echo        "%VENV_PY%" -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
)

rem --- 4. 打包 -----------------------------------------------------------------
echo [4/5] 开始打包（约需 2–5 分钟）...
"%VENV_PY%" -m PyInstaller --noconfirm --clean acb.spec
if errorlevel 1 (
    echo [错误] 打包失败。常见原因：
    echo        - 杀毒软件锁定了 build 目录：请关闭实时防护后重试；
    echo        - 磁盘空间不足：onedir 产物约 150–250MB；
    echo        - exiftool.exe 不存在：本脚本会在缺失时打印提示，可忽略。
    pause
    exit /b 1
)

rem --- 5. 产物清单 -------------------------------------------------------------
echo.
echo [5/5] 打包完成。产物清单：
echo.
echo   dist\AutoColorDiffusion\
echo     AutoColorDiffusion.exe          ^<-- 双击运行（无控制台窗口）
echo     _internal\                  运行库与打包资源（不要删、不要单独移动）
echo       config\models.yaml        模型配置（首次运行会复制到 %%APPDATA%%）
echo       styles\default_neutral.json  内置中性基准风格
echo       styles\AI自主决策.json    内置「AI自主决策」风格（同样不可删）
echo       assets\icc\*.icc          程序自己生成的 sRGB / Adobe RGB(1998) 等效 profile
echo                                  （Display P3 不捆绑：系统里没有时导出会告警，见 README）
echo       assets\jsx\export_batch.jsx  Photoshop 导出脚本模板
 echo       exiftool.exe + exiftool_files\  整份捆绑的 ExifTool（DNG 内嵌 XMP 读写用它）
echo.
echo   运行时数据（不会放在 _internal 里）：
echo     %%APPDATA%%\AutoColorDiffusion\logs\    日志（按日期轮转，单文件 10MB，留 5 份）
echo     %%APPDATA%%\AutoColorDiffusion\cache\thumbs\  缩略图缓存
echo     %%APPDATA%%\AutoColorDiffusion\styles\  训练产出的风格档案
echo     %%APPDATA%%\AutoColorDiffusion\state\   断点续跑状态与 failed.json
echo     %%APPDATA%%\AutoColorDiffusion\config\models.yaml  可写配置副本
echo.
echo   exiftool 已整份捆绑在 _internal\ （启动器 exe + exiftool_files\，DNG 内嵌 XMP 读写用它）；
 echo   不想捆绑就把 vendor\exiftool 挪走（或设 ACB_EXIFTOOL_DIR 指向别的目录）。
echo.
echo ===========================================================================
echo  下一步：自验
echo ===========================================================================
echo   1. 在装有 Python 的本机先做一次链路验证：
echo        "%VENV_PY%" app.py --no-gui --files <你的测试目录> --dry-run
echo   2. 把整个 dist\AutoColorDiffusion 目录拷到**没有装 Python 的干净 Windows 虚拟机**，
 echo      双击 AutoColorDiffusion.exe，添加一个测试目录，在高级设置里勾选「离线调试」，点开始。
echo      这是打包要求里明确规定的验收步骤（在无 Python 环境完成一次 dry-run）。
echo   3. 首次运行若被 SmartScreen 拦住：点"更多信息"->"仍要运行"。
echo      若被杀软报毒：见 README 的「打包后首次运行常见问题」一节。
echo.
pause
