#!/usr/bin/env bash
# SparkReel 端對端示範腳本
# 1) 產生自足的示範直播樣本  2) 執行分析並輸出短片  3) 提示啟動 Web UI
set -euo pipefail
cd "$(dirname "$0")/.."

echo "▶ [1/3] 產生示範直播樣本（影片 + 彈幕 + 字幕）…"
if command -v sparkreel >/dev/null 2>&1; then
  sparkreel make-sample --out examples
else
  PYTHONPATH=src python3 examples/make_sample.py --out examples
fi

echo "▶ [2/3] 執行高光偵測與自動剪輯…"
if command -v sparkreel >/dev/null 2>&1; then
  sparkreel analyze examples/sample_stream.mp4 \
    --chat examples/sample_chat.jsonl \
    --subtitles examples/sample_stream.srt
else
  PYTHONPATH=src python3 -m sparkreel.cli analyze examples/sample_stream.mp4 \
    --chat examples/sample_chat.jsonl --subtitles examples/sample_stream.srt
fi

echo
echo "▶ [3/3] 完成！輸出在 assets/output/ 下。"
echo "    啟動網頁 Live Demo： sparkreel serve   →  http://127.0.0.1:9999"
