#!/usr/bin/env node
/**
 * gateway_call_tool — Call a specific tool on AgentCore Gateway.
 * Usage: node call.js <tool_name> '<json_arguments>'
 */
const { gatewayRequest } = require("./common");

async function main() {
  const toolName = process.argv[2];
  const argsJson = process.argv[3] || "{}";

  if (!toolName) {
    console.error("Usage: node call.js <tool_name> '<json_arguments>'");
    process.exit(1);
  }

  let args;
  try {
    args = JSON.parse(argsJson);
  } catch {
    console.error(`Error: invalid JSON arguments: ${argsJson}`);
    process.exit(1);
  }

  const resp = await gatewayRequest({
    jsonrpc: "2.0",
    id: "call-tool",
    method: "tools/call",
    params: { name: toolName, arguments: args },
  });

  if (resp.error) {
    console.error(`Gateway error: ${resp.error.message}`);
    process.exit(1);
  }

  const content = resp.result?.content || [];
  for (const block of content) {
    if (block.type === "text") console.log(block.text);
    else console.log(JSON.stringify(block));
  }

  if (resp.result?.isError) process.exit(1);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
