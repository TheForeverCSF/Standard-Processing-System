; 标准处理系统 - Inno Setup 安装程序脚本
; 编译：安装 Inno Setup 6 (https://jrsoftware.org/isdl.php)，右键此文件 → Compile；或命令 iscc setup.iss
;
; 用途：把 PyInstaller 打好的「发布目录」直接安装到 Program Files（另一条路线是
;       installer.py + installer.spec 的图形化安装程序，两者产出不同，别混用）。
;
; 版本号规则（重要）：唯一来源是项目根 version.txt。这里的 MyAppVersion 必须与它一致，
;       安装结束时会自动比对程序目录里的 version.txt，不一致会明确报错提示，避免出现
;       "安装包显示一个版本、程序里是另一个版本"。
;
; 用法提示：发布目录名带版本号。改版本时同步下面的 APP_SRC，或编译时用
;       iscc /DAPP_SRC="..\你的发布目录" setup.iss

#define MyAppName "标准处理系统"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "The Forever CSF"
#define MyAppURL "https://github.com/TheForeverCSF"
#define MyAppExeName "标准处理系统.exe"

; ── 发布目录定位（默认取与版本号同名的目录；也认 发布版 这个旧名字）──
#ifdef APP_SRC
#else
  #if FileExists(AddBackslash(SourcePath) + "..\Standard Processing SystemV2.0.0\标准处理系统.exe")
    #define APP_SRC "..\Standard Processing SystemV2.0.0"
  #else
    #define APP_SRC "..\发布版"
  #endif
#endif

[Setup]
AppId={{B8F3A2E1-5C7D-4A9B-8D6F-2E1C3A5B7D9F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=.
OutputBaseFilename={#MyAppName}_安装程序_v{#MyAppVersion}
SetupIconFile=..\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra
SolidCompression=yes
PrivilegesRequired=admin
WizardStyle=modern
DisableProgramGroupPage=yes
DisableDirPage=no
UsePreviousAppDir=yes
CloseApplications=force
RestartApplications=no

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Files]
; 主程序及所有运行时文件（完整复制发布目录）
; Excludes：用户数据（设置/词典）绝不随包分发 —— 它们是用户自己积累的数据，
; 升级时覆盖会造成不可逆丢失（数据实际位置见下方 [UninstallDelete] 注释）
Source: "{#APP_SRC}\*"; DestDir: "{app}"; \
    Excludes: ".app_settings.json,标准名称字典.db,*.log"; \
    Flags: ignoreversion recursesubdirs createallsubdirs uninsremovereadonly
; 用户说明：安装向导里要能打开阅读，先解到临时目录（dontcopy = 不安装进程序目录）
Source: "{#APP_SRC}\标准处理系统 许可、隐私与免责说明.pdf"; DestDir: "{tmp}"; Flags: dontcopy
; 说明文件
Source: "README.txt"; DestDir: "{app}"; Flags: ignoreversion

; 安装完成后注册右键菜单（用户勾选了「系统集成」任务时）
; 扩展名清单与程序内（web/server.py）保持一致：以后新增格式只需改这里的 $exts
[Run]
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""& { \
        $exe = '{app}\{#MyAppExeName}'; \
        $icon = '{app}\{#MyAppExeName}'; \
        $name = '{#MyAppName}'; \
        $q = [char]34; \
        $exts = @('.pdf', '.doc', '.docx', '.docm', '.xls', '.xlsx', '.xlsm', \
                  '.ppt', '.pptx', '.pptm', '.txt', '.csv', '.md', '.log', '.rtf'); \
        foreach ($e in $exts) { \
            $k = 'HKCU:\Software\Classes\SystemFileAssociations\' + $e + '\shell\' + $name; \
            if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }; \
            Set-ItemProperty -Path $k -Name 'MUIVerb' -Value ('使用' + $name + '处理'); \
            Set-ItemProperty -Path $k -Name 'Icon' -Value $icon; \
            $c = $k + '\command'; \
            if (-not (Test-Path $c)) { New-Item -Path $c -Force | Out-Null }; \
            Set-ItemProperty -Path $c -Name '(default)' -Value ($q + $exe + $q + ' ' + $q + '%1' + $q); \
        }; \
        $folderPath = 'HKCU:\Software\Classes\Folder\shell\' + $name; \
        if (-not (Test-Path $folderPath)) { New-Item -Path $folderPath -Force | Out-Null }; \
        Set-ItemProperty -Path $folderPath -Name 'MUIVerb' -Value ('使用' + $name + '处理此文件夹'); \
        Set-ItemProperty -Path $folderPath -Name 'Icon' -Value $icon; \
        $folderCmd = $folderPath + '\command'; \
        if (-not (Test-Path $folderCmd)) { New-Item -Path $folderCmd -Force | Out-Null }; \
        Set-ItemProperty -Path $folderCmd -Name '(default)' -Value ($q + $exe + $q + ' ' + $q + '%1' + $q); \
        $bgPath = 'HKCU:\Software\Classes\Directory\Background\shell\' + $name; \
        if (-not (Test-Path $bgPath)) { New-Item -Path $bgPath -Force | Out-Null }; \
        Set-ItemProperty -Path $bgPath -Name 'MUIVerb' -Value ('使用' + $name + '打开此位置'); \
        Set-ItemProperty -Path $bgPath -Name 'Icon' -Value $icon; \
        $bgCmd = $bgPath + '\command'; \
        if (-not (Test-Path $bgCmd)) { New-Item -Path $bgCmd -Force | Out-Null }; \
        Set-ItemProperty -Path $bgCmd -Name '(default)' -Value ($q + $exe + $q + ' ' + $q + '%V' + $q); \
    }"""; \
    Description: "注册文件资源管理器右键菜单"; \
    Tasks: contextmenu; \
    StatusMsg: "正在注册右键菜单..."; \
    Flags: runhidden

; 安装完成后启动程序
[Run]
Filename: "{app}\{#MyAppExeName}"; \
    Description: "启动 {#MyAppName}"; \
    Tasks: startapp; \
    Flags: postinstall skipifsilent nowait

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: checkedonce
Name: quicklaunchicon; Description: "创建快速启动栏快捷方式"; GroupDescription: "快捷方式："; Flags: checkedonce
Name: startapp; Description: "安装完成后启动 {#MyAppName}"; GroupDescription: "其他："; Flags: checkedonce
Name: contextmenu; Description: "添加文件资源管理器右键菜单（推荐）"; GroupDescription: "系统集成："; Flags: checkedonce
Name: installocr; Description: "安装 Tesseract OCR 识别引擎（处理扫描件需要）"; GroupDescription: "可选组件："; Flags: checkedonce

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\用户说明"; Filename: "{app}\标准处理系统 许可、隐私与免责说明.pdf"
Name: "{group}\第三方组件与许可"; Filename: "{app}\THIRD-PARTY-NOTICES.txt"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: quicklaunchicon

[UninstallRun]
; 清理右键菜单（与安装时注册的键一一对应，含全部扩展名）
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""& { \
        $name = '{#MyAppName}'; \
        $keys = @('HKCU:\Software\Classes\Folder\shell\' + $name, \
                  'HKCU:\Software\Classes\Directory\Background\shell\' + $name); \
        $exts = @('.pdf', '.doc', '.docx', '.docm', '.xls', '.xlsx', '.xlsm', \
                  '.ppt', '.pptx', '.pptm', '.txt', '.csv', '.md', '.log', '.rtf'); \
        foreach ($e in $exts) { \
            $keys += 'HKCU:\Software\Classes\SystemFileAssociations\' + $e + '\shell\' + $name; \
        }; \
        foreach ($k in $keys) { \
            if (Test-Path $k) { Remove-Item -Path $k -Recurse -Force -ErrorAction SilentlyContinue } \
        } \
    }"""; \
    Flags: runhidden

; ── 卸载：用户数据不在程序目录里，卸载程序**不删**用户数据 ──
; 用户数据（设置、标准名称字典）统一存放在 %LOCALAPPDATA%\标准处理系统，
; 卸载时只删程序目录；数据保留，重装后继续可用（卸载完成时会提示数据位置）。
; 旧版本曾在这里删 {app}\标准名称字典.db 与 {app}\.app_settings.json —— 那是
; "卸载即永久丢失用户积累"的路径，已移除。

[Code]
var
  AgreementPage: TOutputMsgWizardPage;
  AgreementCheck: TNewCheckBox;

// 打开用户说明（PDF 已在 [Files] 里标记 dontcopy，需要时解到临时目录再交给系统打开）
procedure OpenAgreementLink(Sender: TObject);
var
  ErrCode: Integer;
begin
  if ExtractTemporaryFile('标准处理系统 许可、隐私与免责说明.pdf') then
    ShellExec('open', ExpandConstant('{tmp}\标准处理系统 许可、隐私与免责说明.pdf'), '', '',
              SW_SHOWNORMAL, ewNoWait, ErrCode)
  else
    MsgBox('未能打开用户说明，请到安装包中查看《标准处理系统 许可、隐私与免责说明.pdf》。', mbInformation, MB_OK);
end;

// 自定义"用户说明"页：勾选框 + 打开说明链接
// 说明：Inno 自带的许可页只支持 .txt/.rtf，而我们的说明是 PDF，所以自建一页。
//       本说明**不是需要"同意"的用户协议**：勾选框只是「我已阅读」的自述，
//       不勾选也只由 NextButtonClick 提醒一下，用户坚持继续照样放行。
procedure InitializeWizard();
var
  Link: TNewStaticText;
begin
  AgreementPage := CreateOutputMsgPage(wpInfoBefore, '用户说明',
    '建议先阅读《标准处理系统 用户说明》',
    '本说明介绍软件的使用范围、隐私处理与免责事项（不是需要"同意"的用户协议，不勾选也可以继续）。点击下面的链接可打开完整说明（PDF）。');

  AgreementCheck := TNewCheckBox.Create(AgreementPage);
  AgreementCheck.Parent := AgreementPage.Surface;
  AgreementCheck.Left := 0;
  AgreementCheck.Top := 0;
  AgreementCheck.Width := AgreementPage.SurfaceWidth;
  AgreementCheck.Caption := '我已阅读《标准处理系统 用户说明》';
  AgreementCheck.Checked := False;

  Link := TNewStaticText.Create(AgreementPage);
  Link.Parent := AgreementPage.Surface;
  Link.Left := 0;
  Link.Top := 26;
  Link.Caption := '打开用户说明（PDF）';
  Link.Font.Color := clBlue;
  Link.Font.Style := [fsUnderline];
  Link.Cursor := crHand;
  Link.OnClick := @OpenAgreementLink;
end;

// 未勾选时**只建议**阅读，不阻断安装：选「是」返回阅读，选「否」直接继续
// （替代原先失效的 WizardForm.LicenseAcceptedRadio 判断：没有 LicenseFile 时
//   许可页根本不会出现，那段判断是死代码）
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = AgreementPage.ID) and (not AgreementCheck.Checked) then
  begin
    if MsgBox('建议您先阅读一下《标准处理系统 用户说明》。' + #13#10 + #13#10 +
              '选择「是」现在返回阅读；选择「否」将直接继续安装。',
              mbConfirmation, MB_YESNO) = IDYES then
      Result := False;      // 返回阅读
  end;
end;

// 版本无关地查找并启动 Tesseract 安装包（程序目录 / _internal 都找）
// 注意：安装包会以管理员权限启动；其内容哈希白名单在程序内
//      （utils/ocr_verify.py），"设置 → 安装 OCR 引擎"那条路径会强制校验。
procedure RunOcrInstallerIfSelected();
var
  Base, Found: String;
  FindRec: TFindRec;
  ResultCode: Integer;
begin
  if not WizardIsTaskSelected('installocr') then
    exit;

  Base := ExpandConstant('{app}');
  Found := '';
  if FindFirst(Base + '\tesseract-ocr-*.exe', FindRec) then begin
    Found := Base + '\' + FindRec.Name;
    FindClose(FindRec);
  end else if FindFirst(Base + '\_internal\tesseract-ocr-*.exe', FindRec) then begin
    Found := Base + '\_internal\' + FindRec.Name;
    FindClose(FindRec);
  end;

  if Found = '' then begin
    MsgBox('未找到 OCR 安装包（tesseract-ocr-*.exe），已跳过。' + #13#10 +
           '如果以后需要处理扫描件，请在程序「设置 → 安装 OCR 引擎」中安装。',
           mbInformation, MB_OK);
    exit;
  end;

  if not Exec(Found, '', Base, SW_SHOW, ewNoWait, ResultCode) then
    MsgBox('OCR 安装程序启动失败：' + Found + #13#10 +
           '可手动运行该文件完成安装。', mbCriticalError, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  V: String;
begin
  if CurStep <> ssPostInstall then
    exit;

  // 1) OCR：勾选了就装，没勾选就提示以后在哪装
  if WizardIsTaskSelected('installocr') then
    RunOcrInstallerIfSelected()
  else
    MsgBox('提示：你跳过了 OCR 引擎安装。' + #13#10 +
           '如果以后需要处理扫描件，请在程序「设置 → 安装 OCR 引擎」中安装。',
           mbInformation, MB_OK);

  // 2) 版本一致性自检：程序里的 version.txt 必须与本安装包版本一致
  //    PyInstaller 6 的 onedir 布局把 datas 放进 _internal，两处都找
  V := '';
  if not LoadStringFromFile(ExpandConstant('{app}\version.txt'), V) then
    LoadStringFromFile(ExpandConstant('{app}\_internal\version.txt'), V);
  if Trim(V) <> '' then begin
    if Trim(V) <> '{#MyAppVersion}' then
      MsgBox('注意：本安装包版本是 {#MyAppVersion}，但程序文件里的 version.txt 是 "' +
             Trim(V) + '"。' + #13#10 +
             '请核对发布目录与项目根 version.txt（版本号唯一来源）后再分发。',
             mbError, MB_OK);
  end;
end;

// 卸载完成后告知用户数据仍在（本安装程序不删用户数据）
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('卸载完成。' + #13#10 + #13#10 +
           '你的个人数据（设置、标准名称字典）仍保留在：' + #13#10 +
           ExpandConstant('{localappdata}\{#MyAppName}') + #13#10 +
           '重装后会自动继续使用；如确认不再需要，可手动删除该文件夹。',
           mbInformation, MB_OK);
end;
