# Запускать из корня репозитория (там, где orchestrator_v7.py) в PowerShell.
# Без git — просто перемещение файлов через Move-Item.
$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path pipeline, baseline, cache, archive | Out-Null

# --- актуальные stage-скрипты в одну папку ---
Move-Item stage1_clean.py pipeline\stage1_clean.py
Move-Item stage2_llm_analyze.py pipeline\stage2_llm_analyze.py
Move-Item stage3_benchmark_v3.py pipeline\stage3_benchmark_v3.py
Move-Item incoming_clean_v2.py pipeline\incoming_clean_v2.py
Move-Item build_baseline_table.py pipeline\build_baseline_table.py

# --- эталон (baseline) в отдельную папку ---
Move-Item krisha_astana_baseline.csv baseline\krisha_astana_baseline.csv
Move-Item krisha_astana_baseline.dropped.csv baseline\krisha_astana_baseline.dropped.csv
Move-Item krisha_astana_detail.csv baseline\krisha_astana_detail_snapshot.csv

# --- устаревшее, но в архив, не в мусор ---
Move-Item stage3_benchmark.py archive\stage3_benchmark_old.py
Move-Item 1_krisha_parser\notifications_log.csv archive\notifications_log_old_schema.csv

# --- явный мусор ---
Remove-Item -Recurse -Force etap2
Remove-Item -Recurse -Force mycop
Get-ChildItem -Recurse -Directory -Filter "__pycache__" -Path . | Remove-Item -Recurse -Force

New-Item -ItemType File -Force -Path cache\.gitkeep | Out-Null

Write-Host "Готово. Дальше:"
Write-Host "1) замени orchestrator_v7.py и pipeline\build_baseline_table.py файлами из архива (скачанными ранее)"
Write-Host "2) скопируй .gitignore в корень"
Write-Host "3) когда решишь пушить — git add -A; git commit; git push"
