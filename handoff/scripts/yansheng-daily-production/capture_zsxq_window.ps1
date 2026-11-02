param(
    [Parameter(Mandatory=$true)][string]$Start,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [int]$MaxPagesPerGroup = 80
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = 'utf-8'
if ($Start -notmatch '(Z|[+-]\d{2}:\d{2})$') { throw 'window start must include an explicit timezone' }
$startTime = [DateTimeOffset]::Parse($Start)
if ($MaxPagesPerGroup -lt 1 -or $MaxPagesPerGroup -gt 200) { throw 'invalid page limit' }
$zone = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
$current = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $zone)
if ($startTime -gt $current) { throw 'window start in future' }
$target = [IO.Path]::GetFullPath($OutputDir)
if (Test-Path -LiteralPath $target) { throw 'output directory already exists; choose a new revision' }
New-Item -ItemType Directory -Path $target | Out-Null
$groups = foreach ($gid in @('15552822254242','28512858211281')) {
    $cursor = $null; $pages = @(); $reached = $false
    foreach ($i in 1..$MaxPagesPerGroup) {
        $file = Join-Path $target ("group-{0}-p{1}.json" -f $gid,$i)
        if ($cursor) { zsxq-cli group +topics --group-id $gid --limit 30 --end-time $cursor --json | Set-Content -Encoding utf8 -LiteralPath $file }
        else { zsxq-cli group +topics --group-id $gid --limit 30 --json | Set-Content -Encoding utf8 -LiteralPath $file }
        if ($LASTEXITCODE -ne 0) { throw "request failed: $gid page $i" }
        $data = Get-Content -Raw -Encoding utf8 -LiteralPath $file | ConvertFrom-Json -DateKind String
        if (-not $data.success) { throw "request returned failure: $gid page $i" }
        $rows = @($data.topics_brief)
        $pages += [ordered]@{page=$i; path=$file; sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLower();
            fetched_at=([TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,$zone)).ToString('o')}
        if ($rows.Count -eq 0 -and -not $data.has_more) { $reached=$true; break }
        if ($rows.Count -eq 0) { throw "empty incomplete page: $gid" }
        $oldest = ($rows | ForEach-Object { [DateTimeOffset]::Parse($_.create_time) } | Sort-Object | Select-Object -First 1)
        if ($oldest -le $startTime -or -not $data.has_more) { $reached=$true; break }
        if (-not $data.next_end_time -or $data.next_end_time -eq $cursor) { throw "pagination cursor stalled: $gid" }
        $cursor = $data.next_end_time
    }
    if (-not $reached) { throw "page limit before window start: $gid" }
    [ordered]@{group=$gid; pages=$pages; reached_start=$true}
}
$result = [ordered]@{start=$startTime.ToString('o'); cutoff=$current.ToString('o'); groups=@($groups);
    captured_at=([TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow,$zone)).ToString('o');
    actual_reading_performed=$false}
$result | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 -LiteralPath (Join-Path $target 'enumeration.json')
Write-Output ("Captured two groups to {0}; cutoff {1}. Enumeration is not reading." -f $target,$current.ToString('o'))
