#!/usr/bin/env node
/**
 * Uninstall a ClawHub community skill and restart OpenClaw to unload it.
 * Usage: node uninstall.js <skill_name>
 *
 * After successful uninstallation, sends a restart request to the contract
 * server so OpenClaw reloads its skill directory without the removed skill.
 */
const http = require("http");
const { execFileSync } = require("child_process");
const { validateSkillName } = require("./common");

const skillName = validateSkillName(process.argv[2]);

try {
  const output = execFileSync(
    "clawhub",
    ["uninstall", skillName, "--no-input"],
    { encoding: "utf-8", timeout: 30_000, stdio: ["pipe", "pipe", "pipe"] },
  );
  console.log(`Successfully uninstalled skill: ${skillName}`);
  if (output.trim()) console.log(output.trim());
} catch (err) {
  const stderr = err.stderr?.trim() || err.message;
  console.error(`Failed to uninstall skill "${skillName}": ${stderr}`);
  process.exit(1);
}

// Trigger OpenClaw restart to unload the removed skill.
const req = http.request(
  {
    hostname: "127.0.0.1",
    port: 8080,
    path: "/internal/restart-openclaw",
    method: "POST",
    headers: { "Content-Length": "0" },
    timeout: 5000,
  },
  (res) => {
    let body = "";
    res.on("data", (chunk) => (body += chunk));
    res.on("end", () => {
      console.log(`\nOpenClaw is restarting to unload the skill.`);
      console.log(
        "The change will take effect after OpenClaw finishes restarting (~2-4 minutes).",
      );
    });
  },
);
req.on("error", (err) => {
  console.log(
    `\nNote: Could not trigger OpenClaw restart (${err.message}).`,
  );
  console.log(
    "The skill will be unloaded after the next session restart or idle timeout.",
  );
});
req.end();
