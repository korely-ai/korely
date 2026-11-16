import axios, { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { API_BASE } from "@/config/backendUrl";
import { NOT_DATA, NOT_DATA_SAID } from "@/lib/apiError";

// Thin axios client for the dashboard of a private install.
//
// It reads a token from localStorage and sends it as Bearer, and it does not
// know or care where that token came from. That is why swapping the identity
// provider touched five files and not fifty: this one was already indifferent.
//
// The token is a `kor_sess_` session, opened by POST /admin/session with a
// username and a password. The `kor_self_` API keys are a separate surface,
// for the developer's own agents calling /v1; the screen never uses one.

/** Where the session token lives in this browser. Both sign-ins write it
 *  (contexts/AuthContext.tsx, hosted/auth.tsx) and this client reads it: one
 *  name, here, where it used to be a constant there and a literal here. */
export const TOKEN_KEY = "token";

/** How long the screen waits for an answer before it says the request
 *  failed. The server's own waits on a third party fit inside it (a
 *  provider's list of models, LIST_TIMEOUT_SECONDS, 20 s, in
 *  services/model_choice.py), and past it a person is better told than left
 *  looking at a spinner (audit of 17 Oct 2026: it was a literal). */
export const REQUEST_TIMEOUT_MS = 30_000;

const apiClient = axios.create({
  baseURL: API_BASE,
  headers: { "Content-Type": "application/json" },
  timeout: REQUEST_TIMEOUT_MS,
});

apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  if (config.data instanceof FormData) {
    delete config.headers["Content-Type"];
  }
  return config;
});

/** Whether a 2xx answer is not data: a body axios could not read as JSON
 *  (it hands such a body over as the text it was) under a type that is not
 *  JSON. Every route the screen calls answers JSON or nothing (a 204), so
 *  this is a page that something in front of the install sent in its
 *  place (a proxy, a maintenance page). It used to reach the page as its
 *  data, and `(data ?? []).map` emptied the whole screen (QA of 14 Nov
 *  2026). */
export function notData(response: AxiosResponse): boolean {
  const body = response.data;
  if (typeof body !== "string" || body.trim() === "") return false;
  const type = AxiosHeaders.from(response.headers as Record<string, string>).get("content-type");
  return !(typeof type === "string" && /json/i.test(type));
}

apiClient.interceptors.response.use(
  (response) => {
    if (!notData(response)) return response;
    // A refusal like any other, with its own code (lib/apiError.ts, NOT_DATA),
    // so every page says what happened in the one way it says a failure.
    return Promise.reject(
      new AxiosError(NOT_DATA_SAID, NOT_DATA, response.config, response.request, response),
    );
  },
  (error) => {
    if (error.response?.status === 401) {
      // The providers listen and clear the rest (AuthContext `clear`). The
      // "userEmail" removed here was a key nothing has written since the
      // Firebase sign-in left the install (audit of 17 Oct 2026).
      // `hadSession`: a session this browser held was refused, so the
      // sign-in page says it ended (SESSION_ENDED); a wrong password with no
      // session is not one.
      const hadSession = localStorage.getItem(TOKEN_KEY) !== null;
      localStorage.removeItem(TOKEN_KEY);
      window.dispatchEvent(new CustomEvent("auth:session-expired", { detail: { hadSession } }));
    }
    return Promise.reject(error);
  },
);

export default apiClient;
