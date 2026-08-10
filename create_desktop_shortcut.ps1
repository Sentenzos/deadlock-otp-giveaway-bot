$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $projectDir "start_bot.cmd"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Deadlock OTP Bot.lnk"

if (-not (Test-Path -LiteralPath $target)) {
    Write-Error "Launcher file was not found: $target"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $projectDir
$shortcut.Description = "Запуск Telegram/Twitch giveaway bot"
$shortcut.Save()

Write-Host "Shortcut created: $shortcutPath"
