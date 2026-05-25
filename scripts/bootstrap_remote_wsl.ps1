param(
    [string]$Remote = "marcd@100.112.76.122",
    [string]$RemoteWinPath = "D:\claude-plan-executor",
    [string]$RemoteWslPath = "/mnt/d/claude-plan-executor",
    [string]$Distro = "",
    [string]$IdentityFile = "",
    [switch]$RunTests
)

$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "update_remote_wsl.ps1"

& $script `
    -Remote $Remote `
    -RemoteWinPath $RemoteWinPath `
    -RemoteWslPath $RemoteWslPath `
    -Distro $Distro `
    -IdentityFile $IdentityFile `
    -RunTests:$RunTests
exit $LASTEXITCODE
