// src/lib/apiClient.ts

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api/v1";

let isRefreshing = false;
let refreshSubscribers: ((token: string) => void)[] = [];

const subscribeTokenRefresh = (cb: (token: string) => void) => {
	refreshSubscribers.push(cb);
};

const onRefreshed = (token: string) => {
	refreshSubscribers.forEach((cb) => cb(token));
	refreshSubscribers = [];
};

// FIX 2026-09-23: extract refresh logic into a standalone exported function so
// the WebSocketProvider can trigger refresh on close code 1008 (policy violation
// = expired/invalid JWT). Without this, the WS stays disconnected with the
// expired token because react-use-websocket retries with the same token every
// 5s forever.
//
// Events dispatched:
//   'auth:token-refreshed' — CustomEvent { detail: { token: string } }
//   'auth:logout'          — CustomEvent (no detail)
// These let AuthContext update its React state without apiClient importing it
// directly (avoids circular import).

export interface RefreshResult {
	ok: boolean;
	token?: string;
}

export const refreshAccessToken = async (): Promise<RefreshResult> => {
	if (isRefreshing) {
		// Another caller is already refreshing; queue and let the original
		// refresh logic notify us via the subscriber pattern below.
		return new Promise<RefreshResult>((resolve) => {
			subscribeTokenRefresh((token: string) => {
				resolve({ ok: true, token });
			});
		});
	}

	const refreshToken = localStorage.getItem("refreshToken");
	if (!refreshToken) {
		window.dispatchEvent(new CustomEvent("auth:logout"));
		return { ok: false };
	}

	isRefreshing = true;
	try {
		const refreshResponse = await fetch(`${API_BASE_URL}/refresh`, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ refresh_token: refreshToken }),
		});

		if (!refreshResponse.ok) {
			localStorage.removeItem("authToken");
			localStorage.removeItem("refreshToken");
			localStorage.removeItem("originalAuthToken");
			localStorage.removeItem("originalRefreshToken");
			window.dispatchEvent(new CustomEvent("auth:logout"));
			return { ok: false };
		}

		const tokenData = await refreshResponse.json();
		localStorage.setItem("authToken", tokenData.access_token);
		if (tokenData.refresh_token) {
			localStorage.setItem("refreshToken", tokenData.refresh_token);
		}
		onRefreshed(tokenData.access_token);
		window.dispatchEvent(
			new CustomEvent("auth:token-refreshed", {
				detail: { token: tokenData.access_token },
			}),
		);
		return { ok: true, token: tokenData.access_token };
	} catch {
		localStorage.removeItem("authToken");
		localStorage.removeItem("refreshToken");
		localStorage.removeItem("originalAuthToken");
		localStorage.removeItem("originalRefreshToken");
		window.dispatchEvent(new CustomEvent("auth:logout"));
		return { ok: false };
	} finally {
		isRefreshing = false;
	}
};

export const apiClient = async <T>(
	endpoint: string,
	options: RequestInit = {},
): Promise<T> => {
	const fullUrl = `${API_BASE_URL}${endpoint}`;
	const token = localStorage.getItem("authToken");
	const headers = new Headers(options.headers);
	headers.set("Cache-Control", "no-cache");
	headers.set("Pragma", "no-cache");
	if (!options.body || !(options.body instanceof FormData)) {
		headers.set("Content-Type", "application/json");
	}
	if (token) {
		headers.set("Authorization", `Bearer ${token}`);
	}
	let response = await fetch(fullUrl, { ...options, headers });

	if (
		response.status === 401 &&
		!endpoint.includes("/token") &&
		!endpoint.includes("/refresh")
	) {
		const refreshToken = localStorage.getItem("refreshToken");
		if (refreshToken) {
			const result = await refreshAccessToken();
			if (!result.ok || !result.token) {
				// refreshAccessToken already dispatched auth:logout + cleared
				// localStorage; AuthContext listener will redirect to /login.
				throw new Error("Authentication failed");
			}

			// Retry original request with new token
			const retryHeaders = new Headers(options.headers);
			retryHeaders.set("Cache-Control", "no-cache");
			retryHeaders.set("Pragma", "no-cache");
			if (!options.body || !(options.body instanceof FormData)) {
				retryHeaders.set("Content-Type", "application/json");
			}
			retryHeaders.set("Authorization", `Bearer ${result.token}`);
			response = await fetch(fullUrl, { ...options, headers: retryHeaders });
		}
	}

	if (!response.ok) {
		const errorBody = (await response.json().catch(() => ({}))) as Record<
			string,
			string | undefined
		>;
		let message =
			errorBody.detail ||
			errorBody.error ||
			`Request failed with status ${response.status}`;
		if (typeof message !== "string") {
			message = JSON.stringify(message);
		}
		throw new Error(message);
	}
	if (response.status === 204) {
		return undefined as T;
	}
	const parsedResponse = await response.json();
	return parsedResponse &&
		typeof parsedResponse === "object" &&
		"data" in parsedResponse
		? parsedResponse.data
		: parsedResponse;
};
