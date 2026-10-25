// src/__tests__/deadCode.test.ts
//
// FIX 4 -- two dead-code fakes that made the product look like it did more
// than it does:
//   * components/dashboard/StrategyPerformance.tsx -- 100% hardcoded PnL
//     ("VolumeBreakout / +125.5 / 75% win rate"), imported by nothing.
//   * useDeployModel in lib/api.ts -- slept 1s and returned success:true
//     (a fake green "deployed" toast) with no backend endpoint at all.
//
// The live dashboard uses TopStrategiesTable, which fetches real data.

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

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

describe("dead code removal (FIX 4)", () => {
	it("the hardcoded StrategyPerformance component is gone", () => {
		expect(
			existsSync(join(SRC, "components/dashboard/StrategyPerformance.tsx")),
		).toBe(false);
	});

	it("the fake useDeployModel hook is gone", () => {
		const apiSource = readFileSync(join(SRC, "lib/api.ts"), "utf8");
		expect(apiSource).not.toContain("useDeployModel");
	});

	it("nothing imports either of them", () => {
		const offenders = walk(SRC)
			.filter((file) => !file.includes("__tests__"))
			.filter((file) => {
				const source = readFileSync(file, "utf8");
				return (
					source.includes("components/dashboard/StrategyPerformance") ||
					source.includes("useDeployModel")
				);
			});
		expect(offenders).toEqual([]);
	});
});
