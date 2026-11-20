#!/usr/bin/env bash
# Draait op een bestaande versietag. Een herhaling vervangt alleen de twee eigen bijlagen.
set -euo pipefail

api="$GITHUB_SERVER_URL/api/v1/repos/$GITHUB_REPOSITORY/releases"
antwoord="$RUNNER_TEMP/release.json"
auth=(-H "Authorization: token $RELEASE_TOKEN")
json=(-H 'Content-Type: application/json')
status=$(curl -sS "${auth[@]}" -o "$antwoord" -w '%{http_code}' "$api/tags/$GITHUB_REF_NAME")
case "$status" in
  200) ;;
  404)
    jq -n --arg tag "$GITHUB_REF_NAME" --rawfile tekst CHANGELOG.md \
      '{tag_name: $tag, name: ("zoeken-en-persoonlijke-ai " + $tag), body: $tekst, draft: true}' |
      curl -fsS "${auth[@]}" "${json[@]}" --data-binary @- "$api" -o "$antwoord"
    ;;
  *) echo "Forgejo gaf HTTP $status bij het opzoeken van de release" >&2; exit 1 ;;
esac

release_id=$(jq -er '.id | numbers' "$antwoord")
bijlagen="$api/$release_id/assets"
for naam in digests.json sbom.spdx.json; do
  oude_ids=$(jq -r --arg naam "$naam" '.assets[]? | select(.name == $naam) | .id' "$antwoord")
  curl -fsS "${auth[@]}" -F "attachment=@release/$naam;type=application/json" "$bijlagen" |
    jq -e --arg naam "$naam" '.id and .name == $naam' > /dev/null
  for id in $oude_ids; do
    curl -fsS "${auth[@]}" -X DELETE "$bijlagen/$id" -o /dev/null
  done
done
if jq -e '.draft' "$antwoord" > /dev/null; then
  curl -fsS "${auth[@]}" "${json[@]}" -X PATCH -d '{"draft":false}' "$api/$release_id" -o /dev/null
fi
