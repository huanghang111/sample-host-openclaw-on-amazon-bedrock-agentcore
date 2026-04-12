#!/usr/bin/env bash
# scripts/manage-gateway-targets.sh — Add sample targets to AgentCore Gateway.
#
# Usage:
#   ./scripts/manage-gateway-targets.sh add weather <OPENWEATHERMAP_API_KEY>
#   ./scripts/manage-gateway-targets.sh add football <FOOTBALL_DATA_API_KEY>
#
# Prerequisites: Gateway must be deployed (cdk deploy OpenClawGateway).

set -euo pipefail

REGION="${CDK_DEFAULT_REGION:-us-west-2}"

# --- Resolve Gateway ID from CloudFormation ---
get_gateway_id() {
  aws cloudformation describe-stacks \
    --stack-name OpenClawGateway \
    --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" \
    --output text --region "$REGION"
}

# --- Create or update API key credential provider ---
ensure_credential_provider() {
  local name="$1" key="$2"
  if aws bedrock-agentcore-control get-api-key-credential-provider \
    --name "$name" --region "$REGION" &>/dev/null; then
    echo "Updating credential provider: $name" >&2
    aws bedrock-agentcore-control update-api-key-credential-provider \
      --name "$name" --api-key "$key" --region "$REGION" --output json | jq -r .credentialProviderArn
  else
    echo "Creating credential provider: $name" >&2
    aws bedrock-agentcore-control create-api-key-credential-provider \
      --name "$name" --api-key "$key" --region "$REGION" --output json | jq -r .credentialProviderArn
  fi
}

# --- Add target to gateway ---
add_target() {
  local gw_id="$1" name="$2" desc="$3" schema="$4" cred_arn="$5" cred_param="$6" cred_location="$7"

  # Check if target already exists
  local existing
  existing=$(aws bedrock-agentcore-control list-gateway-targets \
    --gateway-id "$gw_id" --region "$REGION" --output json \
    | jq -r ".items[]? | select(.name==\"$name\") | .targetId")

  if [[ -n "$existing" ]]; then
    echo "Target '$name' already exists (id: $existing). Skipping."
    return
  fi

  echo "Creating target: $name"
  aws bedrock-agentcore-control create-gateway-target \
    --gateway-id "$gw_id" \
    --name "$name" \
    --description "$desc" \
    --target-configuration "{\"mcp\":{\"openApiSchema\":{\"inlinePayload\":$(echo "$schema" | jq -Rs .)}}}" \
    --credential-provider-configurations "[{\"credentialProviderType\":\"API_KEY\",\"credentialProvider\":{\"apiKeyCredentialProvider\":{\"providerArn\":\"$cred_arn\",\"credentialParameterName\":\"$cred_param\",\"credentialLocation\":\"$cred_location\"}}}]" \
    --region "$REGION" --output json | jq '{targetId: .targetId, name: .name, status: .status}'
}

# --- Sample schemas ---
WEATHER_SCHEMA='{
  "openapi": "3.0.3",
  "info": { "title": "OpenWeatherMap API", "version": "2.5" },
  "servers": [{ "url": "https://api.openweathermap.org/data/2.5" }],
  "paths": {
    "/weather": {
      "get": {
        "operationId": "getCurrentWeather",
        "summary": "Get current weather data for a location",
        "parameters": [
          { "name": "q", "in": "query", "description": "City name (e.g. London,uk)", "schema": { "type": "string" } },
          { "name": "lat", "in": "query", "schema": { "type": "string" } },
          { "name": "lon", "in": "query", "schema": { "type": "string" } },
          { "name": "units", "in": "query", "schema": { "type": "string", "enum": ["standard","metric","imperial"], "default": "metric" } },
          { "name": "lang", "in": "query", "schema": { "type": "string", "default": "en" } }
        ],
        "responses": { "200": { "description": "Current weather data" } }
      }
    }
  }
}'

FOOTBALL_SCHEMA='{
  "openapi": "3.0.3",
  "info": { "title": "Football-Data.org API", "version": "4.0" },
  "servers": [{ "url": "https://api.football-data.org/v4" }],
  "paths": {
    "/competitions": { "get": { "operationId": "listCompetitions", "summary": "List all competitions", "responses": { "200": { "description": "OK" } } } },
    "/competitions/{id}/standings": { "get": { "operationId": "getStandings", "summary": "Get standings for a competition", "parameters": [{ "name": "id", "in": "path", "required": true, "description": "Competition id or code (PL, CL, BL1, SA, PD, FL1)", "schema": { "type": "string" } }, { "name": "season", "in": "query", "schema": { "type": "string" } }], "responses": { "200": { "description": "OK" } } } },
    "/competitions/{id}/matches": { "get": { "operationId": "getCompetitionMatches", "summary": "Get matches for a competition", "parameters": [{ "name": "id", "in": "path", "required": true, "schema": { "type": "string" } }, { "name": "matchday", "in": "query", "schema": { "type": "integer" } }, { "name": "status", "in": "query", "schema": { "type": "string" } }, { "name": "dateFrom", "in": "query", "schema": { "type": "string" } }, { "name": "dateTo", "in": "query", "schema": { "type": "string" } }], "responses": { "200": { "description": "OK" } } } },
    "/competitions/{id}/scorers": { "get": { "operationId": "getScorers", "summary": "Get top scorers", "parameters": [{ "name": "id", "in": "path", "required": true, "schema": { "type": "string" } }, { "name": "limit", "in": "query", "schema": { "type": "integer" } }], "responses": { "200": { "description": "OK" } } } },
    "/competitions/{id}/teams": { "get": { "operationId": "getCompetitionTeams", "summary": "Get teams in a competition", "parameters": [{ "name": "id", "in": "path", "required": true, "schema": { "type": "string" } }], "responses": { "200": { "description": "OK" } } } },
    "/matches": { "get": { "operationId": "listMatches", "summary": "List matches (defaults to today)", "parameters": [{ "name": "dateFrom", "in": "query", "schema": { "type": "string" } }, { "name": "dateTo", "in": "query", "schema": { "type": "string" } }, { "name": "status", "in": "query", "schema": { "type": "string" } }], "responses": { "200": { "description": "OK" } } } },
    "/matches/{id}": { "get": { "operationId": "getMatch", "summary": "Get match details", "parameters": [{ "name": "id", "in": "path", "required": true, "schema": { "type": "integer" } }], "responses": { "200": { "description": "OK" } } } },
    "/teams/{id}": { "get": { "operationId": "getTeam", "summary": "Get team details", "parameters": [{ "name": "id", "in": "path", "required": true, "schema": { "type": "integer" } }], "responses": { "200": { "description": "OK" } } } },
    "/teams/{id}/matches": { "get": { "operationId": "getTeamMatches", "summary": "Get team matches", "parameters": [{ "name": "id", "in": "path", "required": true, "schema": { "type": "integer" } }, { "name": "dateFrom", "in": "query", "schema": { "type": "string" } }, { "name": "dateTo", "in": "query", "schema": { "type": "string" } }, { "name": "limit", "in": "query", "schema": { "type": "integer" } }], "responses": { "200": { "description": "OK" } } } },
    "/persons/{id}": { "get": { "operationId": "getPerson", "summary": "Get player/coach details", "parameters": [{ "name": "id", "in": "path", "required": true, "schema": { "type": "integer" } }], "responses": { "200": { "description": "OK" } } } }
  }
}'

# --- Main ---
ACTION="${1:-}"
TARGET="${2:-}"
API_KEY="${3:-}"

case "$ACTION" in
  add)
    [[ -z "$TARGET" || -z "$API_KEY" ]] && { echo "Usage: $0 add <weather|football> <API_KEY>"; exit 1; }
    GW_ID=$(get_gateway_id)
    echo "Gateway: $GW_ID"

    case "$TARGET" in
      weather)
        CRED_ARN=$(ensure_credential_provider "openweathermap-api-key" "$API_KEY")
        add_target "$GW_ID" "openweathermap-current" "OpenWeatherMap current weather API" \
          "$WEATHER_SCHEMA" "$CRED_ARN" "appid" "QUERY_PARAMETER"
        ;;
      football)
        CRED_ARN=$(ensure_credential_provider "football-data-api-key" "$API_KEY")
        add_target "$GW_ID" "football-data-v4" "Football-data.org API v4" \
          "$FOOTBALL_SCHEMA" "$CRED_ARN" "X-Auth-Token" "HEADER"
        ;;
      *)
        echo "Unknown target: $TARGET (available: weather, football)"
        exit 1
        ;;
    esac
    echo "Done."
    ;;
  list)
    GW_ID=$(get_gateway_id)
    aws bedrock-agentcore-control list-gateway-targets \
      --gateway-id "$GW_ID" --region "$REGION" --output table \
      --query "items[].{Name:name,Status:status,ID:targetId}"
    ;;
  *)
    echo "Usage:"
    echo "  $0 add weather <OPENWEATHERMAP_API_KEY>"
    echo "  $0 add football <FOOTBALL_DATA_API_KEY>"
    echo "  $0 list"
    exit 1
    ;;
esac
