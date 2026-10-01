@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo  标准处理系统 - 安装程序打包脚本
echo  将 installer.py 打包成独立 exe
echo ========================================
echo.
echo 确保已安装依赖：
echo   pip install pyinstaller pywin32 pillow pywebview
echo.
echo 界面说明：安装程序使用 pywebview + 系统 WebView2 渲染 installer_ui.html，
echo 目标机器需具备 Microsoft Edge WebView2 运行时（Win11 / 已装新版 Edge 的 Win10 均自带）；
echo 若缺失，安装程序会自动退化为基础安装界面，不影响安装功能。
echo.

rem ── 打包前置检查：版本号单一来源 + 安装包 ZIP 必须与版本一致 ──
rem （版本号来自项目根 version.txt；spec 会把它打进包里，安装器运行时也读它）
if not exist "..\version.txt" (
    echo [错误] 找不到 ..\version.txt —— 版本号的唯一来源，无法继续。
    echo         请先在项目根目录创建 version.txt 并写入版本号，例如 2.0.0
    pause
    exit /b 1
)
set /p APPVER=<..\version.txt
set "ZIPNAME=Standard Processing SystemV%APPVER%.zip"
echo 版本号: %APPVER%
echo 安装包: %ZIPNAME%
if not exist "%ZIPNAME%" (
    echo.
    echo [错误] 找不到本版本的安装包 %ZIPNAME%
    echo         请先打包应用生成该 ZIP，或核对 version.txt 与包内版本是否一致。
    dir /b "Standard Processing System*.zip" 2>nul
    pause
    exit /b 1
)

echo 正在打包安装程序...
pyinstaller installer.spec --noconfirm --clean

if %errorlevel% equ 0 (
    echo ========================================
    echo  安装程序打包成功！
    echo  输出: dist\标准处理系统_安装程序.exe
    echo  大小约 300 MB（含 ZIP 安装包）
    echo ========================================
) else (
    echo ========================================
    echo  打包失败，请检查报错
    echo ========================================
)
pause
