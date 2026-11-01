import { isAxiosError } from "axios";

/**
 * The one way this screen reads a refusal from the server.
 *
 * Every error body this install sends carries `code` and `message` at the top
 * level, next to the `detail` it always sent (korely_agent/api/errors.py,
 * error_body): the endpoint's own code when it names one, the status's code
 * otherwise, and a sentence for a person, the first failed field of a 422
 * included.
 *
 * Until the audit of 17 Oct 2026 the dashboard read that body with two rules:
 * agentsApi.errorMessage took `detail` only when it was a string, so a 422 or
 * a `{code, message}` refusal showed the page's generic sentence, while
 * hosted.hostedError read the `message` the server sends every time. Both are
 * this function now. `detail` is still read when the top level is missing,
 * for a body that did not come through error_body.
 *
 * The unhandled 500 (`internal_error`, main.py) is not a refusal with a
 * reason: its message is "internal error", which tells a person less than the
 * page's own "nothing was changed, try again". It reads as no message, and the
 * page says its sentence.
 */
export interface ApiError {
  code: string;
  message: string;
}

/** The HTTP status of a failed call; null when no answer came back (the
 *  network, a stopped proxy), undefined when `e` is not a call's error. */
export function apiStatus(e: unknown): number | null | undefined {
  if (!isAxiosError(e)) return undefined;
  return e.response ? e.response.status : null;
}

export function apiError(e: unknown): ApiError | null {
  if (!isAxiosError(e)) return null;
  const data = e.response?.data;
  if (!data || typeof data !== "object") return null;
  let found: ApiError | null = null;
  if (typeof data.message === "string" && data.message) {
    found = { code: String(data.code ?? ""), message: data.message };
  } else {
    const detail = data.detail;
    if (detail && typeof detail === "object" && typeof detail.message === "string") {
      found = { code: String(detail.code ?? ""), message: detail.message };
    } else if (typeof detail === "string" && detail) {
      found = { code: String(data.code ?? ""), message: detail };
    }
  }
  if (found?.code === "internal_error") return null;
  return found;
}
