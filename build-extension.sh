#!/bin/bash
set -e

OUT="fpl-gaffer-extension.zip"
rm -f "$OUT"

cd extension
zip -r "../$OUT" . \
  --exclude "*.DS_Store" \
  --exclude "__MACOSX/*" \
  --exclude "*.map"
cd ..

echo "Built: $OUT ($(du -sh "$OUT" | cut -f1))"
echo ""
echo "Upload at: https://chrome.google.com/webstore/devconsole"
