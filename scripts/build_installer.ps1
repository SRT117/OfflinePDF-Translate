param(
    [switch]$Split,
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'runtime\python\python.exe'
$compiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
if (-not (Test-Path $compiler)) { throw 'Inno Setup 6 compiler (ISCC.exe) not found.' }
& $python (Join-Path $root 'scripts\prepare_offline_assets.py')
if ($LASTEXITCODE -ne 0) { throw 'Offline asset verification failed.' }
$compilerArgs = @()
if ($Split) {
    $compilerArgs += '/DSplitInstaller=1'
}
if (-not [string]::IsNullOrWhiteSpace($Version)) {
    $compilerArgs += "/DAppVersion=$Version"
}
$compilerArgs += (Join-Path $PSScriptRoot 'installer.iss')
& $compiler @compilerArgs
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
