---
name: agentcore-gateway
description: Discover and invoke tools on AgentCore Gateway via MCP protocol. Always call gateway_list_tools first to see what tools are available, then call gateway_call_tool to execute a specific tool. Authentication is handled automatically via IAM (SigV4).
allowed-tools: Bash(node:*)
---

# AgentCore Gateway

Discover and invoke external API tools registered on AgentCore Gateway. The Gateway manages authentication (API keys, OAuth, IAM) for all backend APIs — you never handle credentials directly.

## Important

**Always call gateway_list_tools first** before calling any tool. The available tools depend on what targets are configured on the Gateway and may change over time.

## Usage

### gateway_list_tools

List all available tools on the Gateway.

```bash
node {baseDir}/list.js
```

Returns a JSON array of tools with name, description, and inputSchema.

### gateway_call_tool

Call a specific tool on the Gateway.

```bash
node {baseDir}/call.js <tool_name> '<json_arguments>'
```

- `tool_name` (required): The full tool name from gateway_list_tools (e.g. `openweathermap-current___getCurrentWeather`)
- `json_arguments` (required): JSON string of tool arguments matching the tool's inputSchema

## From Agent Chat

- "What tools are available on the gateway?" → gateway_list_tools
- "What's the weather in Beijing?" → gateway_list_tools first, then gateway_call_tool with the weather tool
- "Search for repos about AI" → gateway_list_tools first, then gateway_call_tool with the appropriate tool
