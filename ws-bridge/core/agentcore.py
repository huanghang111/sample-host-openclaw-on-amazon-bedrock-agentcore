"""AgentCore Runtime invocation with retry logic. Thread-safe."""

import json
import logging
import time

import botocore.exceptions

logger = logging.getLogger("ws-bridge.agentcore")

MAX_INVOKE_RETRIES = 3
INVOKE_RETRY_DELAYS = [5, 15, 30]  # seconds between retries
MAX_RESPONSE_BYTES = 500_000


class AgentCoreService:
    """Invokes per-user AgentCore Runtime sessions. Thread-safe (stateless per call)."""

    def __init__(self, client, runtime_arn: str, qualifier: str):
        self.client = client
        self.runtime_arn = runtime_arn
        self.qualifier = qualifier

    def invoke(self, session_id: str, user_id: str, actor_id: str,
               message, *, channel: str = "") -> dict:
        """Invoke AgentCore Runtime with retry logic.

        message can be a string or dict (for multimodal messages with images).
        Returns a dict with at least a "response" key.
        """
        if not channel:
            channel = actor_id.split(":")[0] if ":" in actor_id else "unknown"

        payload = json.dumps({
            "action": "chat",
            "userId": user_id,
            "actorId": actor_id,
            "channel": channel,
            "message": message,
        }).encode()

        last_error = None
        for attempt in range(MAX_INVOKE_RETRIES):
            try:
                logger.info("Invoking AgentCore: session=%s user=%s attempt=%d",
                            session_id, user_id, attempt + 1)
                resp = self.client.invoke_agent_runtime(
                    agentRuntimeArn=self.runtime_arn,
                    qualifier=self.qualifier,
                    runtimeSessionId=session_id,
                    runtimeUserId=actor_id,
                    payload=payload,
                    contentType="application/json",
                    accept="application/json",
                )
                body = resp.get("response")
                if body:
                    body_bytes = (body.read(MAX_RESPONSE_BYTES + 1)
                                  if hasattr(body, "read")
                                  else str(body).encode()[:MAX_RESPONSE_BYTES])
                    body_text = body_bytes.decode("utf-8", errors="replace")[:MAX_RESPONSE_BYTES]
                    logger.info("AgentCore response len=%d first200=%s",
                                len(body_text), body_text[:200])
                    try:
                        return json.loads(body_text)
                    except json.JSONDecodeError:
                        return {"response": body_text}
                return {"response": "No response from agent."}
            except (botocore.exceptions.ConnectionClosedError, ConnectionResetError) as e:
                last_error = e
                if attempt < MAX_INVOKE_RETRIES - 1:
                    delay = INVOKE_RETRY_DELAYS[attempt]
                    logger.warning("AgentCore connection reset (attempt %d/%d), retrying in %ds: %s",
                                   attempt + 1, MAX_INVOKE_RETRIES, delay, e)
                    time.sleep(delay)
                else:
                    logger.error("AgentCore invocation failed after %d attempts: %s",
                                 MAX_INVOKE_RETRIES, e, exc_info=True)
            except Exception as e:
                logger.error("AgentCore invocation failed: %s", e, exc_info=True)
                return {"response": "Sorry, I'm having trouble right now. Please try again later."}

        return {"response": "Sorry, I'm having trouble right now. Please try again later."}
