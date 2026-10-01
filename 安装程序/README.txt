标准处理系统 - 安装程序
=========================

本目录包含图形化安装程序，无需 Inno Setup 即可编译。

版本号（单一来源，重要）
--------------------------------------
· 版本号**唯一来源**是项目根的 `version.txt`（当前 2.0.0）。要改版本，只改它。
· 本目录里所有版本号都会跟着它走：
  - `installer.spec` 直接读 version.txt 生成 exe 版本信息，并推导出
    安装包名 `Standard Processing SystemV<版本>.zip`；这个名字对不上会**中止打包**。
  - `installer.py` 运行时也从 version.txt 读（打包后找 exe 同级 / 包内副本）。
  - `build_installer.bat` 打包前校验 version.txt 存在、且对应版本的 zip 已就位。
  - `setup.iss`（Inno Setup 路线）里写死的 MyAppVersion 必须与 version.txt 一致，
    安装结束时会自动比对程序目录里的 version.txt，不一致会明确报错。
· `installer_ui.html` 里只剩界面默认值（如版本徽标）与预览模式假数据，运行时会被真实值覆盖。

第三方组件与许可（法务要求，别删）
--------------------------------------
· `THIRD-PARTY-NOTICES.txt`：随发行包分发的第三方组件清单（组件/版本/许可/文件位置）。
· `licenses\`：各许可证全文，均为**逐字节复制**的权威原文，不要手改：
  - GPL-3.0.txt（PyQt6）、LGPL-3.0.txt（Qt 6.11.2）、AGPL-3.0.txt（MuPDF/PyMuPDF）、
    Apache-2.0.txt（PDF.js 4.10.38）、Adobe-CMap-License.txt（web\cmaps 下 168 个 .bcmap）、
    Foxit-Standard-Fonts-License.txt（web\standard_fonts 下 10 个 .pfb）、
    PyMuPDF-COPYING.txt，以及 Pillow / pywebview / pythonnet / clr_loader / PyQt6-sip /
    PyInstaller(bootloader) 的许可文本。
· PDF.js 系数据文件的**原始**许可文件同时放在项目根 `web\` 对应目录里
  （web\LICENSE、web\cmaps\LICENSE、web\standard_fonts\LICENSE_FOXIT），
  这样每次重新构建应用都会自动带上；请勿只更新其中一处。
· 安装包 zip 与安装器 exe 都会带上 `THIRD-PARTY-NOTICES.txt` + `licenses\`
  （前者打进 zip，后者见 installer.spec 的 datas）。
· 两项待法务确认（写在 THIRD-PARTY-NOTICES.txt 第五节的"待办"）：
  1) QtWebEngine 内嵌 Chromium（Qt6WebEngineCore.dll ≈194MB）的许可汇编
     LICENSES.chromium.html 未随 PyQt6 wheel 提供，需从 Qt 6.11.2 官方发行包补齐；
  2) 第四节"源码获取方式"（AGPL-3.0 要求）目前是占位符，需填真实联系方式/仓库。

两种安装包装路线（别混用）
--------------------------------------
路线 A（默认，图形化）：`installer.py` + `installer_ui.html` + `installer.spec`
  → 产出 `dist\标准处理系统_安装程序.exe`，内含 ZIP 安装包与全部界面资源。
路线 B（Inno Setup）：`setup.iss`
  → 把「发布目录」（PyInstaller 打好的程序目录，如 `..\Standard Processing SystemV2.0.0`）
    直接装到 Program Files；不依赖 WebView2，也不含 ZIP。
  编译：安装 Inno Setup 6 → 右键 setup.iss → Compile（或 `iscc setup.iss`）；
      发布目录名不同时用 `iscc /DAPP_SRC="..\你的目录" setup.iss` 覆盖。

界面（installer_ui.html + installer.py）
--------------------------------------
· pywebview（系统 WebView2 内核）渲染内嵌 HTML，无边框窗口 720x600。
· 开场先居中展示 Logo 与欢迎词，再动画打开选项页；三步流程：安装选项 → 安装中 → 完成。
· 安装过程中播放 The Forever CSF 徽标视频，底部以字幕形式实时显示当前任务与日志。
· 勾选框是「我已阅读」的自述：不勾选也能装，点「立即安装」只弹一句"建议先阅读"，
  用户选「继续安装」就继续（说明文档不是需要"同意"的用户协议，不阻断安装）。
· 缺少 WebView2 / pywebview 时，自动退化为基础 Tk 界面（按默认设置安装），不会中断安装。

用户数据（重要）
--------------------------------------
· 主程序的用户数据（设置、标准名称字典、WebEngine 缓存）统一放在
  %LOCALAPPDATA%\标准处理系统 —— 安装目录可能是 %ProgramFiles%（普通用户不可写），
  旧版本写在那里会出现"设置改了不生效、词典录了查不到"，甚至启动卡死。
· 老版本数据在程序目录的用户，主程序首次运行会自动**复制**一份到数据目录（不删原文件）。
· 覆盖安装前，安装程序会先把用户数据备份到「我的文档\标准处理系统备份\覆盖安装备份_时间戳」，
  再执行删除；即使用户选择"不沿用旧词典"，也能从备份取回。
· 关闭了「沿用上一版本的标准名称词典」时，安装前会弹窗二次确认（默认「保留」），
  并按选择把数据目录里的词典替换为随包词典（避免"选了不保留、程序仍在用旧词典"）。
· 卸载时会弹窗询问是否保留用户数据（默认保留），且无论选什么都会先备份到「我的文档」。

如实上报
--------------------------------------
· 桌面/开始菜单/卸载快捷方式、右键菜单、卸载信息登记：逐项按真实结果记录，
  失败的项在完成页以红色「创建失败」显示，不再一律报"已创建"。
· 安装失败不再只闪 3 秒提示：会弹出失败原因详情，并可一键跳转查看完整安装日志。

两种使用方式
--------------------------------------
方式一：直接运行 Python 脚本（需要 Python 环境）
  pip install pywebview pillow pywin32
  python installer.py
  或 python installer.py "D:\其他路径\Standard Processing System.zip"

  安装程序从同目录下的 Standard Processing System*.zip 读取文件并安装。

方式二：打包成独立 exe（推荐分发给用户）
  1. 安装依赖：
     pip install pyinstaller pywin32 pillow pywebview

  2. 运行打包脚本：
     build_installer.bat

  3. 输出在 安装程序\dist\标准处理系统_安装程序.exe
     运行该 exe 即可安装（内含 ZIP 安装包与全部界面资源）

命令行 / 环境变量
--------------------------------------
  [安装包路径]                指定 ZIP 安装包；省略则自动匹配同目录最新版本
  --elevated                  内部使用：表示已提权启动
  CSF_INSTALLER_SKIP_ELEVATE=1   跳过自动提权（界面走查用）
  CSF_INSTALLER_UI_PREVIEW=setup|progress|finish
                              用假数据直接打开指定界面，便于调整 UI
                              （直接双击 installer_ui.html?preview=1#progress 也能预览）

路线 B（Inno Setup）功能
--------------------------------------
├─ 向导式安装（现代样式）、默认装到 Program Files、升级沿用原目录
├─ 用户说明页：勾选「我已阅读」（不勾选也会放行，只弹一次"建议先阅读"提示）；可点链接打开 PDF
├─ 右键菜单覆盖全部支持格式（pdf/word/excel/ppt/txt/csv/md/log/rtf + 文件夹 + 右键空白处）
├─ 可选安装 Tesseract OCR 引擎（安装包按 tesseract-ocr-*.exe 通配查找，不写死版本）
├─ 卸载只删程序目录：用户数据在 %LOCALAPPDATA%\标准处理系统，**不删**，并明确提示位置
└─ 安装结束自检：程序目录 version.txt 与安装包版本不一致时明确报错

安装程序功能
--------------------------------------
├─ 内嵌 HTML 界面（深色、简洁，720x600 无边框窗口）
├─ 安装中播放 Logo 视频 + 底部字幕式任务/日志
├─ 用户说明弹层（超长扫描件自动生成缩略预览）
├─ 自定义安装位置 / 覆盖安装自动回到原目录
├─ 覆盖安装时保留上一版本设置与标准名称词典
├─ 桌面快捷方式、开始菜单快捷方式
├─ 文件资源管理器右键菜单（PDF / Word / 文件夹）
├─ Tesseract OCR 引擎安装（可跳过 / 失败可手动补装）
├─ 单页高级设置：主题、双栏预览、联网与涉密模式、Q/ 标准、年代号匹配、PDF 质量
├─ 控制面板卸载支持
└─ 安装后自动清理旧版本残留
