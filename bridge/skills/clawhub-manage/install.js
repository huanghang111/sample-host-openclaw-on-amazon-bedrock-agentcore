#!/usr/bin/env node
/**
 * Install a ClawHub community skill and restart OpenClaw to load it.
 * Usage: node install.js <skill_name>
 *
 * After successful installation, sends a restart request to the contract
 * server so OpenClaw reloads its skill directory. During restart (~2-4 min),
 * messages fall back to the lightweight agent shim.
 */
const http = require("http");
const { execFileSync } = require("child_process");
const { validateSkillName } = require("./common");

const skillName = validateSkillName(process.argv[2]);

try {
  const output = execFileSync(
    "clawhub",
    ["install", skillName, "--no-input", "--force"],
    { encoding: "utf-8", timeout: 60_000, stdio: ["pipe", "pipe", "pipe"] },
  );
  console.log(`Successfully installed skill: ${skillName}`);
  if (output.trim()) console.log(output.trim());
} catch (err) {
  const stderr = err.stderr?.trim() || err.message;
  console.error(`Failed to install skill "${skillName}": ${stderr}`);
  process.exit(1);
}

// Trigger OpenClaw restart to pick up the new skill.
// Fire-and-forget — don't block the tool response on restart completion.
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
      console.log(`\nOpenClaw is restarting to load the new skill.`);
      console.log(
        "The skill will be available after OpenClaw finishes restarting (~2-4 minutes).",
      );
      console.log(
        "During restart, I can still help you using my built-in capabilities.",
      );
    });
  },
);
req.on("error", (err) => {
  // Non-fatal — skill is installed but OpenClaw won't reload until next session
  console.log(
    `\nNote: Could not trigger OpenClaw restart (${err.message}).`,
  );
  console.log(
    "The skill will be available after the next session restart or idle timeout.",
  );
});
req.end();
