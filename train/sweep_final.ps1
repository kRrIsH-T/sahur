# Final grid: cross-entropy vs RLCD, three seeds each, sharing one calibration split, then seed ensembles.
$py = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$train = Join-Path $PSScriptRoot "train_fast.py"
$ensemble = Join-Path $PSScriptRoot "ensemble.py"
$common = "--char-min 1 --char-max 4 --buckets 65536 --epochs 30"
foreach ($loss in "ce", "rlcd") {
    foreach ($seed in 1, 2, 3) {
        Write-Output "=== $loss seed $seed"
        & $py -W ignore $train --name "f-$loss-s$seed" --loss $loss --seed $seed $common.Split(" ") 2>&1 |
            Select-String -Pattern '"(cal_acc_augmented|coverage|acc_when_accepted|size_mb|train_seconds)"'
    }
    Write-Output "=== ensemble $loss"
    & $py -W ignore $ensemble --name "f-$loss-ens" "f-$loss-s1" "f-$loss-s2" "f-$loss-s3" 2>&1 |
        Select-String -Pattern '"(cal_acc_augmented|coverage|acc_when_accepted|size_mb)"'
}
