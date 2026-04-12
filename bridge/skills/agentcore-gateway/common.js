/**
 * common.js — SigV4-signed HTTP requests to AgentCore Gateway.
 *
 * Uses the container's default credential chain (execution role).
 * DNS workaround: resolves gateway hostname via VPC DNS (169.254.169.253)
 * and connects via IP + SNI to bypass container DNS limitations.
 */
const https = require("https");
const dns = require("dns");
const crypto = require("crypto");
const { fromNodeProviderChain } = require("@aws-sdk/credential-providers");

const GATEWAY_URL = process.env.AGENTCORE_GATEWAY_URL;
const REGION = process.env.AWS_REGION || "us-west-2";

function validateGatewayUrl() {
  if (!GATEWAY_URL) {
    console.error("Error: AGENTCORE_GATEWAY_URL environment variable is not set.");
    process.exit(1);
  }
}

/** SHA-256 hex digest */
function sha256(data) {
  return crypto.createHash("sha256").update(data, "utf8").digest("hex");
}

/** HMAC-SHA256 */
function hmac(key, data) {
  return crypto.createHmac("sha256", key).update(data, "utf8").digest();
}

/** Resolve hostname via VPC DNS (169.254.169.253), fallback to system DNS */
function resolveHostname(hostname) {
  return new Promise((resolve) => {
    const resolver = new dns.Resolver();
    resolver.setServers(["169.254.169.253"]);
    resolver.resolve4(hostname, (err, addrs) => {
      if (!err && addrs && addrs.length > 0) {
        resolve(addrs[0]);
      } else {
        // fallback: use hostname directly (system DNS)
        resolve(hostname);
      }
    });
  });
}

/**
 * Sign and send a POST request to the Gateway MCP endpoint with SigV4.
 */
async function gatewayRequest(body) {
  validateGatewayUrl();
  const creds = await fromNodeProviderChain()();
  const url = new URL(GATEWAY_URL);
  const hostname = url.hostname;

  // Resolve via VPC DNS to get internal IP
  const resolvedIP = await resolveHostname(hostname);

  const payload = JSON.stringify(body);
  const payloadHash = sha256(payload);
  const now = new Date();
  const amzDate = now.toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  const dateStamp = amzDate.slice(0, 8);
  const service = "bedrock-agentcore";
  const scope = `${dateStamp}/${REGION}/${service}/aws4_request`;

  const signedHeaderKeys = ["content-type", "host", "x-amz-content-sha256", "x-amz-date"];
  if (creds.sessionToken) signedHeaderKeys.push("x-amz-security-token");
  signedHeaderKeys.sort();
  const signedHeaders = signedHeaderKeys.join(";");

  const headerMap = {
    "content-type": "application/json",
    host: hostname,
    "x-amz-content-sha256": payloadHash,
    "x-amz-date": amzDate,
  };
  if (creds.sessionToken) headerMap["x-amz-security-token"] = creds.sessionToken;

  const canonicalHeaders = signedHeaderKeys.map((k) => `${k}:${headerMap[k]}\n`).join("");
  const canonical = [
    "POST",
    url.pathname,
    "",
    canonicalHeaders,
    signedHeaders,
    payloadHash,
  ].join("\n");

  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, scope, sha256(canonical)].join("\n");

  let key = Buffer.from("AWS4" + creds.secretAccessKey, "utf8");
  for (const part of [dateStamp, REGION, service, "aws4_request"]) {
    key = hmac(key, part);
  }
  const signature = hmac(key, stringToSign).toString("hex");

  const authorization = `AWS4-HMAC-SHA256 Credential=${creds.accessKeyId}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`;

  const reqHeaders = {
    "Content-Type": "application/json",
    Host: hostname,
    Authorization: authorization,
    "x-amz-date": amzDate,
    "x-amz-content-sha256": payloadHash,
  };
  if (creds.sessionToken) reqHeaders["x-amz-security-token"] = creds.sessionToken;

  return new Promise((resolve, reject) => {
    const req = https.request(
      {
        hostname: resolvedIP,
        port: 443,
        path: url.pathname,
        method: "POST",
        servername: hostname,  // SNI
        headers: reqHeaders,
        rejectUnauthorized: true,
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString();
          if (res.statusCode >= 400) {
            reject(new Error(`HTTP ${res.statusCode}: ${text}`));
          } else {
            resolve(JSON.parse(text));
          }
        });
      }
    );
    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

module.exports = { gatewayRequest };
