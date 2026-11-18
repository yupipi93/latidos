#!/usr/bin/env bash
# Turn a demo/record.mjs recording into a README-sized GIF.
# Usage: demo/gif.sh <framesdir> <out.gif>
# Env: WIDTH=900 FPS=12 COLORS=128 CROP=iw:ih:0:0 START=0 DUR=11 DENOISE=3:3:6:6
set -euo pipefail
dir=${1:?frames dir}; out=${2:?output gif}
WIDTH=${WIDTH:-900}; FPS=${FPS:-12}; COLORS=${COLORS:-128}; CROP=${CROP:-iw:ih:0:0}
START=${START:-0}; DUR=${DUR:-11}; DENOISE=${DENOISE:-3:3:6:6}
ffmpeg -v error -y -f concat -i "$dir/frames.txt" -ss "$START" -t "$DUR" -vf \
  "fps=$FPS,crop=$CROP,hqdn3d=$DENOISE,scale=$WIDTH:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=$COLORS:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
  "$out"
ls -la "$out" | awk '{printf "%s %.1f MB\n", $NF, $5/1048576}'
