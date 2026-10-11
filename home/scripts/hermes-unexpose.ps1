$ErrorActionPreference = 'SilentlyContinue'
# bin\hermes.exe is a silent no-op on this install; resolve by asking each candidate.
$exe = $null
foreach ($c in @(
    'C:\Users\Ha Trung\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe',
    'C:\Users\Ha Trung\AppData\Local\hermes\bin\hermes.exe')) {
    if (Test-Path $c) {
        $ver = (& $c --version 2>&1 | Out-String).Trim()
        if ($ver -match 'Hermes') { $exe = $c; break }
    }
}
if (-not $exe) { $exe = 'C:\Users\Ha Trung\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe' }
$ts      = 'C:\Program Files\Tailscale\tailscale.exe'
$desktop = [Environment]::GetFolderPath('Desktop')
$log     = Join-Path $desktop 'hermes-expose.log'
$urlFile = Join-Path $desktop 'hermes-url.txt'
$port    = 9119

function Log([string]$m) { "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m | Add-Content -Path $log }

# ---------- 1. funnel first, so no public route outlives the backend ----------
# Funnel config is persisted by tailscaled and SURVIVES a reboot, so it must be turned off
# explicitly here -- stopping the backend alone would leave the public URL pointing at nothing.
if (Test-Path $ts) {
    $before = & $ts funnel status 2>&1 | Out-String
    if ($before -match 'https://[a-z0-9.-]+\.ts\.net') {
        & $ts funnel --https=443 off 2>&1 | Out-Null
        Start-Sleep -Seconds 3
        $after = & $ts funnel status 2>&1 | Out-String
        if ($after -match 'https://[a-z0-9.-]+\.ts\.net') {
            Log 'FAIL - funnel van con sau lenh off; kiem tra bang: tailscale funnel status'
        } else {
            Log 'FUNNEL-OFF cong public da dong'
        }
    } else {
        Log 'FUNNEL-NONE khong co funnel dang bat'
    }
} else {
    Log 'WARN - khong thay tailscale.exe, bo qua buoc tat funnel'
}

# ---------- 2. backend ----------
$before = @(Get-NetTCPConnection -State Listen -LocalPort $port)
if ($before.Count -eq 0) {
    Log 'BACKEND-NONE cong 9119 trong san'
} else {
    $oldPid = $before[0].OwningProcess
    # hermes dashboard --stop terminates every dashboard/serve process, including the one the
    # Desktop app spawns for its own window. A relaunch on 127.0.0.1 afterwards is normal.
    & $exe dashboard --stop | Out-Null
    Start-Sleep -Seconds 5
    $after = @(Get-NetTCPConnection -State Listen -LocalPort $port)
    if ($after.Count -gt 0) {
        Log ("BACKEND-PARTIAL van con listener {0} PID {1} (PID cu {2})" -f `
            $after[0].LocalAddress, $after[0].OwningProcess, $oldPid)
    } else {
        Log "BACKEND-OFF da dung PID $oldPid, cong 9119 trong"
    }
}

Remove-Item $urlFile -ErrorAction SilentlyContinue
Log 'OFF-HET - khong con URL cong khai'
