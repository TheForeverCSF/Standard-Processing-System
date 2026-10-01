# 标准处理系统 Standard Processing System

批量处理办公文档，自动识别并提取其中的中国国家标准与国际标准（GB/T、GB、ISO、IEC、EN、ASTM 等）编号，
关联标准名称字典，生成结构化的标准引用报告。本地处理优先，支持 OCR。

## 功能

- 批量导入 PDF / Word / Excel / PowerPoint / 纯文本类文档
- 自动从正文、表格、图片（OCR）中识别标准编号
- 本地标准名称字典匹配 + 联网查询补充（**联网查询可关闭**）
- 生成结构化处理结果与统计报告
- 支持导出 Excel（`.xlsx`）/ JSON / CSV
- 标准名称字典的审核与管理
- 可选的**涉密模式**：严格断网隔离（需管理员权限，详见下文）

## 环境要求

- Python 3.10+
- Windows（旧版 `.doc` / `.xls` 格式的转换依赖 `pywin32` 与 Office 组件）
- 可选：Tesseract OCR、PaddleOCR（用于图片文字识别）

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 Web 服务（浏览器访问）
python web/server.py
```

浏览器打开 `http://127.0.0.1:8765` 即可使用。

桌面模式：`python main_browser.py`（需 PyQt6 + PyQt6-WebEngine）。**打包发行的正式版本即以此模式启动。**

## 支持的文档格式

| 完整支持 | 其他已支持 |
|---|---|
| PDF（`.pdf`） | Word：`.doc` `.docx` `.docm`<br>Excel：`.xls` `.xlsx` `.xlsm`<br>PowerPoint：`.pptx` `.pptm`<br>文本类：`.txt` `.csv` `.tsv` `.md` `.log` `.rtf` |

- **不支持**旧版 PowerPoint 二进制格式 `.ppt`（请先另存为 `.pptx`）。
- **无法处理**加密或受密码保护的文档（请先自行解密）。
- PDF 为经过最充分测试的格式；其他格式功能已实现，但测试覆盖不如 PDF，请对结果多加留意。

## 项目结构

```
标准处理系统/
├── web/                    # Web 服务端 + 前端界面
│   ├── server.py           # HTTP API 服务
│   ├── index.html          # 前端界面
│   └── pdf.min.mjs         # PDF.js 渲染引擎
├── core/                   # 文档处理核心
│   ├── processor.py        # 处理调度
│   ├── pdf_handler.py      # PDF 处理
│   ├── docx_handler.py     # DOCX 处理
│   ├── excel_handler.py    # Excel 处理
│   ├── pptx_handler.py     # PPTX 处理
│   ├── ocr_handler.py      # OCR 识别
│   ├── ai_handler.py       # AI 辅助
│   ├── preview_service.py  # 预览服务
│   └── security_config.py  # 涉密模式与凭据管理
├── gui/                    # 桌面界面（PyQt6）
├── utils/
│   └── helpers.py          # 工具函数
├── 安装程序/                # 安装包构建脚本、第三方组件与许可文本
├── dict_manager.py         # 字典管理工具
└── 标准名称字典.db          # 标准名称数据库（SQLite）
```

## 隐私与安全

- **本地处理优先**：所有文档处理（文本提取、OCR 识别、字典匹配）默认全部在您的本机完成。
  我们**不上传、不收集**您的文档内容；本程序**不含**遥测、统计上报、崩溃上报、自动更新检查或用户跟踪。

- **联网查询标准名称 —— 默认开启，可在设置中关闭**

  处理完成后，本程序会就本地字典未能匹配到的标准编号发起查询，**仅发送标准编号**，不发送文档内容。
  查询目标站点：

  | 境内 | **境外** |
  |---|---|
  | `openstd.samr.gov.cn`、`std.samr.gov.cn` | `en.wikipedia.org`、`webstore.ansi.org`、`www.iso.org` |

  上述站点的服务器会按其自身规则记录访问日志（通常包含 IP 地址与访问时间）。
  请求以本系统自身标识发送（`StandardProcessingSystem/<版本号>`），**不伪装浏览器、不伪造请求来源**。

  **在涉密网络、内网、或禁止外联的网络环境中，请先关闭此功能**（或启用涉密模式）。

- **本地绑定**：Web 服务器仅绑定 `127.0.0.1:8765`，不对外暴露。
  **该服务不设账号密码认证**，请勿在不受信任的计算机或多人共用的环境中运行 Web 模式。

- **临时文件与日志**：处理中的文件暂存于系统临时目录（`%TEMP%\standard_system`），
  **正常退出时会自动清理**，程序异常终止时可能残留。
  处理日志**包含所处理文件的路径与文件名**，仅保存在本机、不会外发；涉密模式下路径会被脱敏。

- **涉密模式**（可选，**需管理员权限**）：启用后将在应用层拦截本程序自身的全部非本机网络连接、
  写入 Windows 防火墙规则、并对日志中的文件路径脱敏。关闭同样需要管理员权限。
  **该模式采用应用层拦截，不能替代物理隔离、专用网络等必要措施**，
  也不保证满足任何特定保密等级或行业监管要求。

> 完整的数据处理说明、免责声明、责任限制与许可附加条款，见
> **[《标准处理系统 许可、隐私与免责说明》](标准处理系统_许可隐私与免责说明_v3.0.md)**。

## 许可证

本项目以 **GNU Affero General Public License v3.0（AGPL-3.0）** 发布。
完整条款见 [LICENSE](LICENSE)，亦可从 <https://www.gnu.org/licenses/agpl-3.0.html> 获取。

> **源码获取**
>
> - 源码仓库：<https://github.com/TheForeverCSF/Standard-Processing-System>
> - 该仓库的 **Releases 页面**同时提供与本发行版本对应的源代码归档（Source code zip / tar.gz）——
>   与二进制包位于同一位置、可直接下载、不再另行收费。
> - 若上述地址暂时无法访问，可通过邮箱索取：**the_forever_csf@126.com**

> **网络服务条款提示**：依 AGPL-3.0 第 13 条，若您修改本程序后通过网络向他人提供服务，
> 您必须向所有通过网络访问该服务的使用者提供您所修改版本的完整源代码。

第三方组件及其许可全文见 [THIRD-PARTY-NOTICES.txt](安装程序/THIRD-PARTY-NOTICES.txt)
与 `安装程序/licenses/` 目录。组件与许可的对应关系、免责与责任限制条款，
见《标准处理系统 许可、隐私与免责说明》第三十四条、第三十五条与第四部分。

## 使用期望（非许可条款）

我们希望本系统被用于合法、正当的用途，并恳请您不要将其用于侵害他人合法权益、
或违反您所在地法律法规的活动。

以上仅为开发者作为个人的期望，**不构成本项目的许可条件，亦不构成对 AGPL-3.0
所授予的任何权利的额外限制**。您可以不同意以上任何一条，这不会影响您依该许可使用本系统的权利。

## 声明

本系统由 The Forever CSF 设计开发，AI 代码生成辅助完成。
