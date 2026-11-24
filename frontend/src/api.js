// Talks to the backend. The URL is fixed at build time (VITE_API_URL), e.g. http://localhost:8000.
const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");

export class ChatError extends Error {}

/** FastAPI sends {detail: "text"} or {detail: [{msg: "..."}]} (validation). Turn either into one line. */
function describeDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (d && d.msg ? String(d.msg).replace(/^Value error, /, "") : ""))
      .filter(Boolean)
      .join("; ");
  }
  return "";
}

/** Demo switch: opening the page with ?simulate=key_1=429 (or *=429) sends X-Simulate-Failure.
 *  Normal visitors never have the parameter, so they never send the header. */
function simulateHeader() {
  try {
    const v = new URLSearchParams(window.location.search).get("simulate");
    return v && /^[\w*=,]{1,64}$/.test(v) ? { "X-Simulate-Failure": v } : {};
  } catch {
    return {};
  }
}

/** POST /chat. Resolves with the response body, or throws ChatError with a user-readable message. */
export async function sendChat({ sessionId, message, systemPrompt }) {
  const body = { session_id: sessionId, message };
  if (systemPrompt && systemPrompt.trim()) body.system_prompt = systemPrompt.trim();

  let response;
  try {
    response = await fetch(`${API_URL}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...simulateHeader() },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ChatError("Can't reach the server. Check your connection and try again.");
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    /* non-JSON body: handled below by status code */
  }

  if (response.ok && data && data.reply) return data;
  if (response.status === 422) {
    const why = describeDetail(data && data.detail);
    throw new ChatError(`That message couldn't be sent${why ? `: ${why}` : "."} Please edit it and try again.`);
  }
  if (response.status === 503) {
    throw new ChatError("All AI providers are unavailable right now. Please try again in a moment.");
  }
  throw new ChatError(`Something went wrong on the server (status ${response.status}). Please try again.`);
}
