# Auto-activate the virtual environment when opening PowerShell in this folder
$venvPath = ".\.venv\Scripts\Activate.ps1"

if (Test-Path $venvPath) {
    Write-Host "Activating virtual environment..."
    & $venvPath
} else {
    Write-Host "No virtual environment found at $venvPath"
}
