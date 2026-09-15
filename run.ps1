# Esegue il tracker (fetch + build) e pubblica la dashboard su GitHub Pages.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$env:Path += ";C:\Program Files\Git\cmd"
$log = Join-Path $PSScriptRoot "logs\run.log"

"=== $(Get-Date -Format s) avvio ===" | Out-File $log -Append -Encoding utf8
py tracker.py run 2>&1 | Out-File $log -Append -Encoding utf8

git add -A 2>&1 | Out-File $log -Append -Encoding utf8
git commit -m "dati $(Get-Date -Format 'yyyy-MM-dd HH:mm')" 2>&1 | Out-File $log -Append -Encoding utf8
git push 2>&1 | Out-File $log -Append -Encoding utf8
"=== $(Get-Date -Format s) fine ===" | Out-File $log -Append -Encoding utf8
