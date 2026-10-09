$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    throw "Установите Python 3.11 или новее и добавьте python.exe в PATH."
}
$NodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $NodeCommand) {
    throw "Установите Node.js 22 или новее; node.exe будет включён в приложение для управления Zalo."
}
$PythonVersion = & $PythonCommand.Source -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ([version]$PythonVersion -lt [version]"3.11") {
    throw "Нужен Python 3.11 или новее. Найдена версия $PythonVersion."
}
$PythonBits = & $PythonCommand.Source -c "import struct; print(struct.calcsize('P') * 8)"
if ($PythonBits -ne "64") { throw "Нужен 64-разрядный Python для Windows x64." }
$NodeVersion = & $NodeCommand.Source -p "process.versions.node"
if ([version]$NodeVersion -lt [version]"22.0") {
    throw "Нужен Node.js 22 или новее. Найдена версия $NodeVersion."
}

& $PythonCommand.Source -m pip install -r (Join-Path $Root "requirements-windows.txt")
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости Windows-сборки." }

$Dist = Join-Path $Root "dist-windows"
$Work = Join-Path $Root ".build-windows"
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
$Static = Join-Path $Root "botrieltor\static"
$Driver = Join-Path $Root "botrieltor\zalo_driver.mjs"
$Node = $NodeCommand.Source
$NodeLicense = Join-Path (Split-Path $Node -Parent) "LICENSE"
$NodeLicenses = Join-Path (Split-Path $Node -Parent) "LICENSES"

$PyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "BotRieltor",
    "--distpath", $Dist,
    "--workpath", $Work,
    "--add-data", "${Static}:botrieltor\static",
    "--add-data", "${Driver}:botrieltor",
    "--add-binary", "${Node}:.",
    "--collect-all", "webview",
    "--collect-all", "pythonnet",
    "--collect-all", "clr_loader",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "clr",
    (Join-Path $Root "windows_app.py")
)
& $PythonCommand.Source @PyInstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller не собрал Windows-приложение." }

$Readme = Join-Path $Root "README-Windows.txt"
$Archive = Join-Path $Dist "BotRieltor-Windows-x64.zip"
$PackageFiles = @((Join-Path $Dist "BotRieltor.exe"), $Readme)
if (Test-Path $NodeLicense) {
    $BundledLicense = Join-Path $Dist "Node-LICENSE.txt"
    Copy-Item -Force $NodeLicense $BundledLicense
    $PackageFiles += $BundledLicense
}
if (Test-Path $NodeLicenses) {
    $BundledLicenses = Join-Path $Dist "Node-LICENSES"
    Copy-Item -Recurse -Force $NodeLicenses $BundledLicenses
    $PackageFiles += $BundledLicenses
}
Compress-Archive -Force -Path $PackageFiles -DestinationPath $Archive
Write-Host "Windows-сборка готова: $Archive"
