#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
base="app/web/static/vendor"
mkdir -p "$base/bootstrap/css" "$base/bootstrap/js" "$base/bootstrap-icons/font/fonts"
fetch() {
  local url="$1" target="$2" tmp
  tmp="${target}.part"
  echo "下载: $url"
  curl --fail --location --retry 3 --connect-timeout 15 --max-time 120 --silent --show-error "$url" -o "$tmp"
  test -s "$tmp" || { echo "空文件: $url" >&2; exit 1; }
  mv "$tmp" "$target"
}
fetch 'https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css' "$base/bootstrap/css/bootstrap.min.css"
fetch 'https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js' "$base/bootstrap/js/bootstrap.bundle.min.js"
fetch 'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css' "$base/bootstrap-icons/font/bootstrap-icons.css"
fetch 'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/fonts/bootstrap-icons.woff2' "$base/bootstrap-icons/font/fonts/bootstrap-icons.woff2"
fetch 'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/fonts/bootstrap-icons.woff' "$base/bootstrap-icons/font/fonts/bootstrap-icons.woff"
echo '下载完成。文件位于 app/web/static/vendor，Web 页面已改为本地引用。'
