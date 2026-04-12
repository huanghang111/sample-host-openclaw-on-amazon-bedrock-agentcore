#!/usr/bin/env node
/**
 * gateway_list_tools — List all available tools on AgentCore Gateway.
 * Usage: node list.js
 */
const { gatewayRequest } = require("./common");

async function main() {
  const resp = await gatewayRequest({
    jsonrpc: "2.0",
    id: "list-tools",
    method: "tools/list",
    params: {},
  });

  if (resp.error) {
    console.error(`Gateway error: ${resp.error.message}`);
    process.exit(1);
  }

  const tools = resp.result?.tools || [];
  if (tools.length === 0) {
    console.log("No tools available on the gateway.");
    return;
  }

  for (const t of tools) {
    console.log(`\n## ${t.name}`);
    console.log(t.description || "(no description)");
    if (t.inputSchema?.properties) {
      const props = t.inputSchema.properties;
      const required = t.inputSchema.required || [];
      console.log("Parameters:");
      for (const [k, v] of Object.entries(props)) {
        const req = required.includes(k) ? " (required)" : "";
        console.log(`  - ${k}: ${v.description || v.type || "any"}${req}`);
      }
    }
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
