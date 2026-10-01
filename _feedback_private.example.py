# -*- coding: utf-8 -*-
# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""反馈通道私有凭据 —— **模板文件**

本文件是 `_feedback_private.py` 的模板，用于说明构建期注入的接口。
真正的 `_feedback_private.py` 已在 .gitignore 中排除，**不会也不得进入版本库**。

────────────────────────────────────────────────────────────
为什么公开源码里没有反馈凭据
────────────────────────────────────────────────────────────

本系统的反馈功能通过钉钉机器人的 Webhook 转发用户填写的反馈文本。
Webhook 地址与签名密钥属于**凭据**，一旦公开会被爬虫与搜索引擎永久收录，
因此不随公开源码分发。

本系统对反馈凭据采用**两级读取**（见 core/security_config.py）：

    1. 环境变量 DINGTALK_WEBHOOK_URL / DINGTALK_WEBHOOK_SECRET
    2. 构建期注入的私有模块 _feedback_private.py（即本模板对应的文件）

两级都未配置时，程序**仍可正常构建与运行**，但反馈功能不会发送任何数据，
而是提示用户改用邮箱联系方式（见《许可、隐私与免责说明》第三十九条）。

────────────────────────────────────────────────────────────
如何启用自己的反馈通道
────────────────────────────────────────────────────────────

1. 在钉钉群中添加「自定义机器人」，安全设置选择「加签」；
2. 复制得到的 Webhook 地址与签名密钥；
3. 在项目根目录创建 `_feedback_private.py`，内容如下（替换为你的值）：

       FEEDBACK_DEFAULT_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=<你的 token>"
       FEEDBACK_DEFAULT_SECRET  = "SEC<你的密钥>"

4. 打包（PyInstaller）时会自动包含该模块（见 标准处理系统.spec 的条件 hiddenimports）。

⚠ 请勿把 `_feedback_private.py` 提交到任何版本库。
"""

# 以下为占位值，**请勿直接使用**——它们不会生效。
FEEDBACK_DEFAULT_WEBHOOK = ""
FEEDBACK_DEFAULT_SECRET = ""
