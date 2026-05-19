/**
 * Tests for OpenClaw Gateway protocol v4 chat-event handling.
 * Run: node --test protocol-v4.test.js
 *
 * Mirrors the delta/replace/final accumulation logic from agentcore-contract.js
 * (`bridgeMessage` ws.on("message") handler). PR openclaw#80725 added optional
 * `deltaText` (additive increment) alongside the legacy `message` field
 * (cumulative snapshot). Replace frames fully overwrite the accumulator.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

function extractTextFromContent(content) {
  if (!content) return "";
  if (Array.isArray(content)) {
    return content.filter((b) => b.type === "text").map((b) => b.text).join("");
  }
  if (typeof content === "string") return content;
  if (typeof content === "object" && typeof content.text === "string") return content.text;
  return "";
}

function extractFromPayload(pl) {
  return (
    extractTextFromContent(pl.message?.content) ||
    extractTextFromContent(pl.message) ||
    extractTextFromContent(pl.text) ||
    extractTextFromContent(pl.content)
  );
}

// Mirror of the chat-event accumulation in bridgeMessage().
function applyChatEvent(state, msg) {
  if (msg.type !== "event" || msg.event !== "chat") return state;
  const payload = msg.payload || {};

  if (payload.state === "delta") {
    if (typeof payload.deltaText === "string" && payload.deltaText.length > 0) {
      return { ...state, responseText: state.responseText + payload.deltaText };
    }
    const text = extractFromPayload(payload);
    if (text) return { ...state, responseText: text };
    return state;
  }

  if (payload.state === "replace") {
    const text = extractFromPayload(payload);
    if (text) return { ...state, responseText: text };
    return state;
  }

  if (payload.state === "final") {
    const text = extractFromPayload(payload);
    return { ...state, responseText: text || state.responseText, final: true };
  }

  return state;
}

describe("v4 delta — additive deltaText", () => {
  it("appends successive deltaText chunks", () => {
    let s = { responseText: "" };
    s = applyChatEvent(s, { type: "event", event: "chat", payload: { state: "delta", deltaText: "Hello" } });
    s = applyChatEvent(s, { type: "event", event: "chat", payload: { state: "delta", deltaText: ", " } });
    s = applyChatEvent(s, { type: "event", event: "chat", payload: { state: "delta", deltaText: "world!" } });
    assert.equal(s.responseText, "Hello, world!");
  });

  it("ignores empty deltaText", () => {
    let s = { responseText: "abc" };
    s = applyChatEvent(s, { type: "event", event: "chat", payload: { state: "delta", deltaText: "" } });
    assert.equal(s.responseText, "abc");
  });
});

describe("v3 backward-compat — cumulative message snapshots", () => {
  it("replaces accumulator with cumulative message string", () => {
    let s = { responseText: "" };
    s = applyChatEvent(s, { type: "event", event: "chat", payload: { state: "delta", message: "Hello" } });
    s = applyChatEvent(s, { type: "event", event: "chat", payload: { state: "delta", message: "Hello, world!" } });
    assert.equal(s.responseText, "Hello, world!");
  });

  it("extracts cumulative content blocks", () => {
    let s = { responseText: "" };
    s = applyChatEvent(s, {
      type: "event",
      event: "chat",
      payload: { state: "delta", message: { content: [{ type: "text", text: "partial" }] } },
    });
    assert.equal(s.responseText, "partial");
  });
});

describe("v4 — deltaText takes precedence over cumulative message when both present", () => {
  it("uses deltaText additive path even when cumulative message also present", () => {
    let s = { responseText: "Hello" };
    // Server may include both: deltaText (incremental) + message (cumulative).
    // We prefer deltaText to avoid double-counting.
    s = applyChatEvent(s, {
      type: "event",
      event: "chat",
      payload: { state: "delta", deltaText: ", world!", message: "Hello, world!" },
    });
    assert.equal(s.responseText, "Hello, world!");
  });
});

describe("v4 replace frame", () => {
  it("overwrites accumulator with replace payload", () => {
    let s = { responseText: "stale partial" };
    s = applyChatEvent(s, {
      type: "event",
      event: "chat",
      payload: { state: "replace", message: "fresh content" },
    });
    assert.equal(s.responseText, "fresh content");
  });
});

describe("final frame", () => {
  it("captures complete cumulative final text", () => {
    let s = { responseText: "Hello" };
    s = applyChatEvent(s, {
      type: "event",
      event: "chat",
      payload: { state: "final", message: "Hello, world! [end]" },
    });
    assert.equal(s.responseText, "Hello, world! [end]");
    assert.equal(s.final, true);
  });

  it("preserves accumulator if final has no content", () => {
    let s = { responseText: "Hello, world!" };
    s = applyChatEvent(s, { type: "event", event: "chat", payload: { state: "final" } });
    assert.equal(s.responseText, "Hello, world!");
    assert.equal(s.final, true);
  });
});

describe("connect frame schema — protocol v4 negotiation", () => {
  it("contract.js requests minProtocol=4 and maxProtocol=4", () => {
    const fs = require("fs");
    const path = require("path");
    const src = fs.readFileSync(path.join(__dirname, "agentcore-contract.js"), "utf8");
    assert.match(src, /minProtocol:\s*4/, "contract should pin minProtocol to 4");
    assert.match(src, /maxProtocol:\s*4/, "contract should pin maxProtocol to 4");
  });
});
