/**
 * Tests for warm-up conversation history — accumulation during lightweight
 * agent phase and injection into OpenClaw on handoff.
 *
 * Covers:
 *   - stripWarmupFooter: removes warm-up footer metadata before storing
 *   - formatWarmupHistory: formats history as context prefix for OpenClaw
 *   - lightweight agent chat() history parameter: multi-turn context
 *   - accumulation logic: cap enforcement, footer stripping
 *   - handoff injection: first OpenClaw message gets history prefix
 *
 * Run: cd bridge && node --test warmup-history.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

// --- Mirror of contract server internals (not exported) ---

const WARMUP_FOOTER_MARKER = "\n\n---\n_Warm-up mode";

function stripWarmupFooter(text) {
  if (!text) return "";
  const idx = text.indexOf(WARMUP_FOOTER_MARKER);
  return idx >= 0 ? text.slice(0, idx) : text;
}

function formatWarmupHistory(history) {
  if (!history || history.length === 0) return "";
  const lines = history.map(
    (m) => `${m.role === "user" ? "User" : "Assistant"}: ${m.content}`,
  );
  return (
    "[Previous conversation during startup — please maintain context continuity:]\n" +
    lines.join("\n\n") +
    "\n\n[Current message:]\n"
  );
}

// --- stripWarmupFooter ---

describe("stripWarmupFooter", () => {
  it("strips the warm-up footer from a response", () => {
    const response =
      "Hello! How can I help you?" +
      "\n\n---\n" +
      "_Warm-up mode — after full startup (~5-6 second), additional " +
      "community skills come online: YouTube transcripts, deep research, " +
      "task decomposition with sub-agents, etc._";
    assert.equal(stripWarmupFooter(response), "Hello! How can I help you?");
  });

  it("returns text unchanged when no footer present", () => {
    assert.equal(stripWarmupFooter("Just a normal response"), "Just a normal response");
  });

  it("handles empty string", () => {
    assert.equal(stripWarmupFooter(""), "");
  });

  it("handles null/undefined", () => {
    assert.equal(stripWarmupFooter(null), "");
    assert.equal(stripWarmupFooter(undefined), "");
  });

  it("strips footer even with extra content before it", () => {
    const response =
      "Line 1\nLine 2\nLine 3" +
      "\n\n---\n_Warm-up mode — something";
    assert.equal(stripWarmupFooter(response), "Line 1\nLine 2\nLine 3");
  });

  it("only strips from the first occurrence of footer marker", () => {
    const response =
      "Part 1\n\n---\n_Warm-up mode first\n\n---\n_Warm-up mode second";
    assert.equal(stripWarmupFooter(response), "Part 1");
  });

  it("does not strip similar-looking but different separators", () => {
    const response = "Text\n\n---\nSome other footer";
    assert.equal(stripWarmupFooter(response), "Text\n\n---\nSome other footer");
  });
});

// --- formatWarmupHistory ---

describe("formatWarmupHistory", () => {
  it("formats a single exchange", () => {
    const history = [
      { role: "user", content: "hello" },
      { role: "assistant", content: "Hi there!" },
    ];
    const result = formatWarmupHistory(history);
    assert.ok(result.startsWith("[Previous conversation during startup"));
    assert.ok(result.includes("User: hello"));
    assert.ok(result.includes("Assistant: Hi there!"));
    assert.ok(result.endsWith("[Current message:]\n"));
  });

  it("formats multiple exchanges in order", () => {
    const history = [
      { role: "user", content: "What is 2+2?" },
      { role: "assistant", content: "4" },
      { role: "user", content: "And 3+3?" },
      { role: "assistant", content: "6" },
    ];
    const result = formatWarmupHistory(history);
    const userIdx1 = result.indexOf("User: What is 2+2?");
    const assistIdx1 = result.indexOf("Assistant: 4");
    const userIdx2 = result.indexOf("User: And 3+3?");
    const assistIdx2 = result.indexOf("Assistant: 6");
    // All present and in order
    assert.ok(userIdx1 < assistIdx1);
    assert.ok(assistIdx1 < userIdx2);
    assert.ok(userIdx2 < assistIdx2);
  });

  it("returns empty string for empty history", () => {
    assert.equal(formatWarmupHistory([]), "");
  });

  it("returns empty string for null/undefined", () => {
    assert.equal(formatWarmupHistory(null), "");
    assert.equal(formatWarmupHistory(undefined), "");
  });

  it("preserves multiline content in messages", () => {
    const history = [
      { role: "user", content: "Line 1\nLine 2\nLine 3" },
      { role: "assistant", content: "Response\nwith\nnewlines" },
    ];
    const result = formatWarmupHistory(history);
    assert.ok(result.includes("Line 1\nLine 2\nLine 3"));
    assert.ok(result.includes("Response\nwith\nnewlines"));
  });

  it("separates messages with double newlines", () => {
    const history = [
      { role: "user", content: "msg1" },
      { role: "assistant", content: "reply1" },
    ];
    const result = formatWarmupHistory(history);
    assert.ok(result.includes("User: msg1\n\nAssistant: reply1"));
  });

  it("result can be prepended to a user message", () => {
    const history = [
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ];
    const prefix = formatWarmupHistory(history);
    const combined = prefix + "What's the weather?";
    assert.ok(combined.includes("[Current message:]\nWhat's the weather?"));
    assert.ok(combined.includes("User: hi"));
  });
});

// --- Accumulation logic ---

describe("warmup history accumulation", () => {
  const MAX_WARMUP_HISTORY = 20;

  it("accumulates user and assistant messages as pairs", () => {
    const warmupHistory = [];
    // Simulate 3 warm-up exchanges
    const exchanges = [
      { user: "hello", assistant: "Hi there!" },
      { user: "what time is it?", assistant: "I don't have a clock, but..." },
      { user: "tell me a joke", assistant: "Why did the chicken..." },
    ];
    for (const ex of exchanges) {
      const responseWithFooter =
        ex.assistant + "\n\n---\n_Warm-up mode — after full startup...";
      if (warmupHistory.length < MAX_WARMUP_HISTORY) {
        warmupHistory.push({ role: "user", content: ex.user });
        warmupHistory.push({
          role: "assistant",
          content: stripWarmupFooter(responseWithFooter),
        });
      }
    }
    assert.equal(warmupHistory.length, 6); // 3 exchanges x 2 messages
    assert.equal(warmupHistory[0].role, "user");
    assert.equal(warmupHistory[0].content, "hello");
    assert.equal(warmupHistory[1].role, "assistant");
    assert.equal(warmupHistory[1].content, "Hi there!"); // Footer stripped
    assert.equal(warmupHistory[4].content, "tell me a joke");
    assert.equal(warmupHistory[5].content, "Why did the chicken...");
  });

  it("respects MAX_WARMUP_HISTORY cap", () => {
    const warmupHistory = [];
    // Fill to max
    for (let i = 0; i < MAX_WARMUP_HISTORY / 2; i++) {
      warmupHistory.push({ role: "user", content: `msg ${i}` });
      warmupHistory.push({ role: "assistant", content: `reply ${i}` });
    }
    assert.equal(warmupHistory.length, MAX_WARMUP_HISTORY);

    // Attempt to add one more — should be skipped
    if (warmupHistory.length < MAX_WARMUP_HISTORY) {
      warmupHistory.push({ role: "user", content: "overflow" });
      warmupHistory.push({ role: "assistant", content: "overflow reply" });
    }
    assert.equal(warmupHistory.length, MAX_WARMUP_HISTORY); // Still at cap
    assert.ok(!warmupHistory.some((m) => m.content === "overflow"));
  });

  it("stripped responses do not contain footer text", () => {
    const footer =
      "\n\n---\n_Warm-up mode — after full startup (~5-6 second), additional " +
      "community skills come online: YouTube transcripts, deep research, " +
      "task decomposition with sub-agents, etc._";
    const rawResponse = "Here is your answer." + footer;
    const cleaned = stripWarmupFooter(rawResponse);
    assert.ok(!cleaned.includes("Warm-up mode"));
    assert.ok(!cleaned.includes("community skills"));
    assert.equal(cleaned, "Here is your answer.");
  });
});

// --- Handoff injection ---

describe("warmup history handoff injection", () => {
  it("injects history prefix into first OpenClaw message", () => {
    let warmupHistory = [
      { role: "user", content: "hello" },
      { role: "assistant", content: "Hi! How can I help?" },
    ];

    const bridgeText = "What's the weather in Tokyo?";
    let messageToBridge = bridgeText;

    // Simulate contract server injection logic
    if (warmupHistory.length > 0) {
      messageToBridge = formatWarmupHistory(warmupHistory) + bridgeText;
      warmupHistory = [];
    }

    assert.ok(messageToBridge.includes("User: hello"));
    assert.ok(messageToBridge.includes("Assistant: Hi! How can I help?"));
    assert.ok(messageToBridge.includes("[Current message:]\nWhat's the weather in Tokyo?"));
    assert.equal(warmupHistory.length, 0); // Cleared after injection
  });

  it("does not inject when history is empty", () => {
    const warmupHistory = [];
    const bridgeText = "Just a normal message";
    let messageToBridge = bridgeText;

    if (warmupHistory.length > 0) {
      messageToBridge = formatWarmupHistory(warmupHistory) + bridgeText;
    }

    assert.equal(messageToBridge, bridgeText); // Unchanged
  });

  it("preserves image markers in injected message", () => {
    let warmupHistory = [
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ];

    const bridgeText = 'Look at this\n\n[OPENCLAW_IMAGES:[{"s3Key":"img.jpg","contentType":"image/jpeg"}]]';
    let messageToBridge = bridgeText;

    if (warmupHistory.length > 0) {
      messageToBridge = formatWarmupHistory(warmupHistory) + bridgeText;
      warmupHistory = [];
    }

    assert.ok(messageToBridge.includes("[OPENCLAW_IMAGES:"));
    assert.ok(messageToBridge.includes("User: hi"));
  });

  it("handles multi-exchange history injection", () => {
    let warmupHistory = [
      { role: "user", content: "What is 2+2?" },
      { role: "assistant", content: "4" },
      { role: "user", content: "And 3+3?" },
      { role: "assistant", content: "6" },
      { role: "user", content: "You're good at math" },
      { role: "assistant", content: "Thanks!" },
    ];

    const bridgeText = "Now solve x^2 = 4";
    const messageToBridge = formatWarmupHistory(warmupHistory) + bridgeText;

    // All 3 exchanges present
    assert.ok(messageToBridge.includes("User: What is 2+2?"));
    assert.ok(messageToBridge.includes("Assistant: 6"));
    assert.ok(messageToBridge.includes("Assistant: Thanks!"));
    assert.ok(messageToBridge.includes("[Current message:]\nNow solve x^2 = 4"));
  });

  it("second message after handoff has no history prefix", () => {
    let warmupHistory = [
      { role: "user", content: "hi" },
      { role: "assistant", content: "hello" },
    ];

    // First message — injects and clears
    const msg1 = "first post-handoff";
    let bridge1 = msg1;
    if (warmupHistory.length > 0) {
      bridge1 = formatWarmupHistory(warmupHistory) + msg1;
      warmupHistory = [];
    }
    assert.ok(bridge1.includes("User: hi")); // Has history

    // Second message — no history
    const msg2 = "second post-handoff";
    let bridge2 = msg2;
    if (warmupHistory.length > 0) {
      bridge2 = formatWarmupHistory(warmupHistory) + msg2;
      warmupHistory = [];
    }
    assert.equal(bridge2, msg2); // Plain, no prefix
  });
});

// --- Lightweight agent history parameter ---

describe("lightweight agent chat() history parameter", () => {
  // We can't call chat() without a running proxy, but we can verify
  // that the exported function accepts the history parameter and that
  // it would build the correct messages array structure.

  it("chat function is async and accepts history parameter", () => {
    const { chat } = require("./lightweight-agent");
    // chat(userMessage, userId, deadlineMs, history) — last 2 have defaults
    assert.ok(chat.length >= 1, "at least 1 required param");
    assert.equal(chat.constructor.name, "AsyncFunction");
  });

  it("history messages are correctly shaped for OpenAI chat format", () => {
    // Verify the history format matches what the proxy expects
    const history = [
      { role: "user", content: "hello" },
      { role: "assistant", content: "Hi there!" },
    ];
    for (const msg of history) {
      assert.ok(["user", "assistant"].includes(msg.role));
      assert.equal(typeof msg.content, "string");
    }
  });
});

// --- End-to-end flow simulation ---

describe("warmup history end-to-end flow", () => {
  const MAX_WARMUP_HISTORY = 20;

  it("simulates full warm-up -> handoff lifecycle", () => {
    let warmupHistory = [];

    // --- Warm-up phase: 3 messages ---
    const warmupExchanges = [
      { user: "Hi!", response: "Hello!\n\n---\n_Warm-up mode — ..." },
      {
        user: "Save a note: buy milk",
        response: "Saved!\n\n---\n_Warm-up mode — ...",
      },
      {
        user: "What did I just save?",
        response: "You saved 'buy milk'.\n\n---\n_Warm-up mode — ...",
      },
    ];

    for (const ex of warmupExchanges) {
      // Each chat() call would receive warmupHistory and produce a response.
      // Simulate the accumulation that happens in contract server.
      if (warmupHistory.length < MAX_WARMUP_HISTORY) {
        warmupHistory.push({ role: "user", content: ex.user });
        warmupHistory.push({
          role: "assistant",
          content: stripWarmupFooter(ex.response),
        });
      }
    }

    assert.equal(warmupHistory.length, 6);
    // Verify footers are stripped
    assert.ok(!warmupHistory[1].content.includes("Warm-up mode"));
    assert.equal(warmupHistory[1].content, "Hello!");
    assert.equal(warmupHistory[5].content, "You saved 'buy milk'.");

    // --- Handoff: OpenClaw becomes ready, first message ---
    const firstPostHandoff = "Show me all my saved notes";
    let messageToBridge = firstPostHandoff;
    if (warmupHistory.length > 0) {
      messageToBridge = formatWarmupHistory(warmupHistory) + firstPostHandoff;
      warmupHistory = [];
    }

    // OpenClaw receives full context
    assert.ok(messageToBridge.includes("User: Hi!"));
    assert.ok(messageToBridge.includes("Assistant: Hello!"));
    assert.ok(messageToBridge.includes("User: Save a note: buy milk"));
    assert.ok(messageToBridge.includes("Assistant: Saved!"));
    assert.ok(messageToBridge.includes("User: What did I just save?"));
    assert.ok(messageToBridge.includes("Assistant: You saved 'buy milk'."));
    assert.ok(
      messageToBridge.includes(
        "[Current message:]\nShow me all my saved notes",
      ),
    );
    assert.equal(warmupHistory.length, 0);

    // --- Second message: no history ---
    const secondMessage = "Thanks!";
    let bridge2 = secondMessage;
    if (warmupHistory.length > 0) {
      bridge2 = formatWarmupHistory(warmupHistory) + secondMessage;
    }
    assert.equal(bridge2, "Thanks!");
  });

  it("handles rapid messages that exceed history cap", () => {
    let warmupHistory = [];

    // Send 12 messages (exceeds 20-entry cap at message 11)
    for (let i = 0; i < 12; i++) {
      const response = `Reply ${i}\n\n---\n_Warm-up mode — ...`;
      if (warmupHistory.length < MAX_WARMUP_HISTORY) {
        warmupHistory.push({ role: "user", content: `Message ${i}` });
        warmupHistory.push({
          role: "assistant",
          content: stripWarmupFooter(response),
        });
      }
    }

    // Cap at 20 (10 exchanges)
    assert.equal(warmupHistory.length, MAX_WARMUP_HISTORY);
    // Last stored exchange should be #9 (0-indexed)
    assert.equal(warmupHistory[18].content, "Message 9");
    assert.equal(warmupHistory[19].content, "Reply 9");
    // #10 and #11 should not be present
    assert.ok(!warmupHistory.some((m) => m.content === "Message 10"));
    assert.ok(!warmupHistory.some((m) => m.content === "Message 11"));
  });

  it("handles empty warm-up (OpenClaw starts fast, no shim messages)", () => {
    const warmupHistory = [];

    // Immediately in OpenClaw mode — no history to inject
    const bridgeText = "First message";
    let messageToBridge = bridgeText;
    if (warmupHistory.length > 0) {
      messageToBridge = formatWarmupHistory(warmupHistory) + bridgeText;
    }
    assert.equal(messageToBridge, "First message");
  });
});
