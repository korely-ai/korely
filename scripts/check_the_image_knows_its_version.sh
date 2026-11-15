#!/usr/bin/env bash
# The release image has to report the version, not "unknown".
#
# The unit test for this passes either way: it imports the package from the
# repository, where the VERSION file is always next door. In the image only what
# the Dockerfile copies exists, and the first version of that Dockerfile did not
# copy it. So the number worked everywhere except the one place anybody asks for
# it, which is a running install.
set -euo pipefail

IMG="${1:-korely-agent:versioncheck}"
here="$(cd "$(dirname "$0")/.." && pwd)"
want="$(cat "$here/VERSION")"

docker build -q -t "$IMG" "$here" >/dev/null
got="$(docker run --rm --entrypoint python "$IMG" -c \
  'import korely_agent; print(korely_agent.__version__)')"

if [ "$got" != "$want" ]; then
  echo "the image says '$got', the repository says '$want'"
  echo "(is VERSION copied in the Dockerfile?)"
  exit 1
fi
echo "the image reports $got"
