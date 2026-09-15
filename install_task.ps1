# Registra (o aggiorna) l'attivita' pianificata che esegue run.ps1 ogni giorno alle 03:30.
# Eseguire una volta: powershell -ExecutionPolicy Bypass -File install_task.ps1
$name = "VintedTracker"
$script = Join-Path $PSScriptRoot "run.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At 03:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -RunOnlyIfNetworkAvailable
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Attivita' '$name' registrata: ogni giorno alle 03:30 (o al primo avvio utile se il PC era spento)."
