#!/usr/bin/env bash
# Deploy a published image to the EC2 server.
#
#   ./scripts/deploy.sh              # the image CI built for the current commit (sha-<short>)
#   ./scripts/deploy.sh main         # or any tag on ghcr.io/rchacon/pokemaker-api
#   INFRA_DIR=../pokemaker-infra/envs/dev ./scripts/deploy.sh
#
# CI publishes images (see .github/workflows/ci.yml); this script only tells the
# server to switch to one. It uses SSM Run Command, so no SSH, open port or key
# is needed: just AWS credentials allowed to run commands on the instance.
# On the server, `pokemaker-deploy` (installed by pokemaker-infra) pulls the
# image, swaps the container, health-checks it and rolls back on failure.
set -euo pipefail

cd "$(dirname "$0")/.."
INFRA_DIR="${INFRA_DIR:-../pokemaker-infra/envs/prod}"
TAG="${1:-sha-$(git rev-parse --short=7 HEAD)}"
IMAGE="ghcr.io/rchacon/pokemaker-api:$TAG"

if ! docker manifest inspect "$IMAGE" >/dev/null 2>&1; then
  echo "No image $IMAGE on GHCR. Has CI published it? (push to main, or run the CI workflow manually)" >&2
  exit 1
fi

INSTANCE="$(terraform -chdir="$INFRA_DIR" output -raw instance_id)"
REGION="$(terraform -chdir="$INFRA_DIR" output -raw aws_region)"

COMMAND_ID="$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript \
  --comment "Deploy pokemaker-api $TAG" \
  --parameters "commands=[\"/usr/local/bin/pokemaker-deploy $IMAGE\"]" \
  --query Command.CommandId --output text)"

echo "Deploying $IMAGE to $INSTANCE (SSM command $COMMAND_ID)..."
aws ssm wait command-executed --region "$REGION" --command-id "$COMMAND_ID" --instance-id "$INSTANCE" || true

read -r STATUS < <(aws ssm get-command-invocation --region "$REGION" \
  --command-id "$COMMAND_ID" --instance-id "$INSTANCE" --query Status --output text)
aws ssm get-command-invocation --region "$REGION" --command-id "$COMMAND_ID" --instance-id "$INSTANCE" \
  --query '[StandardOutputContent, StandardErrorContent]' --output text

if [[ "$STATUS" != "Success" ]]; then
  echo "Deploy $STATUS. The server kept (or rolled back to) the previous image." >&2
  exit 1
fi
echo "Deployed $IMAGE."
