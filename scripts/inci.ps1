<#
    Terminal shortcuts for the incident diagnosis system.

    Load them once per shell:

        . C:\Pratyush\B.Tech\Project\AI-Assisted-Incident-Diagnosis-System\scripts\inci.ps1

    or permanently, by adding that line to your PowerShell profile:

        notepad $PROFILE          # creates it if missing

    Opening a page is the small half. The useful half is InciUp and InciStatus,
    which bake in the three things that have actually broken this stack:

      1. Container images do not track the repository. Three separate defects in
         this project were a merged fix that was not in the running image, and
         each time the symptom looked like a different bug. InciUp rebuilds.
      2. The orchestrator's Kafka consumer used to die on cold starts and stay
         dead silently. Fixed in #50, but InciStatus still checks it, because
         the failure is invisible from the outside: the API stays healthy and
         incidents simply never open.
      3. A fault injected with no traffic moves no metric and nothing can detect
         it. InciStatus reports the offered request rate for that reason.
#>

$script:InciRoot = Split-Path -Parent $PSScriptRoot

$script:InciUrls = [ordered]@{
    console    = @{ Url = "http://localhost:5010"; What = "Incident Console - inject, decide, review, measure" }
    shop       = @{ Url = "http://localhost";      What = "Sock Shop - the application under test" }
    approval   = @{ Url = "http://localhost:3000"; What = "Approval console (M4) - the HITL workflow" }
    metrics    = @{ Url = "http://localhost:9090"; What = "Prometheus - raw metrics" }
    diagnosis  = @{ Url = "http://localhost:8000/docs"; What = "Diagnosis API docs" }
    orch       = @{ Url = "http://localhost:8090/docs"; What = "Orchestrator API docs" }
}

function script:Open-InciUrl($key) {
    $entry = $script:InciUrls[$key]
    try {
        $r = Invoke-WebRequest -Uri $entry.Url -TimeoutSec 4 -UseBasicParsing -ErrorAction Stop
        Write-Host ("  {0}  -> HTTP {1}" -f $entry.Url, $r.StatusCode) -ForegroundColor Green
    } catch {
        # Say so rather than opening a tab onto a connection error, which is
        # the worst thing to discover with someone watching.
        Write-Host ("  {0} is not responding. Run InciUp first." -f $entry.Url) -ForegroundColor Yellow
    }
    Start-Process $entry.Url
}

# ----------------------------------------------------------------- the pages

function InciWeb      { Open-InciUrl console }
function SockShopWeb  { Open-InciUrl shop }
function InciApproval { Open-InciUrl approval }
function InciMetrics  { Open-InciUrl metrics }
function InciDocs     { Open-InciUrl diagnosis; Open-InciUrl orch }

<#  Everything a demo needs, in the order you would show it.  #>
function InciDemo {
    Write-Host "Opening the demo tabs..." -ForegroundColor Cyan
    foreach ($k in @("shop", "console", "approval", "metrics")) { Open-InciUrl $k; Start-Sleep -Milliseconds 400 }
}

# ----------------------------------------------------------------- the stack

<#  Bring the stack up the way that actually works: build first, then start,
    then confirm. Takes a few minutes the first time.  #>
function InciUp {
    Push-Location $script:InciRoot
    try {
        if (-not (docker info 2>$null)) {
            Write-Host "Docker is not running. Start Docker Desktop and try again." -ForegroundColor Yellow
            return
        }
        Write-Host "Building images (they do not track the repository)..." -ForegroundColor Cyan
        docker compose build
        Write-Host "Starting..." -ForegroundColor Cyan
        docker compose up -d
        Start-Sleep -Seconds 25
        InciStatus
    } finally { Pop-Location }
}

function InciDown {
    Push-Location $script:InciRoot
    try { docker compose down } finally { Pop-Location }
}

function InciLogs($service = "eval-dashboard") {
    Push-Location $script:InciRoot
    try { docker compose logs -f --tail 50 $service } finally { Pop-Location }
}

# ----------------------------------------------------------------- the checks

<#  What is actually wrong, rather than whether containers exist.  #>
function InciStatus {
    Push-Location $script:InciRoot
    try {
        if (-not (docker info 2>$null)) {
            Write-Host "  Docker is not running." -ForegroundColor Red
            return
        }
        $running = (docker compose ps --format "{{.State}}" 2>$null | Where-Object { $_ -eq "running" }).Count
        Write-Host ("  containers running   {0}" -f $running) -ForegroundColor (@{$true="Green";$false="Yellow"}[$running -ge 28])

        # Traffic: a fault with no traffic is undetectable, so this is not cosmetic.
        try {
            $stats = Invoke-RestMethod "http://localhost:5002/stats" -TimeoutSec 4
            $rps = [math]::Round($stats.achieved_rps, 2)
            Write-Host ("  offered load         {0} req/s" -f $rps) -ForegroundColor (@{$true="Green";$false="Yellow"}[$rps -ge 1])
        } catch { Write-Host "  offered load         load generator not answering" -ForegroundColor Yellow }

        # The consumer: its death is invisible from outside, so ask directly.
        try {
            $h = Invoke-RestMethod "http://localhost:8090/health" -TimeoutSec 4
            $c = if ($h.consumer) { $h.consumer.state } else { "not reported (image predates #50)" }
            Write-Host ("  kafka consumer       {0}" -f $c) -ForegroundColor (@{$true="Green";$false="Yellow"}[$c -eq "consuming"])
            Write-Host ("  orchestrator db      {0}" -f $h.database)
        } catch { Write-Host "  orchestrator         not answering" -ForegroundColor Yellow }

        try {
            $d = Invoke-RestMethod "http://localhost:5010/api/health" -TimeoutSec 5
            $props = @($d.checks.PSObject.Properties)
            $ok = @($props | Where-Object { $_.Value.ok }).Count
            $all = $props.Count
            Write-Host ("  console backends     {0}/{1} healthy" -f $ok, $all) -ForegroundColor (@{$true="Green";$false="Yellow"}[$ok -eq $all])
        } catch { Write-Host "  console              not answering - is eval-dashboard up?" -ForegroundColor Yellow }

        # Whether the LLM will actually answer, or fall back silently.
        $envFile = Join-Path $script:InciRoot ".env"
        if (Test-Path $envFile) {
            $key = (Select-String -Path $envFile -Pattern '^\s*GEMINI_API_KEY\s*=\s*(\S+)' -ErrorAction SilentlyContinue)
            if ($key) { Write-Host "  gemini key           set" -ForegroundColor Green }
            else { Write-Host "  gemini key           empty - every diagnosis uses the deterministic ranker" -ForegroundColor Yellow }
        } else {
            Write-Host "  gemini key           no .env - every diagnosis uses the deterministic ranker" -ForegroundColor Yellow
        }
    } finally { Pop-Location }
}

# ----------------------------------------------------------------- the demo

<#  Inject a fault through the console's own API, so the demo path is the one
    being demonstrated. Defaults to a catalogue crash, which is visible in Sock
    Shop as a 502 - a CPU throttle is real but largely invisible on screen.  #>
function InciInject($fault = "service_crash", $service = "catalogue", $seconds = 120) {
    $body = @{ fault_type = $fault; service = $service; duration_s = $seconds } | ConvertTo-Json
    try {
        $r = Invoke-RestMethod "http://localhost:5010/api/faults" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 20
        Write-Host ("  injected {0} on {1} for {2}s" -f $r.fault_type, $r.ground_truth_service, $seconds) -ForegroundColor Green
        Write-Host ("  scenario {0}" -f $r.scenario_id) -ForegroundColor DarkGray
        if ($r.warning) { Write-Host ("  warning: {0}" -f $r.warning) -ForegroundColor Yellow }
        Write-Host "  watch it at http://localhost:5010/#history" -ForegroundColor Cyan
    } catch {
        Write-Host ("  could not inject: {0}" -f $_.Exception.Message) -ForegroundColor Red
    }
}

function InciFaults {
    try { (Invoke-RestMethod "http://localhost:5001/fault-types" -TimeoutSec 5) -join ", " }
    catch { Write-Host "  fault injector not answering. Run InciUp first." -ForegroundColor Yellow }
}

function InciHelp {
    Write-Host ""
    Write-Host "  Pages" -ForegroundColor Cyan
    Write-Host "    InciWeb        the Incident Console (inject, decide, review, measure)"
    Write-Host "    SockShopWeb    Sock Shop, the application under test"
    Write-Host "    InciApproval   the approval console"
    Write-Host "    InciMetrics    Prometheus"
    Write-Host "    InciDemo       every demo tab, in presenting order"
    Write-Host ""
    Write-Host "  Stack" -ForegroundColor Cyan
    Write-Host "    InciUp         build, start, and report what is wrong"
    Write-Host "    InciDown       stop everything"
    Write-Host "    InciStatus     traffic, kafka consumer, console backends, API key"
    Write-Host "    InciLogs <svc> follow one service's logs"
    Write-Host ""
    Write-Host "  Demo" -ForegroundColor Cyan
    Write-Host "    InciFaults                        list the fault classes"
    Write-Host "    InciInject                        crash catalogue for 120s (visible in Sock Shop)"
    Write-Host "    InciInject bad_deploy_latency catalogue 90"
    Write-Host ""
}

Write-Host "Incident system shortcuts loaded. Type InciHelp." -ForegroundColor DarkGray
