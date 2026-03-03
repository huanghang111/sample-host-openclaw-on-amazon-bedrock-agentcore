#!/usr/bin/env node
/**
 * Uninstall a ClawHub community skill.
 * Usage: node uninstall.js <skill_name>
 */
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
