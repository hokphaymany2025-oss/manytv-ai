<#
.SYNOPSIS
    Launches a local ComfyUI instance tuned for an 8GB Intel Arc A750.

.DESCRIPTION
    ComfyUI is a separate project, not vendored into ManyTV. One-time setup:

      1. Clone it somewhere, e.g.:
           git clone https://github.com/comfyanonymous/ComfyUI D:\NewProjects\ComfyUI
      2. Create ITS OWN venv (do not share ManyTV's backend venv) and install
         the Intel XPU torch build + ComfyUI's own requirements.txt there.
         See README.md "Intel Arc (XPU) setup" for the exact pip commands.
      3. Install Intel's Arc & Iris Xe Graphics Windows driver if you
         haven't already (required for the XPU backend to see the GPU).

    -Mode picks ComfyUI's VRAM management strategy. Checked against this
    project's actual cloned comfy/cli_args.py rather than generic docs --
    this version ships a "Dynamic VRAM" system that is ON BY DEFAULT and
    changes what these flags do:
      auto        (default) Passes no VRAM flag at all, so Dynamic VRAM
                  manages loading/offload automatically. This is the
                  intended default on 8GB now, not a fallback.
      gpu-only    DISABLES Dynamic VRAM and forces everything onto the GPU.
                  Only for workflows you're sure fit entirely in 8GB --
                  generally the wrong direction for a video pipeline here.
      lowvram     Legacy flag; per its own --help text it "doesn't do
                  anything if dynamic vram is enabled" (the default) beyond
                  forcing text encoders onto CPU. Kept for completeness /
                  older ComfyUI checkouts.
      novram      DISABLES Dynamic VRAM entirely; for when Dynamic VRAM
                  itself still OOMs.
    There is no "normalvram" flag in this ComfyUI version -- an earlier
    draft of this script referenced one from memory and would have crashed
    with "unrecognized argument"; removed after checking the real source.

    -VramHeadroomGB maps to --vram-headroom, which Dynamic VRAM uses to
    keep that much VRAM free "even counting VRAM from other apps" (its own
    wording) -- directly relevant here since Ollama loads its model fully
    onto this same Arc A750 (confirmed via `ollama ps`) for ManyTV's
    /api/generate-script. The single-slot worker already prevents a script
    job and a video job from *running* concurrently, but Ollama keeps its
    model resident in VRAM for a few minutes after each call by default, so
    some headroom here is cheap insurance against that overlap window.

.EXAMPLE
    .\scripts\run_comfyui.ps1 -ComfyUIPath "D:\NewProjects\ComfyUI"

.EXAMPLE
    .\scripts\run_comfyui.ps1 -ComfyUIPath "D:\NewProjects\ComfyUI" -VramHeadroomGB 2 -Fp8
#>

param(
    [string]$ComfyUIPath = $env:COMFYUI_PATH,
    [ValidateSet("auto", "gpu-only", "lowvram", "novram")]
    [string]$Mode = "auto",
    [int]$Port = 8188,
    # GB of VRAM to keep free for other apps (e.g. Ollama) sharing this GPU.
    # Only meaningful in "auto" mode (Dynamic VRAM); ignored otherwise.
    [double]$VramHeadroomGB = 0,
    # Loads UNet + text encoder weights as fp8 to further cut VRAM use.
    # Requires the workflow's checkpoint/UNet loader to support it; safe to
    # try first, drop it if a node complains.
    [switch]$Fp8
)

if (-not $ComfyUIPath) {
    Write-Error "ComfyUI path not set. Pass -ComfyUIPath or set the COMFYUI_PATH environment variable."
    exit 1
}

if (-not (Test-Path $ComfyUIPath)) {
    Write-Error "ComfyUI path '$ComfyUIPath' does not exist."
    exit 1
}

$VenvPython = Join-Path $ComfyUIPath ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Error "Expected a ComfyUI venv at '$VenvPython'. Create it and install the Intel XPU torch build first (see README.md)."
    exit 1
}

$launchArgs = @("main.py", "--port", $Port)

switch ($Mode) {
    "gpu-only" { $launchArgs += "--gpu-only" }
    "lowvram"  { $launchArgs += "--lowvram" }
    "novram"   { $launchArgs += "--novram" }
    "auto"     {
        if ($VramHeadroomGB -gt 0) {
            $launchArgs += @("--vram-headroom", $VramHeadroomGB)
        }
    }
}

if ($Fp8) {
    $launchArgs += @("--fp8_e4m3fn-unet", "--fp8_e4m3fn-text-enc")
}

Write-Host "Starting ComfyUI (mode=$Mode$(if ($Fp8) { ', fp8' })) on port $Port ..."
Write-Host "  $VenvPython $($launchArgs -join ' ')"

Push-Location $ComfyUIPath
try {
    & $VenvPython @launchArgs
}
finally {
    Pop-Location
}
