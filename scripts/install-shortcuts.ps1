<#
    Adds the terminal shortcuts to your PowerShell profile, so InciWeb and the
    rest work in every new terminal.

        .\scripts\install-shortcuts.ps1

    Run it from wherever you cloned the repository - it works out its own
    location, so nobody has to edit a path. Safe to run twice; it checks before
    adding anything.

    PowerShell only. If your terminal is Git Bash or cmd, these functions do not
    exist there and this will not change that.
#>

$shortcuts = Join-Path $PSScriptRoot "inci.ps1"
if (-not (Test-Path $shortcuts)) {
    Write-Host "Cannot find $shortcuts - run this from inside the repository." -ForegroundColor Red
    return
}

$line = ". `"$shortcuts`""

# $PROFILE differs between Windows PowerShell 5.1 and PowerShell 7, and VS Code
# often launches a different one from the standalone app. This writes to the
# profile of whichever shell is running it, which is the one you are in.
if (-not (Test-Path $PROFILE)) {
    New-Item -ItemType File -Path $PROFILE -Force | Out-Null
    Write-Host "Created $PROFILE" -ForegroundColor DarkGray
}

if (Select-String -Path $PROFILE -SimpleMatch "inci.ps1" -Quiet -ErrorAction SilentlyContinue) {
    Write-Host "Already in your profile - nothing to do." -ForegroundColor DarkGray
} else {
    Add-Content -Path $PROFILE -Value "`n# Incident diagnosis system shortcuts`n$line"
    Write-Host "Added to $PROFILE" -ForegroundColor Green
}

# Load it now as well, so the terminal you are in does not have to be reopened.
. $shortcuts

Write-Host ""
Write-Host "Done. Type InciHelp to see the commands." -ForegroundColor Cyan
Write-Host "Every new PowerShell terminal will load them from now on." -ForegroundColor DarkGray
if ((Get-ExecutionPolicy -Scope CurrentUser) -eq "Restricted") {
    Write-Host ""
    Write-Host "Your execution policy is Restricted, which blocks local scripts." -ForegroundColor Yellow
    Write-Host "  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned" -ForegroundColor Yellow
}
