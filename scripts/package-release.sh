#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || "$1" != /* || "$1" != *.zip ]]; then
  echo "Usage: bash scripts/package-release.sh /absolute/path/product-source.zip" >&2
  exit 2
fi

output_path="$1"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing to package a dirty working tree. Commit the verified release first." >&2
  exit 3
fi

blocked="$(git ls-files | rg '(^|/)(\.env($|\.)|\.dev\.vars$|node_modules/|\.sites-runtime/|\.wrangler/|dist/|\.next/|__pycache__/|\.pytest_cache/|\.ruff_cache/|.*\.(pem|key|p12|pfx|sqlite|sqlite3|db|bak|dump)$)' || true)"
allowed_examples="$(printf '%s\n' "$blocked" | rg '(^|/)\.env(\.droplet)?\.example$' || true)"
blocked="$(comm -23 <(printf '%s\n' "$blocked" | sort) <(printf '%s\n' "$allowed_examples" | sort) || true)"
if [[ -n "$blocked" ]]; then
  echo "Refusing to package blocked files:" >&2
  printf '%s\n' "$blocked" >&2
  exit 4
fi

# Example files must never contain usable high-risk credentials. Print only
# filenames on failure so a CI log cannot become a second disclosure channel.
example_failures="$(git ls-files '*env*.example' | while IFS= read -r file; do
  [[ -f "$file" ]] || continue
  if awk -F= '
    /^[[:space:]]*(LICENSE_SIGNING_PRIVATE_KEY|CREDENTIAL_MASTER_KEY|PHONE_HASH_KEY|CALL_WORKER_TOKEN|WORKER_CONFIG_KEY|LIVEKIT_API_KEY|LIVEKIT_API_SECRET|OPENAI_API_KEY|GROQ_API_KEY|ANTHROPIC_API_KEY|ELEVENLABS_API_KEY|DEEPGRAM_API_KEY)[[:space:]]*=/ {
      value=$0; sub(/^[^=]*=/, "", value); gsub(/^[[:space:]\047\042]+|[[:space:]\047\042]+$/, "", value)
      lower=tolower(value)
      if (value != "" && lower !~ /^(change[-_]?me|replace[-_]|example|placeholder|your[-_]|__[a-z0-9_]+__)/) bad=1
    }
    END { exit bad ? 0 : 1 }
  ' "$file"; then
    printf '%s\n' "$file"
  fi
done)"
if [[ -n "$example_failures" ]]; then
  echo "Refusing to package example files containing non-placeholder secrets:" >&2
  printf '%s\n' "$example_failures" >&2
  exit 5
fi

secret_failures="$(python3 - <<'PY'
from pathlib import Path
import re
import subprocess

paths = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
token_patterns = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9])gh[opusr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
)
obvious_fixture = re.compile(rb"(?i)(test|fake|example|placeholder|abcdefghijklmnopqrstuvwxyz)")
failed = set()
for raw_path in paths:
    if not raw_path:
        continue
    path = Path(raw_path.decode())
    try:
        data = path.read_bytes()
    except OSError:
        continue
    if b"\0" in data[:8192]:
        continue
    for pattern in token_patterns:
        for match in pattern.finditer(data):
            if not obvious_fixture.search(match.group(0)):
                failed.add(str(path))
for path in sorted(failed):
    print(path)
PY
)"
if [[ -n "$secret_failures" ]]; then
  echo "Refusing to package files with possible embedded credentials:" >&2
  printf '%s\n' "$secret_failures" >&2
  exit 6
fi

mkdir -p "$(dirname "$output_path")"
git archive --format=zip --output="$output_path" HEAD
unzip -t "$output_path" >/dev/null

archive_entries="$(unzip -Z1 "$output_path")"
archive_blocked="$(printf '%s\n' "$archive_entries" | rg '(^|/)(\.env($|\.)|\.dev\.vars$|node_modules/|\.git/|dist/|\.next/|__pycache__/|\.pytest_cache/|\.ruff_cache/|.*\.(pem|key|p12|pfx|sqlite|sqlite3|db|bak|dump)$)' || true)"
archive_examples="$(printf '%s\n' "$archive_blocked" | rg '(^|/)\.env(\.droplet)?\.example$' || true)"
archive_blocked="$(comm -23 <(printf '%s\n' "$archive_blocked" | sort) <(printf '%s\n' "$archive_examples" | sort) || true)"
if [[ -n "$archive_blocked" ]]; then
  echo "Packaged archive contains blocked paths:" >&2
  printf '%s\n' "$archive_blocked" >&2
  exit 7
fi
echo "$output_path"
