// src/__tests__/hubApi.test.ts
//
// The Federation Hub calls carry the user's bearer token, so the hub URL must
// not be able to point at a third-party origin by default.
//
// A hardcoded absolute fallback (https://depthsight.diverseinc.net/api/v1/hub)
// would point every deployment that is not hosted on that domain at a
// third-party origin and ship every logged-in user's session token there.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { DEFAULT_HUB_API_PATH, resolveHubApiUrl } from "@/lib/hubApi";

const SRC = join(__dirname, "..");

const walk = (dir: string): string[] => {
	const out: string[] = [];
	for (const entry of readdirSync(dir)) {
		const full = join(dir, entry);
		if (statSync(full).isDirectory()) {
			out.push(...walk(full));
		} else if (/\.(ts|tsx)$/.test(full)) {
			out.push(full);
		}
	}
	return out;
};

describe("resolveHubApiUrl", () => {
	it("defaults to a same-origin relative path", () => {
		expect(resolveHubApiUrl(undefined)).toBe("/api/v1/hub");
		expect(resolveHubApiUrl("")).toBe("/api/v1/hub");
		expect(resolveHubApiUrl("   ")).toBe("/api/v1/hub");
		expect(resolveHubApiUrl(undefined)).toBe(DEFAULT_HUB_API_PATH);
	});

	it("never defaults to an absolute origin", () => {
		const resolved = resolveHubApiUrl(undefined);
		expect(resolved.startsWith("http://")).toBe(false);
		expect(resolved.startsWith("https://")).toBe(false);
		expect(resolved.startsWith("//")).toBe(false);
	});

	it("keeps an explicit override for a genuinely different hub", () => {
		expect(resolveHubApiUrl("https://hub.example.com/api/v1/hub")).toBe(
			"https://hub.example.com/api/v1/hub",
		);
		expect(resolveHubApiUrl("  https://hub.example.com/api/v1/hub  ")).toBe(
			"https://hub.example.com/api/v1/hub",
		);
	});

	it("no source file hardcodes the absolute hub origin any more", () => {
		const offenders = walk(SRC)
			.filter((file) => !file.includes("__tests__"))
			.filter((file) =>
				readFileSync(file, "utf8").includes(
					"https://depthsight.diverseinc.net/api/v1/hub",
				),
			)
			.map((file) => file.slice(SRC.length + 1));

		expect(offenders).toEqual([]);
	});
});
