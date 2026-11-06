// src/lib/oracleCapability.ts
//
// THE WEB CLIENT MUST NOT FABRICATE AN ORACLE.
//
// The primary web client carried the same fiction the PWA carried:
//
//     const canUseOracle = isAdmin || isPro; // Oracle is available to
//                                            // admins and Pro users
//     ...?.toString() || "1"                 // a regime nobody measured
//     (strategy?.config_data?.oracle_settings?.confidence as number) || 95
//     defaultValue={[field.value || 95]}     // a confidence nobody measured
//
// and then SENT those numbers to the server:
//
//     const regime = parseInt(formData.oracleRegime || "1", 10);
//     const confidence = formData.oracleConfidence || 95;
//
// on a deployment whose api container logs, once per worker:
//
//     WARNING [api.simulation_router:122] - Oracle model not found at
//     /app/data/oracle_model.joblib
//
// There is no `train_oracle.py` in this repository, so nothing produces
// that file: the model is not merely missing, it does not exist yet. The
// 95 the customer saw was a CONSTANT -- never a model output -- and the
// screen told them a regime and a confidence that nothing had produced.
//
// So the client asks the SERVER, and this module is the one place that
// interprets the answer. It is deliberately dependency-free and pure: the
// decision "may I show an oracle regime / confidence?" must have exactly
// one implementation, and it must be testable without a browser.
//
// IT MIRRORS `pwa/lib/oracleCapability.ts` FUNCTION FOR FUNCTION. The two
// clients are separate applications and cannot import each other's
// product code, so the agreement between them is PROVED by a test
// (`src/__tests__/webOracleCapability.test.ts`) that runs both modules
// against the same inputs and asserts the same answers -- not by a
// comment here.
//
// IT ALSO OWNS THE CONFIDENCE THRESHOLD. The threshold is a filter
// setting, not a model output: the minimum confidence a symbol must
// reach to be traded. The server declares it
// (`bot_module.symbol_selection.DEFAULT_ORACLE_CONFIDENCE_PERCENT`, sent
// as `oracleDefaultConfidence` on `GET /api/v1/capabilities`), the
// filter applies it, and the client displays and sends back exactly that.
//
// The reason this matters is a SILENT one. When the clients stopped
// sending a hard-coded 95 and sent 0 instead ("there is no oracle
// here"), the server read 0 as "no threshold at all": the >= 0.95 filter
// became `>= 0.0`, which admits EVERY symbol. The guard was weaker and
// nothing said so. So "not chosen" must never become "no threshold" on
// either side of the wire.

/** The server's answer about the oracle. */
export interface OracleCapability {
	/**
	 * True only when a real oracle model is loaded and usable on the
	 * server. False on every deployment today.
	 */
	oracleAvailable: boolean;
	/**
	 * The minimum oracle confidence this deployment applies, in percent,
	 * when a strategy has no confidence of its own. Null/absent means
	 * the server did not say, and the client then sends nothing rather
	 * than inventing a number.
	 */
	oracleDefaultConfidence?: number | null;
}

/**
 * May the client show an oracle regime and an oracle confidence?
 *
 * True ONLY when the server says a real model is loaded. Everything else
 * -- a failed probe, a missing field, a null capability -- is NO.
 *
 * Failing CLOSED is the point: if the probe cannot be completed, the
 * oracle is not offered. The alternative (assume it is there until the
 * server says otherwise) is how a client ends up telling a customer that
 * a 95% confidence exists when no model has ever produced one.
 */
export function oracleIsUsable(
	capability: OracleCapability | null | undefined,
): boolean {
	return capability?.oracleAvailable === true;
}

/**
 * The server's DECLARED confidence threshold, in percent.
 *
 * Null when the server did not declare one. A client that has no
 * declared threshold sends no confidence at all -- it does not fall back
 * to a number it made up.
 */
export function oracleDefaultConfidencePercent(
	capability: OracleCapability | null | undefined,
): number | null {
	const declared = capability?.oracleDefaultConfidence;
	if (typeof declared !== "number" || !Number.isFinite(declared)) {
		return null;
	}
	return declared > 0 ? declared : null;
}

/**
 * The confidence the client DISPLAYS and SENDS.
 *
 * The customer's own choice when they made one; otherwise the server's
 * declared threshold; otherwise nothing at all.
 *
 * A choice of 0 is treated as "not chosen", because that is exactly what
 * the server does with it: an absent or zero confidence falls back to
 * the declared threshold, and a filter that accepts every symbol is not
 * a filter. Treating 0 as a real choice here would show the customer a
 * 0% threshold while the filter ran at the declared one.
 */
export function effectiveOracleConfidence(
	chosen: number | null | undefined,
	defaultConfidence: number | null | undefined,
): number | null {
	if (typeof chosen === "number" && Number.isFinite(chosen) && chosen > 0) {
		return chosen;
	}
	if (
		typeof defaultConfidence === "number" &&
		Number.isFinite(defaultConfidence) &&
		defaultConfidence > 0
	) {
		return defaultConfidence;
	}
	return null;
}

/**
 * The oracle regime, as the server accepts it (0, 1 or 2).
 *
 * Null when nothing valid was chosen. A regime is never invented: the
 * server refuses a DYNAMIC_ORACLE strategy without one
 * (`bot_module/symbol_selection.py`: "oracle_regime must be provided for
 * DYNAMIC_ORACLE mode"), so the client says so instead of picking one.
 */
export function parseOracleRegime(
	regime: string | number | null | undefined,
): number | null {
	if (regime === null || regime === undefined || regime === "") {
		return null;
	}
	const parsed = typeof regime === "number" ? regime : parseInt(regime, 10);
	if (!Number.isFinite(parsed)) {
		return null;
	}
	return parsed === 0 || parsed === 1 || parsed === 2 ? parsed : null;
}

/**
 * The oracle settings to send when the oracle is NOT usable.
 *
 * Absent, not zero and not a default: `null` means "there is no oracle
 * filter here", which is what the server's own contract expects for a
 * strategy that is not filtering by regime. A number here would be a
 * measurement nobody made.
 */
export const NO_ORACLE_SETTINGS = {
	oracleRegime: null,
	oracleConfidence: null,
} as const;

export interface OracleLaunchInput {
	/** The server's answer: is there a real model? */
	oracleAvailable: boolean;
	/** The regime the customer chose, as the client holds it. */
	regime?: string | number | null;
	/** The confidence the customer chose. */
	confidence?: number | null;
	/** The server's declared threshold, in percent. */
	defaultConfidence?: number | null;
}

export interface OracleLaunchSettings {
	regime: number | null;
	confidence: number | null;
}

/**
 * The oracle numbers that go to the server.
 *
 * Nothing is invented here. With no oracle there are no oracle numbers:
 * the regime and the confidence are absent, and the server is told
 * "there is no oracle filter on this strategy".
 *
 * With an oracle, the regime is the customer's own choice (null when
 * they have not chosen one -- the client refuses to launch rather than
 * picking one) and the confidence is the number the customer chose, or
 * the server's declared threshold when they have not chosen one.
 */
export function buildOracleLaunchSettings(
	input: OracleLaunchInput,
): OracleLaunchSettings {
	if (!input.oracleAvailable) {
		return { regime: null, confidence: null };
	}
	return {
		regime: parseOracleRegime(input.regime),
		confidence: effectiveOracleConfidence(
			input.confidence,
			input.defaultConfidence,
		),
	};
}

/**
 * The dynamic mode a SAVED strategy was stored with.
 *
 * "DYNAMIC_NATR" when the strategy carries NATR settings, "DYNAMIC_ORACLE"
 * when it carries oracle settings, null when it carries neither (the
 * client cannot know what the server would have used).
 */
export function savedDynamicMode(
	configData: unknown,
): "DYNAMIC_NATR" | "DYNAMIC_ORACLE" | null {
	const config = configData as
		| { natr_settings?: unknown; oracle_settings?: unknown }
		| null
		| undefined;
	if (!config) return null;
	if (config.natr_settings) return "DYNAMIC_NATR";
	if (config.oracle_settings) return "DYNAMIC_ORACLE";
	return null;
}

export type OracleLaunchRefusal = "ORACLE_UNAVAILABLE" | null;

/**
 * MUST THE LAUNCH BE REFUSED?
 *
 * A strategy the customer SAVED as an oracle strategy must not be
 * relaunched under a different symbol-selection mode. The PWA did
 * exactly that -- `dynamicMode: canUseOracle ? dynamicMode :
 * "DYNAMIC_NATR"` -- so a DYNAMIC_ORACLE strategy was quietly relaunched
 * as a NATR one. The launch fails loudly instead.
 */
export function oracleLaunchRefusal(
	savedMode: "DYNAMIC_NATR" | "DYNAMIC_ORACLE" | null,
	oracleAvailable: boolean,
): OracleLaunchRefusal {
	if (savedMode === "DYNAMIC_ORACLE" && !oracleAvailable) {
		return "ORACLE_UNAVAILABLE";
	}
	return null;
}
