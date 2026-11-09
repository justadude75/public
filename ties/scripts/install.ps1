# Install ties on Windows and add a desktop shortcut.
#   cd ties; powershell -ExecutionPolicy Bypass -File scripts\install.ps1
# Re-running is safe: it updates the install and leaves your vault alone.
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $PSScriptRoot

$Py = (Get-Command py -ErrorAction SilentlyContinue)
if ($Py) { $PyCmd = @("py", "-3") } else { $PyCmd = @("python") }
& $PyCmd[0] $PyCmd[1..9] -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
  Write-Host "ties needs Python 3.10 or newer. Install it from https://www.python.org/downloads/ and run this again."
  exit 1
}

Write-Host "Installing ties in $Here\.venv ..."
& $PyCmd[0] $PyCmd[1..9] -m venv "$Here\.venv"
& "$Here\.venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$Here\.venv\Scripts\python.exe" -m pip install --quiet -e "$Here[encryption,fast]"

$Ties = "$Here\.venv\Scripts\ties.exe"
$Vault = if ($env:TIES_VAULT) { $env:TIES_VAULT } else { "$HOME\.ties\personal.vault" }

$Launcher = "$([Environment]::GetFolderPath('Desktop'))\ties.bat"
Set-Content -Path $Launcher -Value "@echo off`r`nrem Opens ties in your browser. Close this window to stop it.`r`n`"$Ties`" --vault `"$Vault`" serve --open"
Write-Host "Launcher: $Launcher (double-click it)"

if (-not (Test-Path $Vault)) {
  Write-Host ""
  Write-Host "No vault yet. Creating an encrypted one at $Vault (you'll choose a passphrase)."
  & $Ties --vault $Vault init --encrypt
}
Write-Host ""
Write-Host "Done. Start ties with the desktop shortcut."
