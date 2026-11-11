#!/bin/bash
# Install poppler-utils (pdftotext). On 2026-11-11 GitHub's runners could not
# resolve azure.archive.ubuntu.com for over an hour and every job that installs
# poppler failed (daily collection, tests). After a failed try, point apt at
# archive.ubuntu.com instead of the runner's Azure mirror, and retry.
set -u
for attempt in 1 2 3; do
  if sudo apt-get update -qq && sudo apt-get install -y -qq poppler-utils; then
    exit 0
  fi
  echo "::warning::installing poppler-utils failed (attempt $attempt)"
  if [ "$attempt" = 1 ]; then
    echo "pointing apt at archive.ubuntu.com"
    if [ -f /etc/apt/apt-mirrors.txt ]; then
      printf 'http://archive.ubuntu.com/ubuntu/\tpriority:1\nhttp://security.ubuntu.com/ubuntu/\tpriority:2\n' \
        | sudo tee /etc/apt/apt-mirrors.txt > /dev/null
    fi
    sudo sed -i 's#http://azure.archive.ubuntu.com/ubuntu#http://archive.ubuntu.com/ubuntu#g' \
      /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true
  fi
  sleep $((attempt * 15))
done
exit 1
