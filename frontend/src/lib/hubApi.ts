// src/lib/hubApi.ts

/**
 * The Federation Hub lives on the SAME ORIGIN as the API by default.
 *
 * The unset case must resolve to a relative path. A hardcoded absolute
 * fallback (the vendor's own hub origin) would point every deployment that
 * is not hosted on that domain at a third-party origin, and the community hub
 * calls carry the user's bearer token - which would ship every logged-in
 * user's session token to that host.
 *
 * A deployment that genuinely wants a different hub sets VITE_HUB_API_URL
 * explicitly; that override still works.
 */
export const DEFAULT_HUB_API_PATH = "/api/v1/hub";

export const resolveHubApiUrl = (configured?: string): string => {
	const trimmed = configured?.trim();
	return trimmed ? trimmed : DEFAULT_HUB_API_PATH;
};
