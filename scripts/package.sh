#!/usr/bin/env bash
#
# Package the ZAF app bundle into a Zendesk-installable zip.
#
# There is NO transpile or build step. This zips the committed files under
# zaf-build/ exactly as they are: what you edit is what ships. The version is
# read from zaf-build/manifest.json so the zip name always matches the manifest.
#
# Output: dist/tsanet-connect-v<version>.zip
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE="$ROOT/zaf-build"
DIST="$ROOT/dist"

if [ ! -f "$BUNDLE/manifest.json" ]; then
  echo "error: $BUNDLE/manifest.json not found" >&2
  exit 1
fi

VERSION="$(python3 -c "import json; print(json.load(open('$BUNDLE/manifest.json'))['version'])")"
ZIP="$DIST/tsanet-connect-v${VERSION}.zip"

# Locations whose icon IS the thing the user clicks: without assets/icon_<loc>.svg
# Zendesk renders nothing and the location is silently dead. v1.0.49 and v1.0.50
# both shipped a nav_bar with no icon, so the deploy screen was unreachable on
# every install while the manifest, the zip and the app code all looked correct
# (tsanetgit/Zendesk_App#131). ticket_sidebar is deliberately absent: it renders
# without one.
for loc in nav_bar top_bar ticket_editor; do
  declared="$(python3 -c "
import json
m = json.load(open('$BUNDLE/manifest.json'))
print('yes' if '$loc' in m.get('location', {}).get('support', {}) else 'no')")"
  if [ "$declared" = "yes" ] && [ ! -f "$BUNDLE/assets/icon_${loc}.svg" ]; then
    echo "error: manifest declares the '$loc' location but assets/icon_${loc}.svg is missing." >&2
    echo "       Zendesk will not render the location without it." >&2
    exit 1
  fi
done

mkdir -p "$DIST"
rm -f "$ZIP"

# Zendesk expects manifest.json at the zip root, alongside assets/ and
# translations/. README.md is repo documentation and is intentionally excluded.
( cd "$BUNDLE" && zip -r -X "$ZIP" manifest.json assets translations -x '*.DS_Store' >/dev/null )

echo "✓ Packaged $ZIP"
