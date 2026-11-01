// Where the API is. On a private install the answer is "here".
//
// This page is served by the same process that holds the memories: the image
// builds it and the application serves it at `/`, so the API is the origin the
// browser already has. There is no hostname to configure and nothing to get
// wrong at build time, which is the point of the whole arrangement. The
// version this replaces defaulted to the hosted service's address and printed
// an error when a build forgot to say otherwise.
//
// VITE_BACKEND_URL still overrides it, for running `npm run dev` on 5174
// against an install on 8000 (README.md in this directory).

const fromEnv = import.meta.env.VITE_BACKEND_URL as string | undefined;

export const BACKEND_URL: string =
  fromEnv || (typeof window !== "undefined" ? window.location.origin : "");

// No `/api` segment: this install serves `/admin` and `/v1` at the root.
export const API_BASE: string = BACKEND_URL;
