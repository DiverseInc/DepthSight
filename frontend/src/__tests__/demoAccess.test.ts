// src/__tests__/demoAccess.test.ts
//
// FIX 3 -- the demo credentials helper must not appear on the public login
// page unless the build explicitly asks for it.

import { describe, expect, it } from "vitest";
import { DEMO_ENV_FLAG, isDemoCredentialsEnabled } from "@/lib/demoAccess";

describe("demo credentials gate (FIX 3)", () => {
	it("is OFF by default (no env)", () => {
		expect(isDemoCredentialsEnabled(undefined)).toBe(false);
		expect(isDemoCredentialsEnabled({})).toBe(false);
		expect(isDemoCredentialsEnabled(null)).toBe(false);
	});

	it("is OFF when the flag is set to a falsy value", () => {
		expect(isDemoCredentialsEnabled({ [DEMO_ENV_FLAG]: "false" })).toBe(false);
		expect(isDemoCredentialsEnabled({ [DEMO_ENV_FLAG]: "0" })).toBe(false);
		expect(isDemoCredentialsEnabled({ [DEMO_ENV_FLAG]: "" })).toBe(false);
		expect(isDemoCredentialsEnabled({ [DEMO_ENV_FLAG]: "no" })).toBe(false);
	});

	it("is ON only when the flag is explicitly truthy", () => {
		expect(isDemoCredentialsEnabled({ [DEMO_ENV_FLAG]: "true" })).toBe(true);
		expect(isDemoCredentialsEnabled({ [DEMO_ENV_FLAG]: "TRUE" })).toBe(true);
		expect(isDemoCredentialsEnabled({ [DEMO_ENV_FLAG]: "1" })).toBe(true);
		expect(isDemoCredentialsEnabled({ [DEMO_ENV_FLAG]: "yes" })).toBe(true);
		expect(isDemoCredentialsEnabled({ [DEMO_ENV_FLAG]: true })).toBe(true);
	});
});
