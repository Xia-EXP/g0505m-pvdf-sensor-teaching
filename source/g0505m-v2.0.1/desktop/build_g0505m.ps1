# 作用：独立构建 G0505M，先验证源码再生成包含 Qt 运行时的 Windows 便携包。
param([switch]$InstallDependencies)
$ErrorActionPreference = "Stop"
$DesktopRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkspaceRoot = Split-Path -Parent $DesktopRoot
$PythonExe = Join-Path $DesktopRoot ".venv-build\Scripts\python.exe"

# 作用：优先复用项目 Python 3.12，源码包也可由用户指定引导解释器路径。
if (-not (Test-Path -LiteralPath $PythonExe)) {
    $BootstrapPython = $env:G0505M_PYTHON
    if ([string]::IsNullOrWhiteSpace($BootstrapPython)) {
        $BootstrapPython = Join-Path $WorkspaceRoot ".venv\Scripts\python.exe"
    }
    if (-not (Test-Path -LiteralPath $BootstrapPython)) {
        throw "请先安装 Python 3.12 并用 G0505M_PYTHON 指定解释器，或在 desktop 创建 .venv-build。"
    }
    & $BootstrapPython -m venv (Join-Path $DesktopRoot ".venv-build")
    if ($LASTEXITCODE -ne 0) { throw "创建构建环境失败" }
    $InstallDependencies = $true
}

function Invoke-G0505mPython {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    # 作用：外部工具失败立即中断，防止把失败构建当作可交付版本。
    & $PythonExe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "构建命令失败：$($Arguments -join ' ')" }
}

Invoke-G0505mPython @("-c", "import sys; assert sys.version_info[:2] == (3,12), 'Windows release requires Python 3.12'")
if ($InstallDependencies) {
    Invoke-G0505mPython @("-m", "pip", "install", "-r", (Join-Path $DesktopRoot "requirements-dev.txt"))
}

Push-Location $DesktopRoot
try {
    Invoke-G0505mPython @("-m", "ruff", "check", "src", "tests", "run_g0505m.py", "tools\verify_g0505m_ui.py", "tools\package_g0505m.py")
    Invoke-G0505mPython @("-m", "pytest", "--junitxml", (Join-Path $WorkspaceRoot "tmp\g0505m-tests.xml"))
    Invoke-G0505mPython @("tools\verify_g0505m_ui.py")
    # 作用：为新教具使用专属 PyInstaller 工作目录，保留旧 PVDF 构建产物。
    Invoke-G0505mPython @("-m", "PyInstaller", "--noconfirm", "--clean", "--workpath", "build\g0505m", "g0505m_teaching.spec")
    Invoke-G0505mPython @("tools\package_g0505m.py")
}
finally {
    Pop-Location
}
Write-Host "完成：$WorkspaceRoot\release\G0505M手掌受力教具\G0505MTeaching.exe"
