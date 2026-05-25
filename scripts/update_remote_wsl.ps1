param(
    [string]$Remote = "marcd@100.112.76.122",
    [string]$RemoteWinPath = "D:\claude-plan-executor",
    [string]$RemoteWslPath = "/mnt/d/claude-plan-executor",
    [string]$Distro = "",
    [string]$IdentityFile = "",
    [switch]$NoPrune,
    [switch]$RunTests,
    [string]$TestCommand = "venv/bin/python -m pytest -q tests/scripts/test_claude_agent_manifest.py tests/scripts/test_plan_ops_mcp_shape_normalization.py"
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments)

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Invoke-RemotePowerShell {
    param([string]$Script)

    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
    $sshArgs = @()
    if ($IdentityFile.Trim().Length -gt 0) {
        $sshArgs += @("-i", $IdentityFile, "-o", "IdentitiesOnly=yes")
    }
    $sshArgs += @($Remote, "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded)
    Invoke-Checked "ssh" $sshArgs
}

function Invoke-RemoteWsl {
    param([string]$BashScript)

    $encodedBash = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($BashScript))
    $wslCommand = "printf %s '$encodedBash' | base64 -d > /tmp/claude-plan-executor-update.sh && bash /tmp/claude-plan-executor-update.sh"
    $distroLine = ""
    if ($Distro.Trim().Length -gt 0) {
        $escapedDistro = $Distro.Replace("'", "''")
        $distroLine = "`$argsList += @('-d', '$escapedDistro')"
    }

    $ps = @"
`$ErrorActionPreference = 'Stop'
`$encodedBash = '$encodedBash'
`$wslCommand = "printf %s '`$encodedBash' | base64 -d > /tmp/claude-plan-executor-update.sh && bash /tmp/claude-plan-executor-update.sh"
`$argsList = @()
$distroLine
`$argsList += @('--', 'bash', '-lc', `$wslCommand)
& wsl.exe @argsList
exit `$LASTEXITCODE
"@
    Invoke-RemotePowerShell $ps
}

function Convert-To-BashSingleQuoted {
    param([string]$Value)
    return "'" + $Value.Replace("'", "'\''") + "'"
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $repoRoot
try {
    Invoke-Checked "git" @("rev-parse", "--is-inside-work-tree") | Out-Null

    $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("claude-plan-executor-sync-" + [Guid]::NewGuid().ToString("N"))
    $stage = Join-Path $tempRoot "src"
    $archive = Join-Path $tempRoot "claude-plan-executor.tar.gz"
    New-Item -ItemType Directory -Force -Path $stage | Out-Null

    try {
        $files = & git ls-files --cached --modified --others --exclude-standard
        if ($LASTEXITCODE -ne 0) {
            throw "git ls-files failed"
        }

        foreach ($file in $files) {
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
                continue
            }
            $dest = Join-Path $stage $file
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
            Copy-Item -LiteralPath $file -Destination $dest
        }

        Invoke-Checked "tar" @("-czf", $archive, "-C", $stage, ".")

        $remoteIncomingWin = Join-Path $RemoteWinPath ".remote-sync"
        $remoteArchiveWin = Join-Path $remoteIncomingWin "claude-plan-executor.tar.gz"
        $remoteArchiveScp = $remoteArchiveWin.Replace("\", "/")
        $remoteScpTarget = "${Remote}:$remoteArchiveScp"

        Invoke-RemotePowerShell "New-Item -ItemType Directory -Force -Path '$($remoteIncomingWin.Replace("'", "''"))' | Out-Null"
        $scpArgs = @()
        if ($IdentityFile.Trim().Length -gt 0) {
            $scpArgs += @("-i", $IdentityFile, "-o", "IdentitiesOnly=yes")
        }
        $scpArgs += @($archive, $remoteScpTarget)
        Invoke-Checked "scp" $scpArgs

        if ($remoteArchiveWin -notmatch "^[A-Za-z]:") {
            throw "RemoteWinPath must be an absolute Windows drive path, for example D:\claude-plan-executor"
        }
        $drive = $remoteArchiveWin.Substring(0, 1).ToLowerInvariant()
        $rest = $remoteArchiveWin.Substring(2).Replace("\", "/")
        $remoteArchiveWsl = "/mnt/$drive$rest"
        $repoQ = Convert-To-BashSingleQuoted $RemoteWslPath
        $archiveQ = Convert-To-BashSingleQuoted $remoteArchiveWsl
        $prune = if ($NoPrune) { "false" } else { "true" }
        $runTestsValue = if ($RunTests) { "true" } else { "false" }
        $testCommandQ = Convert-To-BashSingleQuoted $TestCommand

        $bash = @"
set -euo pipefail
repo=$repoQ
archive=$archiveQ
test_command=$testCommandQ

mkdir -p "`$repo"
if [ "$prune" = "true" ]; then
  find "`$repo" -mindepth 1 -maxdepth 1 ! -name venv ! -name .remote-sync -exec rm -rf {} +
fi

tar -xzf "`$archive" -C "`$repo"
cd "`$repo"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required inside remote WSL. Install it with: sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip" >&2
  exit 127
fi

python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install --no-cache-dir -r requirements-dev.txt
venv/bin/python plugins/plan-executor/scripts/plan_ops.py path-info --json

if [ "$runTestsValue" = "true" ]; then
  eval "`$test_command"
fi
"@
        Invoke-RemoteWsl $bash
    }
    finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force
        }
    }
}
finally {
    Pop-Location
}
