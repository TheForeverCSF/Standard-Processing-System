@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo  标准处理系统 - 打包脚本（优化版）
echo  预计耗时 5-15 分钟
echo ========================================

echo 正在打包，请耐心等待...
pyinstaller "标准处理系统.spec" --noconfirm --clean

if %errorlevel% equ 0 (
    echo ========================================
    echo  打包成功！正在执行后处理优化...
    echo ========================================

    echo [1/2] 移除无关 Qt 插件（QML 非必要主题等）...
    powershell -NoProfile -Command ^
        "$d='dist\标准处理系统\_internal\PyQt6\Qt6';" ^
        "if (Test-Path \"$d\qml\QtMultimedia\") { Remove-Item \"$d\qml\QtMultimedia\" -Recurse -Force; echo '  已删除 QtMultimedia QML' };" ^
        "if (Test-Path \"$d\plugins\multimedia\") { Remove-Item \"$d\plugins\multimedia\" -Recurse -Force; echo '  已删除 multimedia 插件' };" ^
        "if (Test-Path \"$d\sqldrivers\") { Remove-Item \"$d\sqldrivers\" -Recurse -Force; echo '  已删除 SQL 驱动' };" ^
        "if (Test-Path \"$d\plugins\sqldrivers\") { Remove-Item \"$d\plugins\sqldrivers\" -Recurse -Force; echo '  已删除 SQL 插件' };" ^
        "if (Test-Path \"$d\qml\QtTest\") { Remove-Item \"$d\qml\QtTest\" -Recurse -Force; echo '  已删除 QtTest QML' };" ^
        "echo [2/2] 应用 NTFS 压缩（透明，不影响性能）..." ^
        "compact /C /S:\"dist\标准处理系统\" /Q /I >nul" ^
        "echo  NTFS 压缩完成"

    echo ========================================
    echo  优化完成！
    echo  输出: dist\标准处理系统\标准处理系统.exe
    echo ========================================
) else (
    echo ========================================
    echo  打包失败，请检查报错
    echo ========================================
)
pause
