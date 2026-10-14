$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$MarkerPath = Join-Path $ProjectRoot ".venv\trade-router-project.hash"
$ProjectFile = Join-Path $ProjectRoot "pyproject.toml"
$Sha256 = [System.Security.Cryptography.SHA256]::Create()
try {
    $ProjectHash = [System.BitConverter]::ToString(
        $Sha256.ComputeHash([System.IO.File]::ReadAllBytes($ProjectFile))
    ).Replace("-", "")
} finally {
    $Sha256.Dispose()
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Preparing the local Python environment..."
    & py -3 -m venv (Join-Path $ProjectRoot ".venv")
}

$InstalledHash = if (Test-Path -LiteralPath $MarkerPath) {
    (Get-Content -Raw -LiteralPath $MarkerPath).Trim()
} else {
    ""
}

if ($InstalledHash -ne $ProjectHash) {
    Write-Host "Installing Trade Router dependencies..."
    & $VenvPython -m pip install --disable-pip-version-check -e $ProjectRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }
    Set-Content -LiteralPath $MarkerPath -Value $ProjectHash -Encoding ascii
}

Write-Host "Trade Router: http://127.0.0.1:8890"
Write-Host "Press Ctrl+C or close this window to stop."
& $VenvPython -m trade_router_studio
exit $LASTEXITCODE
